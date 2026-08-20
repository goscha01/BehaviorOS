"""Pipeline 1B-4 report: print the Spotless configuration benchmark.

Usage:
    python manage.py report_config_benchmark \\
        --snapshot <snapshot-uuid> \\
        --analysis-run <conditional-analysis-run-uuid>

Prints one block per BehavioralPolicy on the snapshot, in the format
the 1B-4 spec calls for:

    Condition: PROPERTY_DETAILS_PROVIDED
    Current Spotless configuration: [source rule + config_path]
    Prescribed behavior: A1 → A2 → A3
    Observed behavior: <per-action rate + n>
    Outcome when aligned: ...
    Outcome for alternatives: ...
    Assessment: CONFIG_SUPPORTED | CONFIG_QUESTIONABLE | EXECUTION_GAP | INSUFFICIENT_EVIDENCE
    Evidence: discovery n, holdout status, CI

Also prints an aggregate summary at the top and a diagnostic block for
the CA0001 finding (SERVICE_DETAILS_PROVIDED → FOLLOW_UP_SENT) so the
user can see whether it's a configured behavior, an execution gap, or
not addressed by config.
"""

from __future__ import annotations

from collections import defaultdict, Counter

from django.core.management.base import BaseCommand, CommandError

from apps.conversations.analysis.policy_alignment import (
    _observed_rates_for_condition,
)
from apps.conversations.models import (
    BehavioralPolicy, ConditionalActionPattern, ConditionalAnalysisRun,
    PolicyAlignmentAssessment, TenantConfigSnapshot,
)


