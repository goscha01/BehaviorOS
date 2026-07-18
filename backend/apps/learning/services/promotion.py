"""EvidenceEvent → EvidenceInsight promotion.

The context app persists every runtime call as an EvidenceEvent (cheap,
high-cardinality, never analyzed). The learning app operates on
EvidenceInsight (LLM-annotatable, clustered, referenced by Context Sources).
Nothing bridges the two by default — this service is that bridge.

Every candidate event flows through:

    fetch pending events
        ↓
    evaluate eligibility (pure)
        ↓
    ┌── eligible ──┐    ┌── skip_* ──┐    ┌── failure ──┐
    │ persist as   │    │ mark       │    │ mark        │
    │ EvidenceIn-  │    │ SKIPPED    │    │ FAILED      │
    │ sight, mark  │    │ + reason   │    │ + reason    │
    │ PROMOTED     │    │            │    │ (retryable) │
    └──────────────┘    └────────────┘    └─────────────┘

Every event reaches a terminal promotion_status (never stays PENDING after
the pipeline has scanned it) — skipped events are RETAINED as operational
evidence, they just do NOT enter the learning corpus.

Contract:
- Idempotent: reruns are safe. `EvidenceEvent.promotion_status` gates
  re-processing, and EvidenceInsight's `(source_system, external_id)`
  unique index catches any race that beats the timestamp.
- Per-event failure isolation: a bad payload doesn't stop the batch.
- Self-contained: no cross-system fetch, no LB / Callio dependency.
- No LLM: promotion is a mechanical translation. Analysis is a separate
  step against EvidenceInsight after promotion.
- Emits three structured counters per run for Grafana:
    promotion_attempts_total
    promotion_success_total    (per source_system)
    promotion_skipped_total    (per reason)

Runtime → EvidenceInsight mapping:
    source_system            = event.runtime  ('callio' / 'leadbridge' / ...)
    external_id              = str(event.id)
    evidence_type            = derived from event.event_type
    occurred_at              = event.occurred_at
    outcome / outcome_meta   = event.payload.metadata.runtimeOutcome + full dict
    business_rules_version   = event.payload.metadata.versions.leadbridgeConfigVersion
    source_payload           = event.payload
    ingest_metadata          = { promoted_from_event_id, ... }
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Organization
from apps.context.models import EvidenceEvent
from apps.learning.adapters.dto import Evidence
from apps.learning.models import EvidenceInsight, LearningJob
from apps.learning.services.eligibility import (
    EligibilityResult,
    PromotionDecision,
    evaluate_eligibility,
)
from apps.learning.services.ingestion import EvidenceIngestionService

logger = logging.getLogger(__name__)


# event_type → EvidenceInsight.EvidenceType. Unknown falls to 'other'
# (though the eligibility gate should already have rejected via
# skip_unsupported before we get here — this is belt-and-suspenders).
_EVENT_TYPE_TO_EVIDENCE_TYPE: dict[str, str] = {
    'call_completed': EvidenceInsight.EvidenceType.CALL,
    'inbound_call': EvidenceInsight.EvidenceType.CALL,
    'outbound_call': EvidenceInsight.EvidenceType.CALL,
    'new_lead': EvidenceInsight.EvidenceType.CONVERSATION,
    'customer_reply': EvidenceInsight.EvidenceType.CONVERSATION,
}


@dataclass
class PromotionResult:
    """Outcome of one call to `promote_evidence_events`. The three
    top-line counters are what Grafana panels key off:

        promotion_attempts_total = scanned
        promotion_success_total  = created + updated
        promotion_skipped_total  = sum(skipped_by_reason.values())

    skipped_by_reason breaks the third counter down for the
    per-reason panel.
    """

    scanned: int = 0
    created: int = 0
    updated: int = 0
    failed: int = 0
    skipped_by_reason: dict[str, int] = field(default_factory=lambda: dict(Counter()))
    errors: list[str] = field(default_factory=list)

    @property
    def promoted(self) -> int:
        return self.created + self.updated

    @property
    def skipped(self) -> int:
        return sum(self.skipped_by_reason.values())

    def _record_skip(self, reason: str) -> None:
        self.skipped_by_reason[reason] = self.skipped_by_reason.get(reason, 0) + 1


def _map_evidence_type(event_type: str) -> str:
    return _EVENT_TYPE_TO_EVIDENCE_TYPE.get(event_type, EvidenceInsight.EvidenceType.OTHER)


def _extract_outcome(payload: Mapping[str, Any]) -> dict[str, Any] | None:
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


def _mark_event(
    event: EvidenceEvent,
    *,
    status: str,
    reason: str = '',
    promoted: bool = False,
) -> None:
    """Persist a terminal promotion state on the event.

    Uses an UPDATE (not save()) so we don't trigger auto_now on other
    fields and so multiple workers reading through SKIP LOCKED still
    write a single row atomically. `promoted_at` is set only on success
    — for skipped/failed events it stays null, which lets the "when did
    promotion succeed" question have a clean answer.
    """
    updates: dict[str, Any] = {
        'promotion_status': status,
        'promotion_reason': reason,
        'promotion_checked_at': timezone.now(),
    }
    if promoted:
        updates['promoted_at'] = timezone.now()
    EvidenceEvent.objects.filter(pk=event.pk).update(**updates)


def promote_evidence_events(
    org: Organization,
    *,
    limit: int | None = None,
    job: LearningJob | None = None,
    since=None,
    task_id: str | None = None,
) -> PromotionResult:
    """Promote pending EvidenceEvents for an org into EvidenceInsights.

    Idempotent — safe to run repeatedly. Only PENDING events are scanned
    (other statuses are terminal). Every scanned event reaches a terminal
    state within the call.

    **Concurrency**: uses `SELECT ... FOR UPDATE SKIP LOCKED` on the
    initial batch claim so multiple Celery workers running the same task
    in parallel never claim the same event. Whichever worker claims a
    row holds the lock until the outer transaction commits — a crashed
    worker's rows automatically revert to PENDING on rollback.

    The entire per-org batch runs in one transaction so a worker crash
    mid-batch leaves the queue in a clean state (rollback restores
    every claimed event to PENDING). Nested `atomic()` savepoints
    isolate per-event ingest failures so one bad event doesn't taint
    siblings.
    """
    result = PromotionResult()
    ingest = EvidenceIngestionService(org=org, job=job)

    # Hard ceiling in case caller passed no limit — protects against
    # worker starvation on a runaway backlog. Beat cadence + eligibility
    # filtering should keep real batches well under this.
    max_events = limit if limit is not None else 10_000

    with transaction.atomic():
        qs = (
            EvidenceEvent.objects
            .select_for_update(skip_locked=True)
            .filter(
                org=org,
                promotion_status=EvidenceEvent.PromotionStatus.PENDING,
            )
            .order_by('occurred_at')
        )
        if since is not None:
            qs = qs.filter(occurred_at__gte=since)
        events = list(qs[:max_events])

        # Process INSIDE the transaction so row locks stay held for the
        # duration of each event's processing. A worker crash rolls back
        # the whole batch (rows revert to PENDING), keeping the queue
        # consistent.
        _process_batch(events, ingest, result)

    _emit_run_summary(org, result, task_id=task_id)
    return result


def _process_batch(events, ingest, result):
    """Process a claimed batch. Called INSIDE the SKIP LOCKED transaction
    of `promote_evidence_events` — do not invoke from anywhere else."""
    for event in events:
        result.scanned += 1
        eligibility: EligibilityResult = evaluate_eligibility(event)

        if not eligibility.is_eligible:
            # Terminal SKIPPED — reason lands in a queryable column so the
            # per-reason Grafana panel doesn't need JSON introspection.
            _mark_event(
                event,
                status=EvidenceEvent.PromotionStatus.SKIPPED,
                reason=eligibility.decision,
            )
            result._record_skip(eligibility.decision)
            logger.debug(
                'promotion.skip event=%s reason=%s detail=%r',
                event.id, eligibility.decision, eligibility.detail,
            )
            continue

        # Eligible — attempt ingestion. Anything that raises here is a
        # persistence issue (constraint, connection, malformed evidence
        # DTO) — categorize as FAILED, keep going.
        try:
            evidence = _evidence_from_event(event)
        except Exception as exc:
            logger.exception('promotion.build_dto_failed event=%s', event.id)
            _mark_event(
                event,
                status=EvidenceEvent.PromotionStatus.FAILED,
                reason=f'failed:build_dto:{type(exc).__name__}',
            )
            result.failed += 1
            result.errors.append(f'build_dto({event.id}): {type(exc).__name__}: {exc}')
            continue

        try:
            with transaction.atomic():
                created = ingest._upsert(evidence)  # noqa: SLF001 — sole EvidenceInsight writer
                _mark_event(
                    event,
                    status=EvidenceEvent.PromotionStatus.PROMOTED,
                    promoted=True,
                )
        except Exception as exc:
            logger.exception('promotion.ingest_failed event=%s', event.id)
            _mark_event(
                event,
                status=EvidenceEvent.PromotionStatus.FAILED,
                reason=f'failed:ingest:{type(exc).__name__}',
            )
            result.failed += 1
            result.errors.append(f'ingest({event.id}): {type(exc).__name__}: {exc}')
            continue

        if created:
            result.created += 1
        else:
            result.updated += 1


def _emit_run_summary(org, result, *, task_id: str | None = None) -> None:
    """ONE structured summary log per run — Grafana "outcome distribution"
    panel keys off this line's fields. Order + spelling of keys is a
    dashboard contract; do not rename without updating the panel.

    task_id (when supplied by the Celery task wrapper) is emitted first so
    beat → fan-out → per-org run can be joined on one identifier.
    """
    logger.info(
        'promotion.run task_id=%s org=%s '
        'promotion_attempts_total=%d '
        'promotion_success_total=%d '
        'promotion_skipped_total=%d '
        'promotion_failed_total=%d '
        'skipped_by_reason=%s',
        task_id or '',
        org.id,
        result.scanned,
        result.promoted,
        result.skipped,
        result.failed,
        dict(result.skipped_by_reason),
    )
