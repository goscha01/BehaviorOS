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
from datetime import datetime
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
    ContextRequest so the DB row remembers exactly what the runtime sent
    (helpful when we later add a field to ContextRequestSerializer).
    """
    excerpt = (request.message or '')[:MESSAGE_EXCERPT_MAX]
    return EvidenceEventDTO(
        org=request.org,
        source_kind=source_kind,
        runtime=request.runtime or '',
        channel=request.channel or '',
        event_type=request.event_type or '',
        customer_id=request.customer_id or '',
        lead_id=request.lead_id or '',
        conversation_id=request.conversation_id or '',
        external_id='',  # runtime events are not idempotent by design
        occurred_at=timezone.now(),
        message_excerpt=excerpt,
        payload=dict(request_payload) if request_payload else {},
    )
