"""Backfill historical evidence through the same pipeline as live requests.

Usage:
    python manage.py import_historical_events \\
        --org <uuid> --source leadbridge --fixture path/to/records.json

The fixture is a JSON list of records, each:
    {
      "external_id": "lb_conv_00001",       # required — idempotency key
      "occurred_at": "2026-07-01T14:20:00Z", # required — ISO-8601
      "runtime": "leadbridge",              # optional — defaults to --source
      "channel": "sms",
      "event_type": "conversation_closed",
      "customer_id": "cust_lb_00001",
      "lead_id": "lead_42",
      "conversation_id": "lb_conv_00001",
      "message": "...",
      "payload": {...}                       # optional; falls back to whole record
    }

Reruns are safe — records match on (org, source_kind=historical, external_id).
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Organization
from apps.context.pipeline.imports import HistoricalImportService


class Command(BaseCommand):
    help = 'Import historical evidence records through the EvidencePipeline.'

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True, help='Organization UUID')
        parser.add_argument('--source', required=True,
                            help='Source system name (e.g. "leadbridge")')
        parser.add_argument('--fixture', required=True,
                            help='Path to a JSON list of records')

    def handle(self, *args, **options):
        org_id = options['org']
        source = options['source']
        fixture_path = Path(options['fixture'])

        if not fixture_path.exists():
            raise CommandError(f'Fixture not found: {fixture_path}')

        try:
            org = Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist as exc:
            raise CommandError(f'Organization {org_id} not found') from exc

        try:
            records = json.loads(fixture_path.read_text(encoding='utf-8'))
        except Exception as exc:
            raise CommandError(f'Fixture is not valid JSON: {exc}') from exc

        if not isinstance(records, list):
            raise CommandError('Fixture root must be a JSON list of records.')

        service = HistoricalImportService()
        summary = service.import_records(
            org=org, source_system=source, records=records,
        )

        self.stdout.write(self.style.SUCCESS(
            f'Imported {summary["persisted"]}/{summary["total"]} records '
            f'(skipped {summary["skipped"]}, errors {summary["errors"]}).'
        ))
