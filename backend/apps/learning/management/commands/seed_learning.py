"""One-shot seed: run the full pipeline against the first org.

Chains ingestion → analysis → clustering for the single-tenant Phase 1
setup. Idempotent — each service is a no-op if there's nothing new to
process, so this is safe to re-run.

Used to populate the dashboard with fixture-backed suggestions on first
deploy, before real source system endpoints or a real Celery beat
schedule are wired up.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import Organization
from apps.learning.models import LearningJob
from apps.learning.services.nightly import run_nightly_learning_job


class Command(BaseCommand):
    help = 'Run ingest → analyze → cluster + synthesize for the first org (Phase 1 single-tenant).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--org-name',
            help='Target org by name. Defaults to the first Organization.',
        )

    def handle(self, *args, **options):
        if options.get('org_name'):
            try:
                org = Organization.objects.get(name=options['org_name'])
            except Organization.DoesNotExist:
                raise CommandError(f'Organization {options["org_name"]!r} not found')
        else:
            org = Organization.objects.first()
            if org is None:
                raise CommandError('No Organization exists. Run bootstrap_admin first.')

        self.stdout.write(f'Seeding for org={org.name} ({org.id})')
        result = run_nightly_learning_job(
            org=org,
            triggered_by=LearningJob.TriggeredBy.MANUAL,
        )

        ingestion = result.ingestion
        if ingestion is not None:
            for source, per in ingestion.per_source.items():
                if per.error:
                    self.stdout.write(self.style.ERROR(
                        f'  [ingest:{source}] ERROR — {per.error}'
                    ))
                else:
                    self.stdout.write(
                        f'  [ingest:{source}] created={per.created} updated={per.updated}'
                    )

        self.stdout.write(
            f'  [analyze] analyzed={result.evidence_analyzed} failed={result.evidence_failed}'
        )
        self.stdout.write(
            f'  [synth] suggestions_created={result.suggestions_created}'
        )
        self.stdout.write(self.style.SUCCESS(
            f'seed_learning: job={result.job_id} '
            f'cost=${result.total_cost_usd} '
            f'stopped_for_budget={result.stopped_for_budget}'
        ))
