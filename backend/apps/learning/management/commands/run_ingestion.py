"""Manually trigger evidence ingestion for one org.

Useful for verifying adapters before Celery beat is wired up:

    python manage.py run_ingestion --org <org-id>
    python manage.py run_ingestion --org <org-id> --source leadbridge
    python manage.py run_ingestion --org <org-id> --since 2026-07-01T00:00:00Z
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from apps.accounts.models import Organization
from apps.learning.adapters import get_adapter
from apps.learning.services.ingestion import EvidenceIngestionService


class Command(BaseCommand):
    help = 'Run evidence ingestion for one org, from all enabled sources or a specific source.'

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True, help='Organization UUID')
        parser.add_argument(
            '--source',
            help='Optional single source_system key (e.g. leadbridge). '
                 'Defaults to LEARNING_ENABLED_SOURCES.',
        )
        parser.add_argument(
            '--since',
            help='ISO-8601 datetime cutoff. Adapter chooses default if omitted.',
        )

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(id=options['org'])
        except Organization.DoesNotExist:
            raise CommandError(f'Organization {options["org"]} not found')

        since = None
        if options.get('since'):
            since = parse_datetime(options['since'])
            if since is None:
                raise CommandError(f'Could not parse --since={options["since"]!r}')

        service = EvidenceIngestionService(org=org)

        if options.get('source'):
            try:
                adapter = get_adapter(options['source'])
            except LookupError as exc:
                raise CommandError(str(exc))
            result = service.ingest_source(adapter, since=since)
            self._report_source(result)
            return

        run = service.ingest_all_enabled(since=since)
        for result in run.per_source.values():
            self._report_source(result)
        self.stdout.write(self.style.SUCCESS(
            f'Total: {run.total_created} created, {run.total_updated} updated'
        ))

    def _report_source(self, result):
        if result.error:
            self.stdout.write(self.style.ERROR(
                f'[{result.source_system}] FAILED — {result.error}'
            ))
            return
        self.stdout.write(
            f'[{result.source_system}] {result.created} created, {result.updated} updated'
        )
