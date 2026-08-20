"""Pipeline 1B-5: customer-state & intent benchmark.

Usage:
    python manage.py analyze_customer_states \\
        --org <uuid> \\
        --corpus spotless_lb_quo@v2 \\
        --extractor-model gpt-4o-mini \\
        [--split-seed 42] \\
        [--config-snapshot <uuid>]         # for config coverage cross-ref
        [--material-lift 0.10]

Prints:
  - corpus baseline positive rate
  - all sufficiently-supported CUSTOMER_SIGNAL rankings (single + n-gram)
  - discovery vs holdout side-by-side
  - HIGH_INTENT / RISK_SIGNAL / INSUFFICIENT_EVIDENCE classifications
  - length-stratified consistency check
  - config coverage per supported state (from latest snapshot if not
    passed explicitly)
  - raw customer-turn text samples for the strongest findings
  - final acceptance-gate verdict + PRICE_REQUESTED reproduction check
"""

from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Organization
from apps.conversations.analysis.conditional import (
    ConditionalConfig, build_records,
)
from apps.conversations.analysis.customer_state_analysis import (
    ANALYZER_VERSION, DEFAULT_MATERIAL_LIFT, DEFAULT_MIN_SUPPORT_NGRAM,
    DEFAULT_MIN_SUPPORT_SINGLE, SignalStats, analyze,
)
from apps.conversations.models import (
    BehavioralPolicy, ConversationTurn, LearningCorpus,
    SemanticExtractionRun, TenantConfigSnapshot,
)
from apps.conversations.semantic.extractor import EXTRACTOR_VERSION
from apps.conversations.semantic.ontology import (
    CUSTOMER_SIGNAL_EVENTS, ONTOLOGY_VERSION,
)
from apps.conversations.semantic.prompt import PROMPT_VERSION


