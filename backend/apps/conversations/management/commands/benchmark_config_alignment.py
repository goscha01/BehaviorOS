"""Pipeline 1B-4: run the deterministic alignment classifier over a
(TenantConfigSnapshot × ConditionalAnalysisRun) pair.

For each BehavioralPolicy on the snapshot, look up 1B-3 conditional
patterns for the same condition and emit a PolicyAlignmentAssessment
with one of {CONFIG_SUPPORTED, CONFIG_QUESTIONABLE, EXECUTION_GAP,
INSUFFICIENT_EVIDENCE}.

Usage:
    python manage.py benchmark_config_alignment \\
        --snapshot <snapshot-uuid> \\
        --analysis-run <conditional-analysis-run-uuid>

Idempotent per (snapshot, analysis_run, policy) — re-running updates
nothing; it will fail with a unique-constraint error, which the
command handles as "already benchmarked, skipping". To re-benchmark
after code changes, delete the old PolicyAlignmentAssessment rows
first.
"""

from __future__ import annotations

from collections import defaultdict, Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from apps.conversations.analysis.policy_alignment import classify
from apps.conversations.models import (
    BehavioralPolicy, ConditionalActionPattern, ConditionalAnalysisRun,
    PolicyAlignmentAssessment, TenantConfigSnapshot,
)


class Command(BaseCommand):
    help = 'Benchmark BehavioralPolicy rows against 1B-3 conditional evidence.'

    def add_arguments(self, parser):
        parser.add_argument('--snapshot', required=True)
        parser.add_argument('--analysis-run', required=True)

    def handle(self, *args, **options):
        try:
            snapshot = TenantConfigSnapshot.objects.get(pk=options['snapshot'])
        except TenantConfigSnapshot.DoesNotExist as exc:
            raise CommandError(f'snapshot {options["snapshot"]} not found') from exc
        try:
            run = ConditionalAnalysisRun.objects.get(pk=options['analysis_run'])
        except ConditionalAnalysisRun.DoesNotExist as exc:
            raise CommandError(f'analysis_run {options["analysis_run"]} not found') from exc

        policies = list(BehavioralPolicy.objects.filter(snapshot=snapshot))
        if not policies:
            raise CommandError(
                f'snapshot {snapshot.pk} has no BehavioralPolicy rows — '
                f'run normalize_config_snapshot first.'
            )

        # Index 1B-3 conditional patterns by condition
        patterns_by_condition: dict[str, list[ConditionalActionPattern]] = defaultdict(list)
        for p in ConditionalActionPattern.objects.filter(analysis_run=run):
            patterns_by_condition[p.condition_event].append(p)

        self.stdout.write(self.style.NOTICE(
            f'Benchmark: snapshot={snapshot.pk} vs analysis_run={run.pk} '
            f'policies={len(policies)} conditions_with_evidence='
            f'{len(patterns_by_condition)}'
        ))

        status_counts: Counter = Counter()
        with transaction.atomic():
            for policy in policies:
                patterns_for_c = patterns_by_condition.get(policy.condition_event, [])
                decision = classify(policy, patterns_for_c)
                try:
                    PolicyAlignmentAssessment.objects.create(
                        snapshot=snapshot,
                        analysis_run=run,
                        policy=policy,
                        primary_pattern=decision.primary_pattern,
                        alignment_status=decision.status,
                        deterministic_rationale=decision.rationale,
                        evidence_conversation_ids=decision.evidence_conversation_ids,
                    )
                    status_counts[decision.status] += 1
                except IntegrityError:
                    # Already benchmarked (unique constraint on
                    # snapshot × run × policy). Skip.
                    status_counts['SKIPPED_EXISTING'] += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Alignment summary:'))
        for status, n in status_counts.most_common():
            self.stdout.write(f'  {status:30}  {n}')
