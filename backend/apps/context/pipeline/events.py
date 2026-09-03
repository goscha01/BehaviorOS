"""EvidenceEventDTO — the boundary type between callers and the pipeline.

Runtime calls and historical imports both produce these before the
pipeline touches the DB. Keeping a DTO between "inbound data" and "ORM
row" means:

- Historical import code doesn't have to duplicate ContextRequest parsing.
- Tests can construct events without a full HTTP request.
- The pipeline never has to guess about payload shape — the DTO nails
  every field down at the boundary.

The DTO is deliberately close-shaped to `EvidenceEvent`. Anything the DB
doesn't store (like `org` — a foreign key) is a normal Python attribute
here; anything the DB stores as-is passes straight through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from typing import Any

from django.utils import timezone

from apps.context.engine.base import ContextRequest
from apps.context.models import EvidenceEvent


MESSAGE_EXCERPT_MAX = 500


@dataclass
class EvidenceEventDTO:
    """One evidence event, pre-persistence.

    `org` is a resolved Organization (or None — anonymous events are
    valid, they just don't produce aggregate updates). Everything else
    is the same shape the DB stores.
    """

    org: Any  # Organization | None
    source_kind: str  # EvidenceEvent.SourceKind value
    runtime: str
    channel: str = ''
    event_type: str = ''
    customer_id: str = ''
    lead_id: str = ''
    conversation_id: str = ''
    external_id: str = ''
    occurred_at: datetime = field(default_factory=timezone.now)
    message_excerpt: str = ''
    payload: dict = field(default_factory=dict)


def evidence_from_context_request(
    request: ContextRequest,
    request_payload: dict,
    *,
    source_kind: str = EvidenceEvent.SourceKind.RUNTIME,
) -> EvidenceEventDTO:
    """Convert a live /v1/context call into an EvidenceEventDTO.

    `request_payload` is the raw request body — kept alongside the parsed
    ContextRequest so the DB row remembers exactly what the runtime sent.

    Idempotency: if the caller supplied `metadata.externalId`, that value
    is persisted as the DTO's `external_id`. Combined with `(org,
    source_kind, external_id)` uniqueness on `EvidenceEvent`, this makes
    the same caller-supplied ID a stable upsert key across retries — safe
    for historical backfills that need to rerun without duplicating.

    Namespacing: the caller is responsible for making external_id unique
    within their source. LB historical import uses "lb-hist:<conversation
    UUID>"; Callio uses its own prefix. Because the runtime string is also
    stored on the row, downstream analysis can partition by
    (runtime, external_id) if it wants stricter cross-source isolation.

    Auto-classification: when metadata.externalId is present AND the
    caller did not explicitly pass source_kind, we upgrade to HISTORICAL.
    A caller-supplied external_id on a live runtime event doesn't make
    sense — the only reason to send one is idempotent import.

    Timeline: also honors `metadata.occurredAt` (ISO-8601 string) so
    historical events land on the timeline at the time the real-world
    interaction happened, not at ingest time. Falls back to `now()` when
    the caller doesn't provide it.
    """
    excerpt = (request.message or '')[:MESSAGE_EXCERPT_MAX]
    metadata = request.metadata or {}

    external_id = str(metadata.get('externalId') or '').strip()
    if external_id and source_kind == EvidenceEvent.SourceKind.RUNTIME:
        source_kind = EvidenceEvent.SourceKind.HISTORICAL

    occurred_at = _parse_iso(metadata.get('occurredAt')) if external_id else None
    if occurred_at is None:
        occurred_at = timezone.now()

    return EvidenceEventDTO(
        org=request.org,
        source_kind=source_kind,
        runtime=request.runtime or '',
        channel=request.channel or '',
        event_type=request.event_type or '',
        customer_id=request.customer_id or '',
        lead_id=request.lead_id or '',
        conversation_id=request.conversation_id or '',
        external_id=external_id,
        occurred_at=occurred_at,
        message_excerpt=excerpt,
        payload=dict(request_payload) if request_payload else {},
    )


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        # datetime.fromisoformat handles '2026-09-03T20:11:48.318Z' via
        # replace('Z', '+00:00') pre-3.11; 3.11+ handles 'Z' directly.
        s = value.strip().replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
