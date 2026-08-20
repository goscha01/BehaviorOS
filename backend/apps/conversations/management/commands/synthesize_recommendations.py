"""Pipeline 1C — synthesize BehaviorOS recommendations from validated
evidence.

Usage:
    python manage.py synthesize_recommendations \\
        --org <uuid> \\
        --state-inference-run <uuid> \\
        --config-snapshot <uuid> \\
        [--analysis-run <uuid>] \\
        [--model gpt-4o-mini]

Loads the 1B-6 state-inference results + 1B-4 config snapshot, runs
the deterministic eligibility engine, drafts LLM prose per candidate,
persists RecommendationRun + BehaviorRecommendation rows, and prints
the full 1C report.

Cost: ~$0.001-0.002 per candidate. Expected 6-8 candidates for
Spotless v3, so total ~$0.02.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Organization
from apps.conversations.analysis.conditional import (
    ConditionalConfig, build_records, stratified_split,
)
from apps.conversations.analysis.recommendation_synthesis import (
    STATE_TO_SIGNALS, SYNTHESIZER_VERSION, StateEvidence,
    TransitionEvidence, build_candidates, draft_prose,
)
from apps.conversations.analysis.state_inference import (
    STATE_AT_RISK, STATE_BOOKING_INTENT, STATE_ENGAGED, STATE_EXPLORING,
    STATE_HIGH_INTENT,
)
from apps.conversations.models import (
    BehavioralPolicy, BehaviorRecommendation, ConditionalAnalysisRun,
    CustomerStateInferenceRun, InferredCustomerState, LearningCorpus,
    RecommendationRun, TenantConfigSnapshot,
)
from apps.learning.services.llm_client import LearningLLMClient


class Command(BaseCommand):
    help = 'Pipeline 1C — synthesize BehaviorOS recommendations.'

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True)
        parser.add_argument('--state-inference-run', required=True)
        parser.add_argument('--config-snapshot', required=True)
        parser.add_argument('--analysis-run', default='',
                            help='Optional ConditionalAnalysisRun for outcome-stat linkage')
        parser.add_argument('--model', default='gpt-4o-mini')
        parser.add_argument('--split-seed', type=int, default=42)

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(pk=options['org'])
        except Organization.DoesNotExist as exc:
            raise CommandError(f'org not found: {options["org"]}') from exc
        try:
            infer_run = CustomerStateInferenceRun.objects.get(
                pk=options['state_inference_run'],
            )
        except CustomerStateInferenceRun.DoesNotExist as exc:
            raise CommandError(
                f'state inference run not found: {options["state_inference_run"]}'
            ) from exc
        try:
            snapshot = TenantConfigSnapshot.objects.get(pk=options['config_snapshot'])
        except TenantConfigSnapshot.DoesNotExist as exc:
            raise CommandError(
                f'config snapshot not found: {options["config_snapshot"]}'
            ) from exc
        analysis_run = None
        if options['analysis_run']:
            try:
                analysis_run = ConditionalAnalysisRun.objects.get(
                    pk=options['analysis_run'],
                )
            except ConditionalAnalysisRun.DoesNotExist as exc:
                raise CommandError(
                    f'analysis run not found: {options["analysis_run"]}'
                ) from exc

        corpus = infer_run.corpus
        self.stdout.write(self.style.NOTICE(
            f'1C synthesis: state_inference={infer_run.pk} '
            f'config_snapshot={snapshot.pk} '
            f'analysis_run={analysis_run.pk if analysis_run else "(none)"} '
            f'model={options["model"]}'
        ))

        # --- 1. Compute state evidence from persisted transitions ---
        state_ev, transition_ev = self._compute_evidence(
            infer_run, seed=options['split_seed'],
        )

        # --- 2. Load policies + build candidates (deterministic) ---
        policies = list(BehavioralPolicy.objects.filter(snapshot=snapshot))
        candidates = build_candidates(
            state_evidence=state_ev,
            transition_evidence=transition_ev,
            policies=policies,
            at_risk_is_validated=False,   # 1B-6 said AT_RISK not validated
        )
        self.stdout.write(
            f'  eligibility engine produced {len(candidates)} candidates'
        )

        # --- 3. Create RecommendationRun + persist ---
        run, created = RecommendationRun.objects.get_or_create(
            corpus=corpus,
            state_inference_run=infer_run,
            config_snapshot=snapshot,
            synthesizer_version=SYNTHESIZER_VERSION,
            defaults={
                'org': org,
                'conditional_analysis_run': analysis_run,
                'llm_model': options['model'],
                'status': RecommendationRun.Status.RUNNING,
                'started_at': timezone.now(),
            },
        )
        if not created:
            self.stdout.write(self.style.WARNING(
                f'reusing existing run {run.pk} — not re-drafting'
            ))
        client = LearningLLMClient()
        recs_persisted = []
        if created:
            with transaction.atomic():
                for i, cand in enumerate(candidates, 1):
                    prose = draft_prose(
                        cand, llm_client=client, model=options['model'],
                    )
                    run.llm_input_tokens += prose.llm_input_tokens
                    run.llm_output_tokens += prose.llm_output_tokens
                    run.llm_cost_usd += prose.llm_cost_usd
                    rec = BehaviorRecommendation.objects.create(
                        run=run,
                        recommendation_id=f'R{i:04d}',
                        rec_class=cand.rec_class,
                        confidence=cand.confidence,
                        subject_state=cand.subject_state,
                        subject_signals=cand.subject_signals,
                        linked_policy_ids=cand.linked_policy_ids,
                        linked_transition=cand.linked_transition,
                        evidence=cand.evidence,
                        supporting_conversation_ids=cand.supporting_conversation_ids,
                        observation=prose.observation,
                        interpretation=prose.interpretation,
                        proposed_action_scope=cand.proposed_action_scope,
                        proposed_action=prose.proposed_action,
                        limitations=prose.limitations,
                    )
                    recs_persisted.append(rec)
                run.recommendations_generated = len(recs_persisted)
                run.status = RecommendationRun.Status.COMPLETED
                run.completed_at = timezone.now()
                run.save()
        else:
            recs_persisted = list(run.recommendations.all())

        self.stdout.write(
            f'  persisted {len(recs_persisted)} recommendations, '
            f'llm_tokens=in{run.llm_input_tokens}/out{run.llm_output_tokens} '
            f'cost=${run.llm_cost_usd}'
        )

        # --- 4. Report ---
        self._print_report(run, recs_persisted)

    def _compute_evidence(self, infer_run, *, seed):
        """Rebuild state + transition evidence from persisted state
        inference. Uses the same discovery/holdout split as 1B-5/1B-6."""
        corpus = infer_run.corpus
        cfg = ConditionalConfig()
        records, _ = build_records(corpus, infer_run.extraction_run, config=cfg)
        discovery, holdout = stratified_split(
            records, discovery_fraction=cfg.discovery_fraction, seed=seed,
        )
        d_ids = {r.conversation_id for r in discovery}
        h_ids = {r.conversation_id for r in holdout}
        outcomes = {r.conversation_id: r.outcome_class for r in records}
        d_pos = sum(1 for c in d_ids if outcomes.get(c) == 'positive')
        d_neg = sum(1 for c in d_ids if outcomes.get(c) == 'negative')
        d_baseline = d_pos / (d_pos + d_neg) if (d_pos + d_neg) else 0.0
        h_pos = sum(1 for c in h_ids if outcomes.get(c) == 'positive')
        h_neg = sum(1 for c in h_ids if outcomes.get(c) == 'negative')
        h_baseline = h_pos / (h_pos + h_neg) if (h_pos + h_neg) else 0.0

        # Load all persisted transitions + reconstruct which conversations
        # entered which states / used which transitions.
        transitions_by_conv: dict[str, list[InferredCustomerState]] = defaultdict(list)
        for t in (InferredCustomerState.objects
                  .filter(inference_run=infer_run)
                  .order_by('conversation_id', 'ordinal')):
            transitions_by_conv[str(t.conversation_id)].append(t)

        # Per-state evidence
        state_ev: dict[str, StateEvidence] = {}
        for state in [STATE_EXPLORING, STATE_ENGAGED, STATE_HIGH_INTENT,
                       STATE_BOOKING_INTENT, STATE_AT_RISK]:
            d_state_convs = [
                c for c in d_ids
                if any(t.state == state for t in transitions_by_conv[c])
            ]
            h_state_convs = [
                c for c in h_ids
                if any(t.state == state for t in transitions_by_conv[c])
            ]
            d_state_pos = sum(1 for c in d_state_convs if outcomes[c] == 'positive')
            d_state_neg = sum(1 for c in d_state_convs if outcomes[c] == 'negative')
            n_d = len(d_state_convs)
            d_rate = (d_state_pos / n_d) if n_d else 0.0
            d_lift = d_rate - d_baseline
            h_state_pos = sum(1 for c in h_state_convs if outcomes[c] == 'positive')
            n_h = len(h_state_convs)
            h_rate = (h_state_pos / n_h) if n_h else 0.0
            h_lift = h_rate - h_baseline
            same_sign = (d_lift >= 0 and h_lift >= 0) or (d_lift < 0 and h_lift < 0)
            reproduced = same_sign and n_h >= 3
            # Positive-outcome evidence conv IDs
            pos_ids = [c for c in d_state_convs if outcomes[c] == 'positive']
            state_ev[state] = StateEvidence(
                state=state, n_discovery=n_d,
                d_positive_rate=round(d_rate, 4),
                d_baseline=round(d_baseline, 4),
                d_lift=round(d_lift, 4),
                h_lift=round(h_lift, 4),
                h_n=n_h,
                holdout_reproduced=reproduced,
                supporting_conversation_ids=pos_ids[:20],
            )

        # Transitions
        trans_convs: dict[tuple, list[str]] = defaultdict(list)
        for c, ts in transitions_by_conv.items():
            if c not in d_ids:
                continue
            for t in ts:
                trans_convs[(t.previous_state, t.state)].append(c)
        transition_ev: list[TransitionEvidence] = []
        for (prev, cur), convs in trans_convs.items():
            n = len(convs)
            pos = sum(1 for c in convs if outcomes.get(c) == 'positive')
            neg = n - pos
            rate = (pos / n) if n else 0.0
            lift = rate - d_baseline
            transition_ev.append(TransitionEvidence(
                previous_state=prev, state=cur, n=n,
                positive_rate=round(rate, 4),
                lift=round(lift, 4),
                supporting_conversation_ids=[c for c in convs if outcomes.get(c) == 'positive'][:20],
            ))
        return state_ev, transition_ev

    def _print_report(self, run, recs):
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'=== Pipeline 1C recommendation report — run {run.pk} ==='
        ))
        by_class = Counter(r.rec_class for r in recs)
        by_confidence = Counter(r.confidence for r in recs)
        self.stdout.write(f'  synthesizer:  {run.synthesizer_version}')
        self.stdout.write(f'  corpus:       {run.corpus.name}@{run.corpus.version}')
        self.stdout.write(f'  state_run:    {run.state_inference_run.pk}')
        self.stdout.write(f'  snapshot:     {run.config_snapshot.pk}')
        self.stdout.write(f'  llm_model:    {run.llm_model}')
        self.stdout.write(f'  cost:         ${run.llm_cost_usd}')
        self.stdout.write(f'  by class:     {dict(by_class)}')
        self.stdout.write(f'  by confidence:{dict(by_confidence)}')
        self.stdout.write('')
        for rec in recs:
            self._print_one(rec)

        # Acceptance gate
        useful = [r for r in recs
                  if r.rec_class in (
                      BehaviorRecommendation.RecClass.STATE_COVERAGE_GAP,
                      BehaviorRecommendation.RecClass.STATE_PARTIAL_COVERAGE,
                      BehaviorRecommendation.RecClass.CONFIG_ALIGNMENT,
                      BehaviorRecommendation.RecClass.OBSERVED_STATE_INSIGHT,
                  )
                  and r.confidence in (
                      BehaviorRecommendation.Confidence.HIGH,
                      BehaviorRecommendation.Confidence.MEDIUM,
                      BehaviorRecommendation.Confidence.LOW,
                  )]
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=== Acceptance gate ==='))
        if len(useful) >= 2:
            self.stdout.write(self.style.SUCCESS(
                f'PASS: {len(useful)} recommendations with actionable class + '
                f'non-INSUFFICIENT confidence.'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f'NULL: only {len(useful)} actionable recommendations generated. '
                f'System did not clear the 2-3 useful-findings threshold '
                f'at v3 extraction quality + current corpus size.'
            ))

    def _print_one(self, rec):
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n{rec.recommendation_id}  [{rec.rec_class}]  confidence={rec.confidence}'
        ))
        if rec.subject_state:
            self.stdout.write(f'  subject_state:   {rec.subject_state}')
        if rec.subject_signals:
            self.stdout.write(f'  subject_signals: {rec.subject_signals}')
        if rec.linked_transition:
            self.stdout.write(f'  transition:      {rec.linked_transition}')
        if rec.linked_policy_ids:
            self.stdout.write(f'  linked_policies: {len(rec.linked_policy_ids)}')
        self.stdout.write(f'  OBSERVATION:     {rec.observation}')
        self.stdout.write(f'  INTERPRETATION:  {rec.interpretation}')
        if rec.proposed_action:
            self.stdout.write(f'  PROPOSED ACTION [{rec.proposed_action_scope}]: {rec.proposed_action}')
        else:
            self.stdout.write(f'  PROPOSED ACTION: (none — scope={rec.proposed_action_scope})')
        self.stdout.write(f'  LIMITATIONS:     {rec.limitations}')
        self.stdout.write(f'  EVIDENCE:        {rec.evidence}')
        if rec.supporting_conversation_ids:
            self.stdout.write(
                f'  evidence_convs:  {rec.supporting_conversation_ids[:5]} '
                f'(+{max(0, len(rec.supporting_conversation_ids) - 5)} more)'
            )