class Command(BaseCommand):
    help = 'Print the Pipeline 1B-4 configuration-alignment benchmark report.'

    def add_arguments(self, parser):
        parser.add_argument('--snapshot', required=True)
        parser.add_argument('--analysis-run', required=True)

    def handle(self, *args, **options):
        try:
            snapshot = TenantConfigSnapshot.objects.get(pk=options['snapshot'])
        except TenantConfigSnapshot.DoesNotExist as exc:
            raise CommandError(f'snapshot not found: {options["snapshot"]}') from exc
        try:
            run = ConditionalAnalysisRun.objects.get(pk=options['analysis_run'])
        except ConditionalAnalysisRun.DoesNotExist as exc:
            raise CommandError(f'analysis_run not found: {options["analysis_run"]}') from exc

        assessments = list(
            PolicyAlignmentAssessment.objects
            .filter(snapshot=snapshot, analysis_run=run)
            .select_related('policy', 'primary_pattern')
            .order_by('policy__condition_event', 'policy__ordinal')
        )
        if not assessments:
            raise CommandError(
                f'no assessments for snapshot={snapshot.pk} × '
                f'run={run.pk}. Run benchmark_config_alignment first.'
            )

        # Index 1B-3 patterns for the observed-rates block.
        patterns_by_condition: dict[str, list[ConditionalActionPattern]] = defaultdict(list)
        for p in ConditionalActionPattern.objects.filter(analysis_run=run):
            patterns_by_condition[p.condition_event].append(p)

        # Header
        self.stdout.write(self.style.SUCCESS(
            '\n=== Pipeline 1B-4: Configuration Alignment Benchmark ===\n'
        ))
        self.stdout.write(
            f'Snapshot:      {snapshot.pk}\n'
            f'Source:        {snapshot.source_system} '
            f'tenant={snapshot.tenant_external_id} '
            f'service_group={snapshot.service_group or "(all)"}\n'
            f'Captured at:   {snapshot.created_at.isoformat()}\n'
            f'Contract ver:  {snapshot.contract_version}\n'
            f'Analysis run:  {run.pk}\n'
            f'Corpus:        {run.corpus.name}@{run.corpus.version}\n'
            f'Discovery n:   pos={run.n_discovery_positive} neg={run.n_discovery_negative}\n'
            f'Holdout n:     pos={run.n_holdout_positive} neg={run.n_holdout_negative}\n'
        )

        # Aggregate summary
        status_counts = Counter(a.alignment_status for a in assessments)
        self.stdout.write(self.style.NOTICE(
            f'Assessments:   {len(assessments)} policies benchmarked\n'
        ))
        for status, n in status_counts.most_common():
            self.stdout.write(f'  {status:30}  {n}')
        self.stdout.write('')

        # Per-policy blocks
        for a in assessments:
            self._print_block(a, patterns_by_condition.get(a.policy.condition_event, []))

        # CA0001 diagnosis
        self._print_ca0001_diagnosis(assessments, patterns_by_condition)

    def _print_block(self, a: PolicyAlignmentAssessment,
                      patterns_for_c: list[ConditionalActionPattern]) -> None:
        p = a.policy
        prescribed = ' -> '.join(p.prescribed_action_events) or '(no action)'
        cfg_path = (p.source_pointer or {}).get('config_path', '<unspecified>')
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\nCondition: {p.condition_event}'
        ))
        self.stdout.write(f'  Current LB configuration:  path={cfg_path}')
        rule_preview = (p.source_rule_text or '')[:300].replace('\n', ' ')
        self.stdout.write(f'    rule: "{rule_preview}"')
        self.stdout.write(f'  Prescribed behavior:       {prescribed}')
        self.stdout.write(f'  Channel:                   {p.channel}')
        self.stdout.write(f'  Extraction confidence:     {p.extraction_confidence:.2f}')

        # Observed distribution across all actions for this C
        rates, total = _observed_rates_for_condition(
            p.condition_event, patterns_for_c,
        )
        if total > 0:
            self.stdout.write(f'  Observed behavior (n={total} first-responses to {p.condition_event}):')
            ranked = sorted(rates.items(), key=lambda kv: -kv[1])
            for action, rate in ranked[:8]:
                marker = ' *' if action in (p.prescribed_action_events or []) else ''
                self.stdout.write(f'    {action:30}  {rate*100:5.1f}%{marker}')
        else:
            self.stdout.write(
                f'  Observed behavior:         (no observations of '
                f'{p.condition_event} in this analysis run)'
            )

        # Outcomes for primary pattern
        pp = a.primary_pattern
        if pp is not None:
            self.stdout.write(
                f'  Outcomes (primary pattern {pp.condition_event} -> '
                f'{pp.action_event}):'
            )
            self.stdout.write(
                f'    CA cell:       rate={pp.d_ca_rate:.2f} '
                f'({pp.d_ca_positive}/{pp.d_ca_positive + pp.d_ca_negative})'
            )
            self.stdout.write(
                f'    other-A cell:  rate={pp.d_co_rate:.2f} '
                f'({pp.d_co_positive}/{pp.d_co_positive + pp.d_co_negative})'
            )
            self.stdout.write(
                f'    primary effect: {pp.d_primary_effect:+.2f}  '
                f'CI[{pp.d_primary_ci_low:+.2f}, {pp.d_primary_ci_high:+.2f}]'
            )
            self.stdout.write(
                f'    overall={pp.overall_status}  '
                f'holdout={pp.holdout_status}'
            )
        self.stdout.write(f'  Assessment: {a.alignment_status}')
        self.stdout.write(f'    rationale: {a.deterministic_rationale}')
        if a.evidence_conversation_ids:
            ev = a.evidence_conversation_ids[:3]
            self.stdout.write(f'    evidence conv ids (top): {ev}')

    def _print_ca0001_diagnosis(
        self, assessments: list[PolicyAlignmentAssessment],
        patterns_by_condition: dict[str, list[ConditionalActionPattern]],
    ) -> None:
        self.stdout.write(self.style.SUCCESS(
            '\n=== CA0001 diagnostic '
            '(SERVICE_DETAILS_PROVIDED → FOLLOW_UP_SENT) ==='
        ))
        c = 'SERVICE_DETAILS_PROVIDED'
        matching_assessments = [
            a for a in assessments if a.policy.condition_event == c
        ]
        if not matching_assessments:
            self.stdout.write(
                '  No BehavioralPolicy for SERVICE_DETAILS_PROVIDED — '
                'the current LB configuration does not explicitly '
                'address this customer condition. CA0001 is neither '
                'prescribed nor prohibited by config; it is an '
                '**unaddressed behavior**.'
            )
            return
        # If we DO have policies for this condition, cross-reference:
        prescribes_follow_up = any(
            'FOLLOW_UP_SENT' in (a.policy.prescribed_action_events or [])
            for a in matching_assessments
        )
        prescribes_other = any(
            (a.policy.prescribed_action_events
             and 'FOLLOW_UP_SENT' not in a.policy.prescribed_action_events)
            for a in matching_assessments
        )
        if prescribes_follow_up:
            self.stdout.write(
                '  Config EXPLICITLY prescribes FOLLOW_UP_SENT after '
                'SERVICE_DETAILS_PROVIDED. CA0001 (-0.47 effect, '
                'holdout-reproduced) is evidence that the '
                '**configured behavior itself may need revision**.'
            )
        elif prescribes_other:
            self.stdout.write(
                '  Config prescribes something OTHER than FOLLOW_UP_SENT '
                'after SERVICE_DETAILS_PROVIDED. CA0001 represents an '
                '**execution deviation** — agents are reaching for '
                'follow-up instead of what the playbook says. See the '
                'per-policy blocks above for the prescribed alternatives.'
            )
        else:
            self.stdout.write(
                '  Config addresses SERVICE_DETAILS_PROVIDED but with '
                'no clear prescribed action list — treat as unaddressed.'
            )
