"""Pipeline 1B-6: CustomerState v1 — infer, persist, validate, report.

Runs deterministic state inference over an existing extraction,
persists the state history, validates outcomes, tests incremental
value vs raw signals, and reports config coverage per state.

Usage:
    python manage.py analyze_customer_states_v2 \\
        --org <uuid> \\
        --corpus spotless_lb_quo@v2 \\
        --extractor-model gpt-4o-mini \\
        [--split-seed 42] \\
        [--config-snapshot <uuid>]

Persists CustomerStateInferenceRun + InferredCustomerState rows.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Organization
from apps.conversations.analysis.conditional import (
    ConditionalConfig, build_records, stratified_split,
)
from apps.conversations.analysis.discovery import _diff_ci
from apps.conversations.analysis.state_inference import (
    AT_RISK_MIN_SIGNALS, BOOKING_INTENT_SIGNALS,
    CANDIDATE_AT_RISK_SIGNALS, ENGAGED_SIGNALS, EXPLORING_SIGNALS,
    HIGH_INTENT_SIGNALS, INFERENCE_VERSION, QUARANTINED_SIGNALS,
    STATE_AT_RISK, STATE_BOOKING_INTENT, STATE_ENGAGED, STATE_EXPLORING,
    STATE_HIGH_INTENT, STATE_UNKNOWN,
    STATES, infer_state_history,
)
from apps.conversations.models import (
    BehavioralPolicy, ConversationSemanticEvent, ConversationTurn,
    CustomerStateInferenceRun, InferredCustomerState, LearningCorpus,
    SemanticExtractionRun, TenantConfigSnapshot,
)
from apps.conversations.semantic.extractor import EXTRACTOR_VERSION
from apps.conversations.semantic.ontology import ONTOLOGY_VERSION
from apps.conversations.semantic.prompt import PROMPT_VERSION


# Threshold (from 1B-5 convention): |lift| this large counts as material
MATERIAL_LIFT = 0.10
# When comparing state vs component signal, "coverage gain" that counts
# as meaningful aggregation (state covers ≥ this many more conversations
# than its best single component)
COVERAGE_GAIN_MIN = 5
# AT_RISK validation: positive rate must be at least this many points
# BELOW baseline to count as validated
AT_RISK_VALIDATION_DELTA = 0.10


class Command(BaseCommand):
    help = 'Pipeline 1B-6 CustomerState v1 — infer, validate, report.'

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True)
        parser.add_argument('--corpus', required=True, help='name@version')
        parser.add_argument('--extractor-model', required=True)
        parser.add_argument('--split-seed', type=int, default=42)
        parser.add_argument('--config-snapshot', default='')

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(pk=options['org'])
        except Organization.DoesNotExist as exc:
            raise CommandError(f'org not found: {options["org"]}') from exc
        try:
            name, version = options['corpus'].split('@', 1)
        except ValueError as exc:
            raise CommandError('--corpus must be name@version') from exc
        try:
            corpus = LearningCorpus.objects.get(
                org=org, name=name, version=version,
            )
        except LearningCorpus.DoesNotExist as exc:
            raise CommandError(f'corpus {name}@{version} not found') from exc
        try:
            extraction_run = SemanticExtractionRun.objects.get(
                corpus=corpus,
                extractor_version=EXTRACTOR_VERSION,
                ontology_version=ONTOLOGY_VERSION,
                prompt_version=PROMPT_VERSION,
                model=options['extractor_model'],
            )
        except SemanticExtractionRun.DoesNotExist as exc:
            raise CommandError(
                f'no extraction run for {name}@{version} '
                f'{EXTRACTOR_VERSION}/{ONTOLOGY_VERSION}/{PROMPT_VERSION}/'
                f'{options["extractor_model"]}'
            ) from exc

        # ---- Config-snapshot hash for audit trail ----
        rules_snapshot = {
            'version': INFERENCE_VERSION,
            'exploring': sorted(EXPLORING_SIGNALS),
            'engaged': sorted(ENGAGED_SIGNALS),
            'high_intent': sorted(HIGH_INTENT_SIGNALS),
            'booking_intent': sorted(BOOKING_INTENT_SIGNALS),
            'candidate_at_risk': sorted(CANDIDATE_AT_RISK_SIGNALS),
            'quarantined': sorted(QUARANTINED_SIGNALS),
            'at_risk_min_signals': AT_RISK_MIN_SIGNALS,
        }
        rules_hash = hashlib.sha256(
            json.dumps(rules_snapshot, sort_keys=True).encode('utf-8'),
        ).hexdigest()

        # ---- Load records ----
        cfg = ConditionalConfig()
        records, load_meta = build_records(corpus, extraction_run, config=cfg)
        if len(records) < 20:
            raise CommandError(f'too few records: {len(records)}')

        self.stdout.write(self.style.NOTICE(
            f'CustomerState v1 inference: extraction={extraction_run.pk} '
            f'records={load_meta["n_included"]} '
            f'excluded_lead_mismatch={load_meta["n_excluded_lead_mismatch"]} '
            f'rules_hash={rules_hash[:12]}'
        ))

        # ---- Create / reuse inference run ----
        run, created = CustomerStateInferenceRun.objects.get_or_create(
            corpus=corpus, extraction_run=extraction_run,
            inference_version=INFERENCE_VERSION,
            defaults={
                'org': org,
                'status': CustomerStateInferenceRun.Status.PENDING,
                'config_snapshot_hash': rules_hash,
            },
        )
        if not created:
            self.stdout.write(self.style.WARNING(
                f'reusing existing inference run {run.pk} '
                f'(created {run.created_at.isoformat()}) — '
                f'not overwriting existing state rows'
            ))
        else:
            run.started_at = timezone.now()
            run.status = CustomerStateInferenceRun.Status.RUNNING
            run.save(update_fields=['started_at', 'status', 'updated_at'])

        # ---- Infer state histories ----
        # Load all events for the records in one query
        conv_ids = [r.conversation_id for r in records]
        events_by_conv = defaultdict(list)
        for r in records:
            for ev in r.events:
                events_by_conv[r.conversation_id].append(ev)

        histories = {}
        n_transitions = 0
        for r in records:
            h = infer_state_history(r.events, r.conversation_id)
            histories[r.conversation_id] = h
            n_transitions += len(h.transitions)

        if created:
            # Persist transitions in a single transaction
            with transaction.atomic():
                for conv_id, h in histories.items():
                    for t in h.transitions:
                        InferredCustomerState.objects.create(
                            inference_run=run,
                            conversation_id=conv_id,
                            ordinal=t.ordinal,
                            state=t.state,
                            previous_state=t.previous_state,
                            trigger_event_types=t.trigger_event_types,
                            trigger_event_ordinals=t.trigger_event_ordinals,
                            effective_turn=t.effective_turn,
                            reason=t.reason,
                        )
                run.n_conversations_inferred = len(records)
                run.n_transitions_emitted = n_transitions
                run.status = CustomerStateInferenceRun.Status.COMPLETED
                run.completed_at = timezone.now()
                run.save()

        self.stdout.write(
            f'  histories: {len(histories)} conversations, '
            f'{n_transitions} transitions'
        )

        # ---- Split for validation ----
        discovery, holdout = stratified_split(
            records, discovery_fraction=cfg.discovery_fraction,
            seed=options['split_seed'],
        )
        d_ids = {r.conversation_id for r in discovery}
        h_ids = {r.conversation_id for r in holdout}
        d_hist = {cid: histories[cid] for cid in d_ids}
        h_hist = {cid: histories[cid] for cid in h_ids}
        d_out = {r.conversation_id: r.outcome_class for r in discovery}
        h_out = {r.conversation_id: r.outcome_class for r in holdout}
        d_baseline = _rate(sum(1 for c in d_ids if d_out[c] == 'positive'),
                            sum(1 for c in d_ids if d_out[c] == 'negative'))

        # ---- Report ----
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            '=== Pipeline 1B-6 CustomerState v1 report ==='
        ))
        self.stdout.write(f'inference run: {run.pk}')
        self.stdout.write(f'rules hash:    {rules_hash[:16]}')
        self.stdout.write(f'baseline positive rate (discovery): {d_baseline:.2%}')
        self.stdout.write(
            f'discovery: n={len(d_ids)}  '
            f'holdout: n={len(h_ids)}'
        )

        # State entry distribution + outcome
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'State entry distribution (any-time entry) — discovery + outcome:'
        ))
        state_outcomes = self._state_outcome_stats(d_hist, d_out)
        for state in [STATE_EXPLORING, STATE_ENGAGED, STATE_HIGH_INTENT,
                       STATE_BOOKING_INTENT, STATE_AT_RISK]:
            info = state_outcomes.get(state)
            if not info:
                self.stdout.write(f'  {state:20}  (no conversations entered)')
                continue
            n_in, n_pos, n_neg, rate = info
            lift = rate - d_baseline
            self.stdout.write(
                f'  {state:20}  n_entered={n_in:3}  pos={n_pos:3}  '
                f'neg={n_neg:3}  rate={rate:.2%}  lift={lift:+.2f}'
            )

        # Holdout replication for the main states
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Holdout replication (per state):'
        ))
        h_baseline = _rate(sum(1 for c in h_ids if h_out[c] == 'positive'),
                            sum(1 for c in h_ids if h_out[c] == 'negative'))
        self.stdout.write(f'  holdout baseline: {h_baseline:.2%}')
        h_state_outcomes = self._state_outcome_stats(h_hist, h_out)
        for state in [STATE_EXPLORING, STATE_ENGAGED, STATE_HIGH_INTENT,
                       STATE_BOOKING_INTENT, STATE_AT_RISK]:
            d_info = state_outcomes.get(state)
            h_info = h_state_outcomes.get(state)
            if not d_info or not h_info:
                self.stdout.write(f'  {state:20}  (missing on one split)')
                continue
            _, _, _, d_rate = d_info
            h_n, _, _, h_rate = h_info
            d_lift = d_rate - d_baseline
            h_lift = h_rate - h_baseline
            same_sign = (d_lift >= 0 and h_lift >= 0) or (d_lift < 0 and h_lift < 0)
            status = 'REPRODUCED' if same_sign and h_n >= 3 else (
                'UNDERPOWERED' if h_n < 3 else 'NOT_REPRODUCED'
            )
            self.stdout.write(
                f'  {state:20}  d_lift={d_lift:+.2f}  h_lift={h_lift:+.2f}  '
                f'h_n={h_n}  {status}'
            )

        # Transitions distribution
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Transition distribution (discovery):'
        ))
        trans_counts = Counter()
        for h in d_hist.values():
            for t in h.transitions:
                trans_counts[(t.previous_state, t.state)] += 1
        for (prev, cur), n in sorted(trans_counts.items(), key=lambda x: -x[1])[:15]:
            self.stdout.write(f'  {prev:18} → {cur:18} n={n}')

        # Transitions + outcome for supported ones
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Transition outcome rates (n>=5, discovery):'
        ))
        trans_outcome = defaultdict(lambda: {'pos': 0, 'neg': 0})
        for cid, h in d_hist.items():
            outcome = d_out[cid]
            for t in h.transitions:
                key = (t.previous_state, t.state)
                if outcome == 'positive':
                    trans_outcome[key]['pos'] += 1
                else:
                    trans_outcome[key]['neg'] += 1
        for key, cnt in sorted(trans_outcome.items(),
                                key=lambda x: -(x[1]['pos'] + x[1]['neg'])):
            total = cnt['pos'] + cnt['neg']
            if total < 5:
                continue
            rate = cnt['pos'] / total
            lift = rate - d_baseline
            prev, cur = key
            self.stdout.write(
                f'  {prev:18} → {cur:18}  n={total:3}  '
                f'pos={cnt["pos"]:3} neg={cnt["neg"]:3}  '
                f'rate={rate:.2%}  lift={lift:+.2f}'
            )

        # AT_RISK validation
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('AT_RISK validation'))
        ar_info = state_outcomes.get(STATE_AT_RISK)
        if not ar_info or ar_info[0] < 5:
            self.stdout.write(
                '  AT_RISK inferred < 5 discovery conversations → '
                'INSUFFICIENT_EVIDENCE for validation. '
                'AT_RISK reported structurally but not validated as a '
                'true risk state.'
            )
            at_risk_validated = False
        else:
            n_in, n_pos, n_neg, ar_rate = ar_info
            if (d_baseline - ar_rate) >= AT_RISK_VALIDATION_DELTA:
                self.stdout.write(
                    f'  VALIDATED: AT_RISK rate={ar_rate:.2%} vs baseline '
                    f'{d_baseline:.2%} → delta={d_baseline - ar_rate:+.2f} '
                    f'(threshold {AT_RISK_VALIDATION_DELTA:+.2f})'
                )
                at_risk_validated = True
            else:
                self.stdout.write(
                    f'  NOT VALIDATED: AT_RISK rate={ar_rate:.2%} vs '
                    f'baseline {d_baseline:.2%} → delta='
                    f'{d_baseline - ar_rate:+.2f} does not exceed '
                    f'threshold. AT_RISK exists structurally but does '
                    f'NOT predict loss in this corpus. Do not treat as '
                    f'a validated risk state in v1.'
                )
                at_risk_validated = False

        # Incremental-value comparison
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Incremental value vs raw signals (discovery)'
        ))
        self._compare_state_vs_signals(records, discovery, d_hist, d_out,
                                        d_baseline)

        # Config coverage per state
        snapshot = self._resolve_snapshot(
            org, options.get('config_snapshot') or None,
        )
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'Config coverage per state (snapshot {snapshot.pk if snapshot else "(none)"})'
        ))
        if snapshot is None:
            self.stdout.write('  (skipping — no snapshot)')
        else:
            policies = list(BehavioralPolicy.objects.filter(snapshot=snapshot))
            self._coverage_by_state(policies, state_outcomes,
                                     at_risk_validated=at_risk_validated)

        # Raw text samples (for verification)
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Raw text samples per state (verification)'
        ))
        for state in [STATE_HIGH_INTENT, STATE_BOOKING_INTENT, STATE_AT_RISK]:
            self._print_samples_for_state(state, d_hist, d_out)

        # Acceptance gate
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=== Acceptance gate ==='))
        self._acceptance_gate(state_outcomes, h_state_outcomes,
                               d_baseline, h_baseline)

    # -------------------------- helpers --------------------------

    def _state_outcome_stats(self, histories, outcomes):
        """Return {state: (n_entered, n_pos, n_neg, positive_rate)}."""
        counts = defaultdict(lambda: {'pos': 0, 'neg': 0})
        for cid, h in histories.items():
            outcome = outcomes.get(cid)
            if outcome is None:
                continue
            for state in h.states_visited():
                if outcome == 'positive':
                    counts[state]['pos'] += 1
                else:
                    counts[state]['neg'] += 1
        out = {}
        for state, cnt in counts.items():
            total = cnt['pos'] + cnt['neg']
            rate = cnt['pos'] / total if total else 0.0
            out[state] = (total, cnt['pos'], cnt['neg'], rate)
        return out

    def _compare_state_vs_signals(self, all_records, discovery, d_hist, d_out,
                                    baseline):
        # For each state, compare with its best-single-signal component
        state_components = [
            (STATE_HIGH_INTENT, ['PRICE_REQUESTED', 'AVAILABILITY_REQUESTED',
                                   'DISCOUNT_REQUESTED']),
            (STATE_BOOKING_INTENT, ['BOOKING_REQUESTED']),
            (STATE_ENGAGED, ['PROPERTY_DETAILS_PROVIDED', 'QUALIFICATION_ANSWER']),
        ]
        outcomes = d_out
        for state, components in state_components:
            state_convs = {cid for cid, h in d_hist.items() if h.entered(state)}
            n_state = len(state_convs)
            n_state_pos = sum(1 for c in state_convs if outcomes[c] == 'positive')
            state_rate = n_state_pos / n_state if n_state else 0.0
            state_lift = state_rate - baseline

            self.stdout.write(f'  --- {state} ---')
            self.stdout.write(
                f'    STATE: covers n={n_state} rate={state_rate:.2%} '
                f'lift={state_lift:+.2f}'
            )
            best_signal_lift = -1.0
            best_signal = None
            best_n = 0
            for sig in components:
                sig_convs = {
                    r.conversation_id for r in discovery
                    if any(ev.event_type == sig for ev in r.events)
                }
                n_sig = len(sig_convs)
                n_sig_pos = sum(1 for c in sig_convs if outcomes[c] == 'positive')
                sig_rate = n_sig_pos / n_sig if n_sig else 0.0
                sig_lift = sig_rate - baseline
                self.stdout.write(
                    f'    signal {sig:26} n={n_sig:3} rate={sig_rate:.2%} '
                    f'lift={sig_lift:+.2f}'
                )
                if sig_lift > best_signal_lift:
                    best_signal_lift = sig_lift
                    best_signal = sig
                    best_n = n_sig
            # Verdict
            if best_signal is None:
                verdict = 'NO_COMPONENT_SUPPORT'
            elif n_state - best_n >= COVERAGE_GAIN_MIN and state_lift >= 0:
                verdict = (
                    f'AGGREGATES ({n_state - best_n} more convs than best '
                    f'component {best_signal})'
                )
            elif abs(state_lift - best_signal_lift) < 0.02 and n_state <= best_n + 1:
                verdict = (
                    f'RENAMES best_signal={best_signal} '
                    f'(state adds no coverage or lift)'
                )
            elif state_lift >= best_signal_lift and n_state >= best_n:
                verdict = f'SLIGHT_GAIN over best component {best_signal}'
            else:
                verdict = f'WEAKER than best component {best_signal}'
            self.stdout.write(f'    → verdict: {verdict}')

    def _resolve_snapshot(self, org, explicit_id):
        if explicit_id:
            try:
                return TenantConfigSnapshot.objects.get(pk=explicit_id)
            except TenantConfigSnapshot.DoesNotExist as exc:
                raise CommandError(
                    f'snapshot {explicit_id} not found'
                ) from exc
        return (TenantConfigSnapshot.objects
                .filter(org=org)
                .order_by('-created_at').first())

    def _coverage_by_state(self, policies, state_outcomes,
                            *, at_risk_validated):
        by_condition = defaultdict(list)
        for p in policies:
            by_condition[p.condition_event].append(p)
        # For each state, which signals lead into it, and does config
        # address any of those signals?
        state_map = {
            STATE_EXPLORING: EXPLORING_SIGNALS,
            STATE_ENGAGED: ENGAGED_SIGNALS,
            STATE_HIGH_INTENT: HIGH_INTENT_SIGNALS,
            STATE_BOOKING_INTENT: BOOKING_INTENT_SIGNALS,
            STATE_AT_RISK: CANDIDATE_AT_RISK_SIGNALS,
        }
        for state, signals in state_map.items():
            info = state_outcomes.get(state)
            n = info[0] if info else 0
            if state == STATE_AT_RISK and not at_risk_validated:
                addendum = ' (structural only — not validated)'
            else:
                addendum = ''
            covered = [s for s in signals if s in by_condition]
            uncovered = [s for s in signals if s not in by_condition]
            summary = 'FULL' if not uncovered else (
                'PARTIAL' if covered else 'NOT_ADDRESSED'
            )
            self.stdout.write(
                f'  {state:20}  n_entered={n:3}  coverage={summary}{addendum}'
            )
            if covered:
                self.stdout.write(f'      covered signals: {covered}')
            if uncovered:
                self.stdout.write(f'      uncovered signals: {uncovered}')

    def _print_samples_for_state(self, state, d_hist, d_out):
        entries = [(cid, d_out[cid]) for cid, h in d_hist.items()
                   if h.entered(state)]
        if not entries:
            return
        self.stdout.write(f'')
        self.stdout.write(f'  --- {state} ({len(entries)} discovery convs) ---')
        for cid, outcome in entries[:3]:
            snippets = list(
                ConversationTurn.objects
                .filter(conversation_id=cid, speaker='customer')
                .order_by('occurred_at')
                .values_list('text', flat=True)[:3]
            )
            snippets = [(t or '').strip()[:120] for t in snippets if t]
            self.stdout.write(
                f'    conv={cid[:8]} outcome={outcome}: {snippets}'
            )

    def _acceptance_gate(self, d_state_outcomes, h_state_outcomes,
                          d_baseline, h_baseline):
        # Criterion: at least one non-terminal state must reproduce
        # meaningful outcome separation on holdout.
        passed = False
        details = []
        for state in [STATE_EXPLORING, STATE_ENGAGED, STATE_HIGH_INTENT,
                       STATE_BOOKING_INTENT]:
            d = d_state_outcomes.get(state)
            h = h_state_outcomes.get(state)
            if not d or not h:
                continue
            d_lift = d[3] - d_baseline
            h_lift = h[3] - h_baseline
            if abs(d_lift) >= MATERIAL_LIFT and (
                (d_lift >= 0 and h_lift >= 0)
                or (d_lift < 0 and h_lift < 0)
            ) and h[0] >= 3:
                passed = True
                details.append(
                    f'{state}: d_lift={d_lift:+.2f} h_lift={h_lift:+.2f} '
                    f'h_n={h[0]}'
                )
        if passed:
            self.stdout.write(self.style.SUCCESS(
                'PASS: at least one non-terminal state reproduces material '
                'outcome separation on holdout:'
            ))
            for d in details:
                self.stdout.write(f'  ✓ {d}')
        else:
            self.stdout.write(self.style.WARNING(
                'NULL RESULT: no non-terminal state reproduced material '
                'outcome separation on holdout. Semantic events themselves '
                'may be the better primitive at this corpus size.'
            ))


def _rate(pos, neg):
    total = pos + neg
    return pos / total if total > 0 else 0.0
