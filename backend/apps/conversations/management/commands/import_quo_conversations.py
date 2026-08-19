"""Import Quo conversations for one tenant through Pipeline 1A.

Usage:
    python manage.py import_quo_conversations \\
        --org <uuid> [--sigcore-workspace <sigcore-workspace-uuid>] \\
        [--since 2026-01-01] [--until 2026-08-01] \\
        [--limit 200] [--dry-run] [--use-http-resolvers]

The command:
- Iterates records from the QuoAdapter (fixtures by default; Sigcore
  HTTP when SIGCORE_URL + SIGCORE_SERVICE_KEY + --sigcore-workspace
  are all set. Sigcore owns the Quo integration; BehaviorOS never holds
  Quo credentials directly).
- For each record, runs the full ConversationIngestionPipeline:
  normalize → persist → LB resolve → SF resolve → outcomes → EvidenceEvent.
- One failed conversation does NOT abort the batch — every failure is
  logged with its source_conversation_id.
- Dry-run mode runs everything up to (but not including) the DB writes
  by using an atomic-rollback wrapper.

Counters at the end match the spec's "Final counters" list.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Optional

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Organization
from apps.conversations.adapters.quo import QuoAdapter
from apps.conversations.outcomes.leadbridge import (
    HttpLeadBridgeOutcomeFetcher,
    InMemoryLeadBridgeOutcomeFetcher,
)
from apps.conversations.outcomes.serviceflow import (
    HttpServiceFlowOutcomeFetcher,
    InMemoryServiceFlowOutcomeFetcher,
)
from apps.conversations.resolvers.leadbridge import (
    HttpLeadBridgeResolver,
    InMemoryLeadBridgeResolver,
)
from apps.conversations.resolvers.serviceflow import (
    HttpServiceFlowResolver,
    InMemoryServiceFlowResolver,
)
from apps.conversations.services.ingestion import (
    ConversationIngestionPipeline,
    IngestionOutcome,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Import Quo conversations for one org via Pipeline 1A '
        '(normalize → link → outcome → EvidenceEvent).'
    )

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True, help='Organization UUID')
        parser.add_argument(
            '--sigcore-workspace', default='',
            help='Sigcore workspace UUID to pull Quo data from. Required '
                 'to switch the adapter to the Sigcore HTTP backend; '
                 'without it, fixtures are used.',
        )
        parser.add_argument('--since', help='ISO-8601 inclusive start')
        parser.add_argument('--until', help='ISO-8601 exclusive end')
        parser.add_argument('--limit', type=int, default=None,
                            help='Max conversations to process')
        parser.add_argument('--dry-run', action='store_true',
                            help='Run everything then roll back the DB writes')
        parser.add_argument(
            '--use-http-resolvers', action='store_true',
            help='Use HTTP LB/SF resolvers + outcome fetchers instead of '
                 'the empty in-memory ones. Requires LEADBRIDGE_LEARNING_URL '
                 '/ SERVICEFLOW_LEARNING_URL to be set. Endpoints are stubbed '
                 'today — see resolver docstrings.',
        )
        parser.add_argument(
            '--import-run-id', default='',
            help='Correlation ID for this import (defaults to a UUID)',
        )

    def handle(self, *args, **options):
        org = self._resolve_org(options['org'])
        since = _parse_ts(options.get('since'), label='--since')
        until = _parse_ts(options.get('until'), label='--until')
        limit = options.get('limit')
        dry_run = options.get('dry_run', False)
        use_http = options.get('use_http_resolvers', False)
        import_run_id = options.get('import_run_id') or f'import-{uuid.uuid4()}'

        self.stdout.write(self.style.NOTICE(
            f'Starting Quo ingestion — org={org.id} ({org.name}) '
            f'since={since} until={until} limit={limit} '
            f'dry_run={dry_run} run_id={import_run_id}'
        ))

        pipeline = self._build_pipeline(
            org=org, import_run_id=import_run_id, use_http=use_http,
        )
        adapter = QuoAdapter(
            sigcore_workspace_id=options.get('sigcore_workspace') or None,
        )

        counters = _Counters()

        def _run():
            for record in adapter.fetch_records(
                since=since, until=until, limit=limit,
            ):
                counters.fetched += 1
                try:
                    outcome = pipeline.ingest_record(record)
                except Exception as exc:  # noqa: BLE001 — final belt
                    logger.exception(
                        'pipeline crashed on record %s', record.get('id')
                    )
                    counters.errors += 1
                    continue
                counters.absorb(outcome)

        if dry_run:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — DB writes will be rolled back after the batch.'
            ))
            try:
                with transaction.atomic():
                    _run()
                    transaction.set_rollback(True)
            except Exception:
                logger.exception('dry-run outer transaction crashed')
        else:
            _run()

        self._print_summary(counters, dry_run=dry_run)

    # ------------------------------------------------------------------

    def _resolve_org(self, org_id: str) -> Organization:
        try:
            return Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist as exc:
            raise CommandError(f'Organization {org_id} not found') from exc

    def _build_pipeline(
        self, *, org, import_run_id: str, use_http: bool,
    ) -> ConversationIngestionPipeline:
        if use_http:
            lb_resolver = HttpLeadBridgeResolver()
            sf_resolver = HttpServiceFlowResolver()
            lb_outcomes = HttpLeadBridgeOutcomeFetcher()
            sf_outcomes = HttpServiceFlowOutcomeFetcher()
        else:
            lb_resolver = InMemoryLeadBridgeResolver()
            sf_resolver = InMemoryServiceFlowResolver()
            lb_outcomes = InMemoryLeadBridgeOutcomeFetcher()
            sf_outcomes = InMemoryServiceFlowOutcomeFetcher()

        return ConversationIngestionPipeline(
            org=org,
            lb_resolver=lb_resolver,
            sf_resolver=sf_resolver,
            lb_outcome_fetcher=lb_outcomes,
            sf_outcome_fetcher=sf_outcomes,
            import_run_id=import_run_id,
        )

    def _print_summary(self, counters: '_Counters', *, dry_run: bool):
        prefix = '[DRY RUN] ' if dry_run else ''
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'{prefix}Ingestion complete.'))
        for name, value in counters.as_dict().items():
            self.stdout.write(f'  {name:<28} {value}')


class _Counters:
    def __init__(self):
        self.fetched = 0
        self.created = 0
        self.updated = 0
        self.skipped = 0
        self.turns_created = 0
        self.turns_skipped = 0
        self.LB_matches = 0
        self.SF_matches = 0
        self.unmatched = 0
        self.outcomes_created = 0
        self.outcomes_updated = 0
        self.evidence_events_emitted = 0
        self.errors = 0

    def absorb(self, outcome: IngestionOutcome) -> None:
        if outcome.skipped:
            self.skipped += 1
            if outcome.error:
                self.errors += 1
            return

        if outcome.conversation_created:
            self.created += 1
        else:
            self.updated += 1

        self.turns_created += outcome.turns_created
        self.turns_skipped += outcome.turns_already_present

        if outcome.lb_links_created > 0:
            self.LB_matches += 1
        if outcome.sf_links_created > 0:
            self.SF_matches += 1
        if outcome.lb_links_created == 0 and outcome.sf_links_created == 0:
            self.unmatched += 1

        if outcome.outcome_snapshot_created:
            self.outcomes_created += 1
        else:
            # Snapshot exists but was not created THIS run — either the
            # rerun-within-same-second dedupe or no signal at all.
            self.outcomes_updated += 1

        if outcome.evidence_event_id:
            self.evidence_events_emitted += 1

        if outcome.error:
            self.errors += 1

    def as_dict(self) -> dict:
        return {
            'fetched':                   self.fetched,
            'created':                   self.created,
            'updated':                   self.updated,
            'skipped':                   self.skipped,
            'turns_created':             self.turns_created,
            'turns_skipped':             self.turns_skipped,
            'LB_matches':                self.LB_matches,
            'SF_matches':                self.SF_matches,
            'unmatched':                 self.unmatched,
            'outcomes_created':          self.outcomes_created,
            'outcomes_updated':          self.outcomes_updated,
            'evidence_events_emitted':   self.evidence_events_emitted,
            'errors':                    self.errors,
        }


def _parse_ts(value: Optional[str], *, label: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CommandError(f'Invalid {label}: {value!r} ({exc})') from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt
