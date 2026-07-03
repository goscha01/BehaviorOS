"""Manually run evidence analysis for one org.

Usage:
    python manage.py run_analysis --org <org-id>
    python manage.py run_analysis --org <org-id> --limit 20
    python manage.py run_analysis --org <org-id> --model claude-sonnet-4-6
    python manage.py run_analysis --org <org-id> --job <job-id>

Creates a fresh LearningJob if --job isn't given. Useful for verifying
analyzer output before Celery beat is wired up.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import Organization
from apps.learning.models import LearningJob
from apps.learning.services.analysis import EvidenceAnalysisService


class Command(BaseCommand):
    help = 'Run per-evidence analysis for one org, budget-limited via LEARNING_JOB_MAX_USD.'

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True, help='Organization UUID')
        parser.add_argument('--job', help='Reuse an existing LearningJob UUID')
        parser.add_argument(
            '--limit', type=int, help='Cap number of insights considered this run'
        )
        parser.add_argument(
            '--model', help='Override LEARNING_ANALYSIS_MODEL for this run'
        )

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(id=options['org'])
        except Organization.DoesNotExist:
            raise CommandError(f'Organization {options["org"]} not found')

        if options.get('job'):
            try:
                job = LearningJob.objects.get(id=options['job'], org=org)
            except LearningJob.DoesNotExist:
                raise CommandError(f'LearningJob {options["job"]} not found for this org')
        else:
            job = LearningJob.objects.create(
                org=org,
                status=LearningJob.Status.RUNNING,
                triggered_by=LearningJob.TriggeredBy.MANUAL,
                started_at=timezone.now(),
            )
            self.stdout.write(f'Created LearningJob {job.pk}')

        service = EvidenceAnalysisService(
            org=org, job=job, model=options.get('model') or None
        )
        result = service.analyze_unanalyzed(limit=options.get('limit'))

        LearningJob.objects.filter(pk=job.pk).update(
            evidence_processed=result.analyzed,
            evidence_skipped=result.skipped,
            completed_at=timezone.now(),
            status=(
                LearningJob.Status.PARTIAL if result.stopped_for_budget
                else LearningJob.Status.COMPLETED
            ),
        )

        style = self.style.WARNING if result.stopped_for_budget else self.style.SUCCESS
        self.stdout.write(style(
            f'analyzed={result.analyzed} '
            f'skipped={result.skipped} '
            f'failed={result.failed} '
            f'cost=${result.total_cost_usd} '
            f'stopped_for_budget={result.stopped_for_budget}'
        ))
