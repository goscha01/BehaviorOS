"""EvidenceEvent → EvidenceInsight promotion.

The context app persists every runtime call as an EvidenceEvent (cheap,
high-cardinality, never analyzed). The learning app operates on
EvidenceInsight (LLM-annotatable, clustered, referenced by Context Sources).
Nothing bridges the two by default — this service is that bridge.

Contract:
- Idempotent: reruns are safe. `EvidenceEvent.promoted_at` gates re-processing,
  and EvidenceInsight's `(source_system, external_id)` unique index catches
  any race that beats the timestamp.
- Per-event failure isolation: a bad payload doesn't stop the batch.
- Self-contained: no cross-system fetch, no LB / Callio dependency. Callers
  already sent everything we need in the report payload.
- No LLM: promotion is a mechanical translation. Analysis is a separate step
  that runs against EvidenceInsight after promotion.

Runtime → EvidenceInsight mapping:
    source_system            = event.runtime  ('callio' / 'leadbridge' / ...)
    external_id              = str(event.id)  (guarantees uniqueness)
    evidence_type            = derived from event.event_type
    occurred_at              = event.occurred_at
    outcome / outcome_meta   = event.payload.metadata.runtimeOutcome + full dict
    business_rules_version   = event.payload.metadata.versions.leadbridgeConfigVersion
    source_payload           = event.payload
    ingest_metadata          = { promoted_from_event_id, promoted_at, ... }
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Organization
from apps.context.models import EvidenceEvent
from apps.learning.adapters.dto import Evidence
from apps.learning.models import EvidenceInsight, LearningJob
from apps.learning.services.ingestion import EvidenceIngestionService

logger = logging.getLogger(__name__)


# event_type → EvidenceInsight.EvidenceType. Unknown event types fall to
# 'other' so the promotion still succeeds — the analyzer will decline to
# process 'other' but the row is preserved for future review.
_EVENT_TYPE_TO_EVIDENCE_TYPE: dict[str, str] = {
    'call_completed': EvidenceInsight.EvidenceType.CALL,
    'inbound_call': EvidenceInsight.EvidenceType.CALL,
    'outbound_call': EvidenceInsight.EvidenceType.CALL,
    'new_lead': EvidenceInsight.EvidenceType.CONVERSATION,
    'customer_reply': EvidenceInsight.EvidenceType.CONVERSATION,
}


@dataclass
class PromotionResult:
    """Outcome of one call to `promote_evidence_events`."""

    scanned: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def promoted(self) -> int:
        return self.created + self.updated


def _map_evidence_type(event_type: str) -> str:
    return _EVENT_TYPE_TO_EVIDENCE_TYPE.get(event_type, EvidenceInsight.EvidenceType.OTHER)


def _extract_outcome(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Pull the runtimeOutcome + related metadata into the Evidence.outcome slot.

    Runtimes submit reports with:
        payload.metadata.runtimeOutcome                 -> the enum
        payload.metadata.aiDecisionSummary              -> full decision context
        payload.metadata.versions.behaviorContextOutcome
        payload.metadata.versions.behaviorContextLatencyMs

    The ingestion service extracts `status` from the outcome dict and
    stores it as EvidenceInsight.outcome; the full dict lands in
    outcome_metadata. Passing `{"status": <runtimeOutcome>, ...}` keeps
    the mechanical contract intact.
    """
    metadata = payload.get('metadata') if isinstance(payload, Mapping) else None
    if not isinstance(metadata, Mapping):
        return None
    runtime_outcome = metadata.get('runtimeOutcome')
    if not runtime_outcome:
        return None
    outcome: dict[str, Any] = {'status': runtime_outcome}
    for key in ('aiDecisionSummary', 'versions', 'timings', 'durationSeconds'):
        if key in metadata:
            outcome[key] = metadata[key]
    return outcome


def _extract_business_rules_version(payload: Mapping[str, Any]) -> str:
    metadata = payload.get('metadata') if isinstance(payload, Mapping) else None
    if not isinstance(metadata, Mapping):
        return ''
    versions = metadata.get('versions')
    if not isinstance(versions, Mapping):
        return ''
    return str(versions.get('leadbridgeConfigVersion') or '')


def _evidence_from_event(event: EvidenceEvent) -> Evidence:
    """Build the ingestion-layer Evidence DTO from a stored EvidenceEvent.

    Uses str(event.id) as external_id so the EvidenceInsight uniqueness
    constraint (source_system, external_id) does the de-duplication work
    for us — the promoted_at timestamp is a fast-path shortcut, not the
    only defense.
    """
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    return Evidence(
        source_system=event.runtime,
        evidence_type=_map_evidence_type(event.event_type),
        external_id=str(event.id),
        occurred_at=event.occurred_at,
        source_payload=payload,
        outcome=_extract_outcome(payload),
        metadata={
            'promoted_from_event_id': str(event.id),
            'context_channel': event.channel,
            'context_customer_id': event.customer_id,
            'context_conversation_id': event.conversation_id,
        },
        business_rules_version=_extract_business_rules_version(payload),
    )


def promote_evidence_events(
    org: Organization,
    *,
    limit: int | None = None,
    job: LearningJob | None = None,
    since=None,
) -> PromotionResult:
    """Promote unpromoted EvidenceEvents for an org into EvidenceInsights.

    Idempotent — safe to run repeatedly. Only runtime events are promoted;
    historical imports already have adapter parity elsewhere.
    """
    result = PromotionResult()
    ingest = EvidenceIngestionService(org=org, job=job)

    qs = EvidenceEvent.objects.filter(
        org=org,
        source_kind=EvidenceEvent.SourceKind.RUNTIME,
        promoted_at__isnull=True,
    ).order_by('occurred_at')
    if since is not None:
        qs = qs.filter(occurred_at__gte=since)
    if limit is not None:
        qs = qs[:limit]

    events = list(qs)  # snapshot before we start writing promoted_at

    for event in events:
        result.scanned += 1
        try:
            evidence = _evidence_from_event(event)
        except Exception as exc:
            # Malformed row — log and move on. Do NOT set promoted_at, so
            # a future fix in `_evidence_from_event` re-picks it up.
            logger.exception('promotion: malformed EvidenceEvent %s', event.id)
            result.failed += 1
            result.errors.append(f'build_dto({event.id}): {type(exc).__name__}: {exc}')
            continue

        try:
            with transaction.atomic():
                created = ingest._upsert(evidence)  # noqa: SLF001 — deliberate reuse of the sole writer
                # Setting promoted_at inside the same atomic block guarantees
                # we never mark an event promoted without the corresponding
                # insight landing (and vice versa).
                EvidenceEvent.objects.filter(pk=event.pk).update(
                    promoted_at=timezone.now(),
                )
        except Exception as exc:
            # Persistence failure. promoted_at stays null so the next run
            # retries this event.
            logger.exception('promotion: ingest failed for EvidenceEvent %s', event.id)
            result.failed += 1
            result.errors.append(f'ingest({event.id}): {type(exc).__name__}: {exc}')
            continue

        if created:
            result.created += 1
        else:
            result.updated += 1

    logger.info(
        'promotion: org=%s scanned=%d created=%d updated=%d failed=%d',
        org.id, result.scanned, result.created, result.updated, result.failed,
    )
    return result
