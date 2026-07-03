"""Cluster unclustered candidate recommendations and synthesize suggestions.

Usage:
    python manage.py run_clustering --org <org-id>
    python manage.py run_clustering --org <org-id> --job <job-id>
    python manage.py run_clustering --org <org-id> --model claude-opus-4-7

Reads unclustered CandidateRecommendation rows for the org, merges into
active LearningSuggestions where possible, forms new clusters from the
rest, and synthesizes a LearningSuggestion per cluster (respecting
rejected signatures and the job budget).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import Organization
from apps.learning.models import LearningJob
from apps.learning.services.clustering import EvidenceClusteringService
from apps.learning.services.synthesis import SynthesisService


class Command(BaseCommand):
    help = (
        'Run the clustering + synthesis pass for one org. Groups unclustered '
        'candidates, applies rejection signatures, calls the synthesis model.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True, help='Organization UUID')
        parser.add_argument('--job', help='Reuse an existing LearningJob UUID')
        parser.add_argument(
            '--model', help='Override LEARNING_SYNTHESIS_MODEL for this run'
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

        clustering = EvidenceClusteringService(org=org)
        cluster_result = clustering.run()
        self.stdout.write(
            f'clustering: merged={cluster_result.merged} '
            f'watchlisted={cluster_result.watchlisted} '
            f'rejected_suppressed={cluster_result.rejected_suppressed} '
            f'potential_clusters={len(cluster_result.potential_clusters)}'
        )

        synthesis = SynthesisService(
            org=org, job=job, model=options.get('model') or None
        )
        synth_result = synthesis.synthesize_clusters(cluster_result.potential_clusters)

        LearningJob.objects.filter(pk=job.pk).update(
            completed_at=timezone.now(),
            status=(
                LearningJob.Status.PARTIAL if synth_result.stopped_for_budget
                else LearningJob.Status.COMPLETED
            ),
        )

        style = self.style.WARNING if synth_result.stopped_for_budget else self.style.SUCCESS
        self.stdout.write(style(
            f'synthesis: created={synth_result.created} '
            f'cost=${synth_result.total_cost_usd} '
            f'stopped_for_budget={synth_result.stopped_for_budget}'
        ))