class Command(BaseCommand):
    help = 'Pipeline 1B-5 — customer-state & intent benchmark.'

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True)
        parser.add_argument('--corpus', required=True, help='name@version')
        parser.add_argument('--extractor-model', required=True)
        parser.add_argument('--split-seed', type=int, default=42)
        parser.add_argument('--config-snapshot', default='',
                            help='Optional TenantConfigSnapshot UUID for '
                                 'coverage analysis. Defaults to most recent '
                                 'snapshot for this tenant.')
        parser.add_argument('--material-lift', type=float,
                            default=DEFAULT_MATERIAL_LIFT)
        parser.add_argument('--min-support-single', type=int,
                            default=DEFAULT_MIN_SUPPORT_SINGLE)
        parser.add_argument('--min-support-ngram', type=int,
                            default=DEFAULT_MIN_SUPPORT_NGRAM)

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(pk=options['org'])
        except Organization.DoesNotExist as exc:
            raise CommandError(f'Organization {options["org"]} not found') from exc

        try:
            name, version = options['corpus'].split('@', 1)
        except ValueError as exc:
            raise CommandError('--corpus must be name@version') from exc
        try:
            corpus = LearningCorpus.objects.get(
                org=org, name=name, version=version,
            )
        except LearningCorpus.DoesNotExist as exc:
            raise CommandError(f'Corpus {name}@{version} not found') from exc
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
                f'No extraction run found for {name}@{version} '
                f'{EXTRACTOR_VERSION}/{ONTOLOGY_VERSION}/{PROMPT_VERSION}/'
                f'{options["extractor_model"]}'
            ) from exc

        self.stdout.write(self.style.NOTICE(
            f'Analyzer {ANALYZER_VERSION} '
            f'corpus={name}@{version} extraction={extraction_run.pk} '
            f'seed={options["split_seed"]} '
            f'material_lift={options["material_lift"]}'
        ))

        config = ConditionalConfig()
        records, load_meta = build_records(
            corpus, extraction_run, config=config,
        )
        self.stdout.write(
            f'  records: {load_meta["n_included"]}  '
            f'(excluded: lead_mismatch={load_meta["n_excluded_lead_mismatch"]} '
            f'status_outside_binary={load_meta["n_excluded_status"]})'
        )
        if len(records) < 20:
            raise CommandError(
                f'insufficient records: n={len(records)}. Need >= 20.'
            )

        result = analyze(
            records,
            split_seed=options['split_seed'],
            material_lift=options['material_lift'],
            min_support_single=options['min_support_single'],
            min_support_ngram=options['min_support_ngram'],
        )

        # ---------------- report ----------------
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            '=== Pipeline 1B-5: Customer State & Intent Benchmark ==='
        ))
        self.stdout.write(
            f'corpus baseline positive rate: {result.baseline_pos_rate:.2%}'
        )
        self.stdout.write(
            f'discovery: pos={result.n_discovery_positive} neg={result.n_discovery_negative}   '
            f'holdout: pos={result.n_holdout_positive} neg={result.n_holdout_negative}'
        )

        # --------- singles ----------
        singles_ranked = sorted(result.singles, key=lambda s: -abs(s.d_lift))
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'Single customer signals ranked by |lift| '
            f'(supported: n_present >= {options["min_support_single"]})'
        ))
        self._print_stats_table(singles_ranked)

        # --------- n-grams ----------
        ngrams_ranked = sorted(result.ngrams, key=lambda s: -abs(s.d_lift))
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'Ordered customer-signal n-grams ranked by |lift| '
            f'(supported: n_present >= {options["min_support_ngram"]})'
        ))
        self._print_stats_table(ngrams_ranked)

        # --------- classification tallies ----------
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('Classification summary'))
        for group_name, group in [('singles', result.singles), ('ngrams', result.ngrams)]:
            counts = defaultdict(int)
            for s in group:
                counts[s.classification] += 1
            self.stdout.write(f'  {group_name}: {dict(counts)}')

        # --------- length-stratified consistency ----------
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Length-stratified direction (signals with any classification other than INSUFFICIENT):'
        ))
        for s in singles_ranked + ngrams_ranked:
            if s.classification == 'INSUFFICIENT_EVIDENCE':
                continue
            self._print_length_row(s)
        if all(s.classification == 'INSUFFICIENT_EVIDENCE'
               for s in result.singles + result.ngrams):
            self.stdout.write('  (no classified signals to check)')

        # --------- config coverage ----------
        snapshot = self._resolve_snapshot(
            org, options.get('config_snapshot') or None,
        )
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'Config coverage against snapshot {snapshot.pk if snapshot else "(none available)"}'
        ))
        if snapshot is None:
            self.stdout.write('  (skipping — no snapshot found)')
        else:
            policies = list(BehavioralPolicy.objects.filter(snapshot=snapshot))
            self._print_config_coverage(singles_ranked + ngrams_ranked, policies)

        # --------- PRICE_REQUESTED hypothesis reproduction ----------
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Reproduction check: PRICE_REQUESTED as HIGH_INTENT hypothesis'
        ))
        pr = next((s for s in result.singles if s.pattern == ('PRICE_REQUESTED',)), None)
        if pr is None:
            self.stdout.write(
                '  PRICE_REQUESTED did not meet the single-signal support threshold '
                f'({options["min_support_single"]}) in the discovery set.'
            )
        else:
            self.stdout.write(
                f'  PRICE_REQUESTED single-signal: n_present={pr.d_present} '
                f'rate_given={pr.d_pos_rate_given_signal:.2%} '
                f'baseline={pr.baseline_pos_rate:.2%} '
                f'lift={pr.d_lift:+.2f} '
                f'CI[{pr.d_diff_ci_low:+.2f},{pr.d_diff_ci_high:+.2f}] '
                f'holdout={pr.holdout_status} '
                f'-> {pr.classification}'
            )

        # --------- raw text samples for strongest findings ----------
        strongest = [
            s for s in singles_ranked + ngrams_ranked
            if s.classification in ('HIGH_INTENT', 'RISK_SIGNAL')
        ]
        if strongest:
            self.stdout.write('')
            self.stdout.write(self.style.MIGRATE_HEADING(
                'Raw customer-turn samples for classified signals (verification):'
            ))
            for s in strongest[:5]:
                self._print_samples_for_signal(s, extraction_run)

        # --------- acceptance gate ----------
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=== Acceptance gate ==='))
        any_classified = any(
            s.classification in ('HIGH_INTENT', 'RISK_SIGNAL')
            for s in result.singles + result.ngrams
        )
        if any_classified:
            self.stdout.write(self.style.SUCCESS(
                'PASS: at least one classified signal / sequence reproduced '
                'on holdout with a material effect. See raw-text samples for '
                'verification; final go/no-go on building the customer-state '
                'primitive is a human judgment call.'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                'NULL RESULT: no CUSTOMER_SIGNAL or ordered progression '
                'reproduced on holdout with a material effect at the '
                'configured thresholds. Customer-state does not appear to '
                'be a strong outcome predictor at this corpus size.'
            ))

    # -------------------------- helpers --------------------------

    def _print_stats_table(self, rows: list[SignalStats]) -> None:
        if not rows:
            self.stdout.write('  (none met support threshold)')
            return
        for s in rows:
            pattern_str = ' → '.join(s.pattern)
            self.stdout.write(
                f'  [{s.classification:22}] {pattern_str}'
            )
            self.stdout.write(
                f'      n_present={s.d_present:3}  '
                f'rate_given={s.d_pos_rate_given_signal:.2%}  '
                f'rate_absent={s.d_pos_rate_given_absence:.2%}  '
                f'baseline={s.baseline_pos_rate:.2%}  '
                f'lift={s.d_lift:+.2f}'
            )
            self.stdout.write(
                f'      diff_vs_absence={s.d_diff_vs_absence:+.2f} '
                f'CI[{s.d_diff_ci_low:+.2f}, {s.d_diff_ci_high:+.2f}]  '
                f'holdout n_present={s.h_present} diff={s.h_diff_vs_absence:+.2f} status={s.holdout_status}'
            )

    def _print_length_row(self, s: SignalStats) -> None:
        pattern_str = ' → '.join(s.pattern)
        self.stdout.write(
            f'  [{s.classification:15}] {pattern_str}   '
            f'short={s.len_short_dir or "-":8} long={s.len_long_dir or "-":8}'
        )

    def _resolve_snapshot(self, org, explicit_id: str | None):
        if explicit_id:
            try:
                return TenantConfigSnapshot.objects.get(pk=explicit_id)
            except TenantConfigSnapshot.DoesNotExist as exc:
                raise CommandError(
                    f'snapshot {explicit_id} not found'
                ) from exc
        # Default to most recent for this org.
        return (
            TenantConfigSnapshot.objects
            .filter(org=org)
            .order_by('-created_at')
            .first()
        )

    def _print_config_coverage(
        self, all_stats: list[SignalStats], policies: list[BehavioralPolicy],
    ) -> None:
        # Index policies by condition_event
        by_condition: dict[str, list[BehavioralPolicy]] = defaultdict(list)
        for p in policies:
            by_condition[p.condition_event].append(p)

        # For n-grams, coverage is checked on the FIRST condition — it's
        # what current LB config would react to (config rules react per-
        # condition, not per-sequence).
        for s in all_stats:
            if s.classification == 'INSUFFICIENT_EVIDENCE':
                continue
            pattern_str = ' → '.join(s.pattern)
            head = s.pattern[0]
            matching = by_condition.get(head, [])
            if not matching:
                status = 'NOT_ADDRESSED'
                detail = f'no policy references {head}'
            else:
                actions_sets = set()
                for p in matching:
                    actions_sets.add(tuple(p.prescribed_action_events or []))
                if len(actions_sets) > 1:
                    status = 'CONFLICTING'
                    detail = (
                        f'{len(matching)} policies with different prescriptions '
                        f'for {head}: {[list(a) for a in actions_sets]}'
                    )
                elif not any(p.prescribed_action_events for p in matching):
                    status = 'EXPLICITLY_RECOGNIZED_NO_RESPONSE'
                    detail = f'{len(matching)} policy/policies with no prescribed action'
                else:
                    status = 'EXPLICITLY_RECOGNIZED_WITH_RESPONSE'
                    actions = list(actions_sets)[0]
                    detail = f'prescribed: {" → ".join(actions)}'
            self.stdout.write(
                f'  [{status:38}] {pattern_str}'
            )
            self.stdout.write(f'      → {detail}')

    def _print_samples_for_signal(
        self, s: SignalStats, extraction_run: SemanticExtractionRun,
    ) -> None:
        pattern_str = ' → '.join(s.pattern)
        self.stdout.write('')
        self.stdout.write(f'  [{s.classification}] {pattern_str}:')
        # Pick 2 positive + 2 negative evidence conversations
        for label, conv_ids in [
            ('positive', s.evidence_positive_ids[:2]),
            ('negative', s.evidence_negative_ids[:2]),
        ]:
            for cid in conv_ids:
                # Load turns for the conversation
                turn_rows = list(
                    ConversationTurn.objects
                    .filter(conversation_id=cid)
                    .order_by('occurred_at')
                    .values('speaker', 'text')
                )
                if not turn_rows:
                    continue
                # Find first customer turn matching the signal's head event
                # by looking at the extracted event's turn_start via a
                # secondary query. Simpler: just show the first 3
                # customer turns (usually where the signal fires).
                customer_snippets = [
                    (r['text'] or '').strip()[:150]
                    for r in turn_rows if r['speaker'] == 'customer'
                ][:3]
                if customer_snippets:
                    self.stdout.write(
                        f'    {label} conv={cid[:8]}: {customer_snippets}'
                    )
