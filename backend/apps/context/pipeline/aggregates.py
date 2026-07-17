"""Aggregate updates driven by EvidenceEvents.

Both aggregate tables are "cheap, rebuildable, never authoritative" —
so the rules are:

- Never raise. A blip here must not fail the runtime request.
- Use `F()` and `select_for_update()` where a race would produce a bad
  count. For dict counters (event_type_counts etc.) we accept the
  possibility of a rare lost update — the count is a hint, not a ledger.
- Skip aggregate updates for anonymous events (no `customer_id`). We
  still write `OrgStatistics` because that's org-wide.
- Historical imports use the same code path; the aggregate shape doesn't
  care whether the event was live or backfilled.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import F

from apps.context.models import (
    CustomerHistoryAggregate,
    EvidenceEvent,
    OrgStatistics,
)
from apps.context.pipeline.events import EvidenceEventDTO


logger = logging.getLogger(__name__)


def _bump_counter(counter_dict: dict, key: str) -> None:
    if not key:
        return
    counter_dict[key] = counter_dict.get(key, 0) + 1


def update_customer_history(dto: EvidenceEventDTO) -> None:
    """Fold one event into the customer's aggregate row.

    No-op when there's no customer_id (anonymous event) or no org
    (unknown tenant).
    """
    if not dto.customer_id or dto.org is None:
        return
    try:
        with transaction.atomic():
            agg, created = (
                CustomerHistoryAggregate.objects
                .select_for_update()
                .get_or_create(
                    org=dto.org,
                    customer_id=dto.customer_id,
                    defaults={
                        'total_events': 0,
                        'first_seen_at': dto.occurred_at,
                        'last_seen_at': dto.occurred_at,
                    },
                )
            )
            agg.total_events = F('total_events') + 1
            if agg.first_seen_at is None or dto.occurred_at < agg.first_seen_at:
                agg.first_seen_at = dto.occurred_at
            if agg.last_seen_at is None or dto.occurred_at > agg.last_seen_at:
                agg.last_seen_at = dto.occurred_at

            event_type_counts = dict(agg.event_type_counts or {})
            runtime_counts = dict(agg.runtime_counts or {})
            channel_counts = dict(agg.channel_counts or {})
            _bump_counter(event_type_counts, dto.event_type)
            _bump_counter(runtime_counts, dto.runtime)
            _bump_counter(channel_counts, dto.channel)
            agg.event_type_counts = event_type_counts
            agg.runtime_counts = runtime_counts
            agg.channel_counts = channel_counts
            agg.save()
    except Exception:
        # Aggregates are best-effort. Rebuildable from EvidenceEvents.
        logger.exception('update_customer_history failed for customer=%s', dto.customer_id)


def update_org_statistics(dto: EvidenceEventDTO) -> None:
    """Fold one event into the org-wide stats row.

    Runs even for anonymous events — the count matters even if we can't
    attribute it to a customer.
    """
    if dto.org is None:
        return
    try:
        with transaction.atomic():
            stats, _created = (
                OrgStatistics.objects
                .select_for_update()
                .get_or_create(org=dto.org)
            )
            stats.total_events = F('total_events') + 1
            if stats.last_event_at is None or dto.occurred_at > stats.last_event_at:
                stats.last_event_at = dto.occurred_at

            event_type_counts = dict(stats.event_type_counts or {})
            runtime_counts = dict(stats.runtime_counts or {})
            channel_counts = dict(stats.channel_counts or {})
            _bump_counter(event_type_counts, dto.event_type)
            _bump_counter(runtime_counts, dto.runtime)
            _bump_counter(channel_counts, dto.channel)
            stats.event_type_counts = event_type_counts
            stats.runtime_counts = runtime_counts
            stats.channel_counts = channel_counts
            stats.save()
    except Exception:
        logger.exception('update_org_statistics failed for org=%s', getattr(dto.org, 'id', None))


def persist_evidence_event(dto: EvidenceEventDTO) -> EvidenceEvent | None:
    """Insert the EvidenceEvent row. Idempotent when external_id is set.

    Returns the row (or None on failure). Errors are logged, never raised —
    a DB blip here must not fail the runtime request.
    """
    if dto.org is None:
        return None
    try:
        if dto.external_id:
            # Idempotent path for historical imports.
            row, _created = EvidenceEvent.objects.get_or_create(
                org=dto.org,
                source_kind=dto.source_kind,
                external_id=dto.external_id,
                defaults={
                    'runtime': dto.runtime,
                    'channel': dto.channel,
                    'event_type': dto.event_type,
                    'customer_id': dto.customer_id,
                    'lead_id': dto.lead_id,
                    'conversation_id': dto.conversation_id,
                    'occurred_at': dto.occurred_at,
                    'message_excerpt': dto.message_excerpt,
                    'payload': dto.payload,
                },
            )
            return row
        # Non-idempotent path — every runtime call is a new row.
        return EvidenceEvent.objects.create(
            org=dto.org,
            source_kind=dto.source_kind,
            runtime=dto.runtime,
            channel=dto.channel,
            event_type=dto.event_type,
            customer_id=dto.customer_id,
            lead_id=dto.lead_id,
            conversation_id=dto.conversation_id,
            occurred_at=dto.occurred_at,
            message_excerpt=dto.message_excerpt,
            payload=dto.payload,
        )
    except Exception:
        logger.exception('persist_evidence_event failed')
        return None
