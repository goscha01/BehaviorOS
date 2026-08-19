"""LB-anchored Pipeline 1A importer — builds a clean outcome-labeled
conversation corpus for Pipeline 1B semantic analysis.

Inverse of `import_quo_conversations`: starts from LB leads with known
outcomes, finds the matching Sigcore/Quo conversation for each, and
persists the full evidence chain.

Usage:
    python manage.py import_lb_learning_dataset \\
        --org <behavioros-org-uuid> \\
        --lb-user-id <lb-user-uuid> \\
        [--limit 100] [--since ISO] [--until ISO] \\
        [--status engaged --status booked ...] \\
        [--dry-run] [--import-run-id <id>]

Reports counters + outcome + platform distributions at the end,
covering the dataset-quality questions.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone as dt_timezone
from typing import Optional

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Organization
from apps.conversations.adapters.quo import QuoAdapter
from apps.conversations.normalization.phone import normalize_e164
from apps.conversations.services.lb_anchored_ingestion import (
    LB_ANCHOR_REASON_AMBIGUOUS,
    LB_ANCHOR_REASON_INVALID_PHONE,
    LB_ANCHOR_REASON_NO_CONVERSATION,
    LB_ANCHOR_REASON_NO_PHONE,
    LbAnchoredIngestionService,
    LbAnchoredOutcome,
)
from apps.conversations.services.lb_learning_client import LbLearningClient
from apps.conversations.services.sigcore_phone_index import (
    SigcorePhoneIndex,
    build_sigcore_phone_index,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Build the LB-anchored learning corpus (Pipeline 1A, LB-first).'

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True, help='BehaviorOS Organization UUID')
        parser.add_argument('--lb-user-id', required=True,
                            help='LB tenant key (userId UUID). '
                                 'Spotless: c3d14499-dec1-42c3-a36c-713cb09842c6')
        parser.add_argument('--limit', type=int, default=None,
                            help='Max LB leads to fetch (validation cap)')
        parser.add_argument('--since', help='ISO-8601 lower bound on lead createdAt')
        parser.add_argument('--until', help='ISO-8601 upper bound on lead createdAt (exclusive)')
        parser.add_argument('--status', action='append', default=[],
                            help='Repeatable canonical LB status filter '
                                 '(default: eligible learning set). '
                                 'Ex: --status engaged --status booked')
        parser.add_argument('--dry-run', action='store_true',
                            help='Wrap the batch in an atomic rollback')
        parser.add_argument('--import-run-id', default='',
                            help='Correlation ID (defaults to a UUID)')

    def handle(self, *args, **options):
        org = self._resolve_org(options['org'])
        lb_user_id = options['lb_user_id']
        limit = options.get('limit')
        since = _parse_ts(options.get('since'), label='--since')
        until = _parse_ts(options.get('until'), label='--until')
        dry_run = options.get('dry_run', False)
        statuses = options.get('status') or None
        import_run_id = options.get('import_run_id') or f'lb-dataset-{uuid.uuid4()}'

        self.stdout.write(self.style.NOTICE(
            f'LB-anchored ingestion — org={org.id} ({org.name}) '
            f'lb_user={lb_user_id} limit={limit} since={since} until={until} '
            f'statuses={statuses or "(default eligible set)"} '
            f'dry_run={dry_run} run_id={import_run_id}'
        ))

        # Phase A: build the Sigcore phone index (one-time cost per run).
        self.stdout.write('building sigcore phone index...')
        index = build_sigcore_phone_index()
        self.stdout.write(
            f'  index: {index.total_conversations} conversations, '
            f'{index.phones_with_conversation} unique phones, '
            f'{index.phones_with_multiple} phones with >1 conversation'
        )
        if index.total_conversations == 0:
            raise CommandError(
                'Sigcore phone index is empty — check SIGCORE_URL + SIGCORE_API_KEY'
            )

        # Phase B: fetch LB leads.
        client = LbLearningClient(lb_user_id=lb_user_id)
        self.stdout.write('fetching LB leads (cursor-paginated)...')
        leads = list(client.iter_leads(
            statuses=statuses, since=since, until=until, max_leads=limit,
        ))
        self.stdout.write(f'  fetched {len(leads)} LB leads')
        if not leads:
            raise CommandError(
                'no LB leads returned — check LEADBRIDGE_LEARNING_URL, '
                'LEADBRIDGE_LEARNING_TOKEN, and --lb-user-id'
            )

        # Phase C: pre-scan for AMBIGUOUS_MULTI_LEAD (phones with >1 lead
        # in this corpus). Detected BEFORE ingestion so we can mark
        # accurately.
        leads_by_phone: dict[str, list] = defaultdict(list)
        for lead in leads:
            phone = lead.customer_phone or lead.customer_phone_substitute
            e164 = normalize_e164(phone) if phone else None
            if e164:
                leads_by_phone[e164].append(lead.lead_id)
        ambiguous_phones = {p for p, ids in leads_by_phone.items() if len(ids) > 1}
        self.stdout.write(
            f'  ambiguous phones (>1 LB lead in corpus): {len(ambiguous_phones)}'
        )

        # Phase D: ingest.
        service = LbAnchoredIngestionService(
            org=org, phone_index=index, quo_adapter=QuoAdapter(),
            import_run_id=import_run_id, lb_user_id=lb_user_id,
        )
        counters = _Counters()

        def _run():
            for lead in leads:
                phone = lead.customer_phone or lead.customer_phone_substitute
                e164 = normalize_e164(phone) if phone else None
                is_ambig = bool(e164 and e164 in ambiguous_phones)
                try:
                    result = service.ingest_lead(lead, is_ambiguous=is_ambig)
                except Exception:  # noqa: BLE001 — final belt
                    logger.exception('lead ingestion crashed: %s', lead.lead_id)
                    counters.errors += 1
                    counters.per_reason['CRASH'] += 1
                    continue
                counters.absorb(lead, result)

        if dry_run:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — DB writes will be rolled back after the batch.'
            ))
            with transaction.atomic():
                _run()
                transaction.set_rollback(True)
        else:
            _run()

        self._print_summary(counters, index=index, dry_run=dry_run)

    def _resolve_org(self, org_id: str) -> Organization:
        try:
            return Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist as exc:
            raise CommandError(f'Organization {org_id} not found') from exc

    def _print_summary(self, c: '_Counters', *, index: SigcorePhoneIndex, dry_run: bool):
        prefix = '[DRY RUN] ' if dry_run else ''
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'{prefix}LB-anchored ingestion complete.'))
        for k, v in c.as_dict().items():
            self.stdout.write(f'  {k:<30} {v}')

        self.stdout.write('')
        self.stdout.write('outcome distribution (included records):')
        for k, v in sorted(c.by_outcome_status.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f'  {k:<20} {v}')

        self.stdout.write('')
        self.stdout.write('platform distribution (included records):')
        for k, v in sorted(c.by_platform.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f'  {k:<20} {v}')

        self.stdout.write('')
        self.stdout.write('exclusion reasons:')
        for k, v in sorted(c.per_reason.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f'  {k:<30} {v}')

        self.stdout.write('')
        self.stdout.write(
            f'sigcore index stats: {index.total_conversations} convs, '
            f'{index.phones_with_conversation} unique phones, '
            f'{index.phones_with_multiple} multi-conv phones'
        )


class _Counters:
    def __init__(self):
        self.leads_considered = 0
        self.valid_phones = 0
        self.no_phone = 0
        self.invalid_phone = 0
        self.sigcore_conversation_found = 0
        self.no_sigcore_conversation = 0
        self.unique_one_lead_matches = 0
        self.ambiguous_multi_lead = 0
        self.included_learning_records = 0
        self.evidence_events_emitted = 0
        self.errors = 0

        self.by_outcome_status: Counter = Counter()
        self.by_platform: Counter = Counter()
        self.per_reason: Counter = Counter()

    def absorb(self, lead, result: LbAnchoredOutcome) -> None:
        self.leads_considered += 1

        if result.reason == LB_ANCHOR_REASON_NO_PHONE:
            self.no_phone += 1
            self.per_reason['NO_PHONE'] += 1
            return
        if result.reason == LB_ANCHOR_REASON_INVALID_PHONE:
            self.invalid_phone += 1
            self.per_reason['INVALID_PHONE'] += 1
            return

        self.valid_phones += 1

        if result.reason == LB_ANCHOR_REASON_NO_CONVERSATION:
            self.no_sigcore_conversation += 1
            self.per_reason['NO_CONVERSATION'] += 1
            return
        if result.reason == LB_ANCHOR_REASON_AMBIGUOUS:
            self.ambiguous_multi_lead += 1
            self.per_reason['AMBIGUOUS_MULTI_LEAD'] += 1
            return

        # A Sigcore conversation was found (either included or errored during hydration).
        self.sigcore_conversation_found += 1

        if not result.included:
            self.errors += 1
            self.per_reason[result.reason or 'OTHER'] += 1
            return

        self.unique_one_lead_matches += 1
        self.included_learning_records += 1
        if result.evidence_event_id:
            self.evidence_events_emitted += 1

        self.by_outcome_status[lead.outcome.status] += 1
        self.by_platform[lead.platform or '(none)'] += 1

    def as_dict(self) -> dict:
        return {
            'leads_considered':          self.leads_considered,
            'valid_phones':              self.valid_phones,
            'no_phone':                  self.no_phone,
            'invalid_phone':             self.invalid_phone,
            'sigcore_conversation_found': self.sigcore_conversation_found,
            'no_sigcore_conversation':   self.no_sigcore_conversation,
            'ambiguous_multi_lead':      self.ambiguous_multi_lead,
            'unique_one_lead_matches':   self.unique_one_lead_matches,
            'included_learning_records': self.included_learning_records,
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
