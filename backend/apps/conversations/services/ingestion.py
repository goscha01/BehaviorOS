"""End-to-end ingestion service for one conversation.

Ties together every phase of Pipeline 1A:

    fetch (adapter yielded record)
        ↓
    normalize (source-specific)
        ↓
    persist Conversation + Turns
        ↓
    resolve LB entity → persist EntityLinks
        ↓
    resolve SF entities (using LB lead_id when available) → persist EntityLinks
        ↓
    resolve LB + SF outcomes → persist OutcomeSnapshot
        ↓
    emit EvidenceEvent via apps.context.pipeline.EvidencePipeline

Each stage failure is contained: a bad resolver run doesn't stop
outcome resolution; a failed outcome fetch doesn't stop evidence
emission (the EvidenceEvent still records the conversation exists).

Callers construct one `ConversationIngestionPipeline` per import run
(so the resolvers / fetchers are reused across records) and invoke
`ingest_record()` for each source record.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from apps.context.models import EvidenceEvent as CtxEvidenceEvent
from apps.context.pipeline.events import EvidenceEventDTO
from apps.context.pipeline.pipeline import EvidencePipeline
from apps.conversations.models import (
    Conversation,
    IngestionStatus,
    TargetSystem,
    TargetType,
)
from apps.conversations.normalization.quo import (
    QuoNormalizationError,
    normalize_quo_record,
)
from apps.conversations.outcomes.base import (
    BaseLeadBridgeOutcomeFetcher,
    BaseServiceFlowOutcomeFetcher,
)
from apps.conversations.outcomes.service import (
    OutcomeResolutionResult,
    resolve_and_persist,
)
from apps.conversations.resolvers.base import BaseResolver
from apps.conversations.services.entity_linking import persist_entity_links
from apps.conversations.services.persistence import (
    PersistResult,
    persist_normalized_conversation,
)

logger = logging.getLogger(__name__)


@dataclass
class IngestionOutcome:
    """Per-record result. Callers aggregate these into batch counters."""
    source_conversation_id: str = ''
    conversation: Optional[Conversation] = None
    conversation_created: bool = False
    turns_created: int = 0
    turns_already_present: int = 0
    lb_links_created: int = 0
    sf_links_created: int = 0
    outcome_snapshot_created: bool = False
    evidence_event_id: Optional[str] = None
    error: str = ''
    skipped: bool = False
    stages_completed: list[str] = field(default_factory=list)


class ConversationIngestionPipeline:
    """Composable orchestrator. Callers wire concrete resolvers / fetchers
    at construction time so tests can swap in-memory implementations
    without touching this class.
    """

    def __init__(
        self,
        *,
        org,
        lb_resolver: Optional[BaseResolver] = None,
        sf_resolver: Optional[BaseResolver] = None,
        lb_outcome_fetcher: Optional[BaseLeadBridgeOutcomeFetcher] = None,
        sf_outcome_fetcher: Optional[BaseServiceFlowOutcomeFetcher] = None,
        evidence_pipeline: Optional[EvidencePipeline] = None,
        import_run_id: str = '',
    ):
        self.org = org
        self.lb_resolver = lb_resolver
        self.sf_resolver = sf_resolver
        self.lb_outcome_fetcher = lb_outcome_fetcher
        self.sf_outcome_fetcher = sf_outcome_fetcher
        # `build_context=False` on the EvidencePipeline calls we make —
        # historical import has no live runtime waiting on a response.
        self.evidence_pipeline = evidence_pipeline or EvidencePipeline()
        self.import_run_id = import_run_id

    def ingest_record(self, record: Mapping) -> IngestionOutcome:
        """Ingest one raw source record end-to-end. Never raises — every
        stage catches its own errors and records them on the outcome.
        """
        outcome = IngestionOutcome()

        # Stage 1: normalize.
        try:
            normalized = normalize_quo_record(record)
            outcome.source_conversation_id = normalized.source_conversation_id
            outcome.stages_completed.append('normalize')
        except QuoNormalizationError as exc:
            outcome.source_conversation_id = str(record.get('id', ''))
            outcome.error = f'normalize: {exc}'
            outcome.skipped = True
            return outcome
        except Exception as exc:  # noqa: BLE001
            logger.exception('unexpected normalization error')
            outcome.source_conversation_id = str(record.get('id', ''))
            outcome.error = f'normalize: {type(exc).__name__}: {exc}'
            outcome.skipped = True
            return outcome

        # Stage 2: persist Conversation + Turns.
        try:
            persisted: PersistResult = persist_normalized_conversation(
                normalized, org=self.org, import_run_id=self.import_run_id,
            )
            outcome.conversation = persisted.conversation
            outcome.conversation_created = persisted.conversation_created
            outcome.turns_created = persisted.turns_created
            outcome.turns_already_present = persisted.turns_already_present
            outcome.stages_completed.append('persist')
        except Exception as exc:  # noqa: BLE001
            logger.exception('conversation persistence failed')
            outcome.error = f'persist: {type(exc).__name__}: {exc}'
            return outcome

        conv = outcome.conversation

        # Stage 3: LB entity resolution.
        lb_lead_id: Optional[str] = None
        if self.lb_resolver:
            try:
                lb_results = list(self.lb_resolver.resolve(conv))
                if lb_results:
                    link_outcome = persist_entity_links(conv, lb_results)
                    outcome.lb_links_created = link_outcome.created
                    # Pick the first LEAD-type match as the LB lead id used
                    # for downstream SF lookup.
                    for r in lb_results:
                        if r.target_type == TargetType.LEAD:
                            lb_lead_id = r.target_id
                            break
                outcome.stages_completed.append('lb_resolve')
            except Exception as exc:  # noqa: BLE001
                logger.exception('lb resolver failed for conv %s', conv.id)
                outcome.error = _append_error(outcome.error, f'lb_resolve: {exc}')

        # Stage 4: SF entity resolution.
        if self.sf_resolver:
            try:
                sf_results = list(self.sf_resolver.resolve(
                    conv, leadbridge_lead_id=lb_lead_id,
                ))
                if sf_results:
                    link_outcome = persist_entity_links(conv, sf_results)
                    outcome.sf_links_created = link_outcome.created
                outcome.stages_completed.append('sf_resolve')
            except TypeError:
                # In-memory SF resolver accepts leadbridge_lead_id kwarg,
                # BaseResolver signature does not — real HTTP subclasses
                # may not accept it either. Retry without.
                try:
                    sf_results = list(self.sf_resolver.resolve(conv))
                    if sf_results:
                        link_outcome = persist_entity_links(conv, sf_results)
                        outcome.sf_links_created = link_outcome.created
                    outcome.stages_completed.append('sf_resolve')
                except Exception as exc:  # noqa: BLE001
                    outcome.error = _append_error(
                        outcome.error, f'sf_resolve: {type(exc).__name__}',
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception('sf resolver failed for conv %s', conv.id)
                outcome.error = _append_error(outcome.error, f'sf_resolve: {exc}')

        conv.ingestion_status = IngestionStatus.LINKED
        conv.save(update_fields=['ingestion_status', 'updated_at'])

        # Stage 5: outcome resolution.
        try:
            oc_result: OutcomeResolutionResult = resolve_and_persist(
                conv,
                lb_fetcher=self.lb_outcome_fetcher,
                sf_fetcher=self.sf_outcome_fetcher,
            )
            outcome.outcome_snapshot_created = oc_result.created
            outcome.stages_completed.append('outcome')
        except Exception as exc:  # noqa: BLE001
            logger.exception('outcome resolution failed for conv %s', conv.id)
            outcome.error = _append_error(outcome.error, f'outcome: {exc}')

        conv.ingestion_status = IngestionStatus.OUTCOMES_RESOLVED
        conv.save(update_fields=['ingestion_status', 'updated_at'])

        # Stage 6: emit EvidenceEvent via the existing pipeline.
        try:
            dto = self._build_evidence_dto(conv)
            result = self.evidence_pipeline.handle_evidence_dto(
                dto, build_context=False,
            )
            if result.evidence_event:
                outcome.evidence_event_id = str(result.evidence_event.id)
            outcome.stages_completed.append('evidence_emit')
        except Exception as exc:  # noqa: BLE001
            logger.exception('evidence emission failed for conv %s', conv.id)
            outcome.error = _append_error(
                outcome.error, f'evidence_emit: {type(exc).__name__}',
            )

        conv.ingestion_status = IngestionStatus.EMITTED
        conv.save(update_fields=['ingestion_status', 'updated_at'])

        return outcome

    def _build_evidence_dto(self, conv: Conversation) -> EvidenceEventDTO:
        # Gather the current EntityLinks so downstream analyzers can see
        # who this conversation is linked to WITHOUT re-querying our DB.
        links = list(conv.entity_links.values(
            'target_system', 'target_type', 'target_id', 'match_method',
            'confidence',
        ))
        latest_snapshot = conv.outcome_snapshots.order_by('-captured_at').first()

        payload = {
            'conversation': {
                'id': str(conv.id),
                'source': conv.source,
                'source_conversation_id': conv.source_conversation_id,
                'channel': conv.channel,
                'customer_phone': conv.customer_phone,
                'started_at': conv.started_at.isoformat(),
                'ended_at': conv.ended_at.isoformat() if conv.ended_at else None,
                'turn_count': conv.turns.count(),
            },
            'entity_links': links,
            'outcome_snapshot': (
                _snapshot_dict(latest_snapshot) if latest_snapshot else None
            ),
            'provenance': {
                'import_run_id': self.import_run_id,
                'pipeline': 'conversations-1a',
            },
        }

        # Use source_conversation_id for external_id — guarantees
        # historical-import idempotency at the EvidenceEvent level too.
        external_id = f'conv:{conv.source}:{conv.source_conversation_id}'

        # Pick a customer_id: LB lead id if we have one, else phone, else empty.
        lb_lead_id = ''
        for link in links:
            if (link['target_system'] == TargetSystem.LEADBRIDGE
                    and link['target_type'] == TargetType.LEAD):
                lb_lead_id = link['target_id']
                break

        return EvidenceEventDTO(
            org=self.org,
            source_kind=CtxEvidenceEvent.SourceKind.HISTORICAL,
            runtime=conv.source,           # 'quo' — reused as source system
            channel=conv.channel,
            event_type='conversation',
            customer_id=conv.customer_phone or '',
            lead_id=lb_lead_id,
            conversation_id=str(conv.id),
            external_id=external_id,
            occurred_at=conv.started_at,
            message_excerpt=_first_turn_excerpt(conv),
            payload=payload,
        )

    def ingest_batch(self, records: Iterable[Mapping]) -> list[IngestionOutcome]:
        """Convenience wrapper — ingest each record, keep going on failures."""
        return [self.ingest_record(r) for r in records]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _append_error(existing: str, new: str) -> str:
    return f'{existing}; {new}' if existing else new


def _first_turn_excerpt(conv: Conversation) -> str:
    first_turn = conv.turns.order_by('occurred_at').first()
    if not first_turn:
        return ''
    return (first_turn.text or '')[:500]


def _snapshot_dict(snap) -> dict:
    return {
        'captured_at': snap.captured_at.isoformat(),
        'lb_status': snap.lb_status,
        'lb_engaged': snap.lb_engaged,
        'lb_booked': snap.lb_booked,
        'lb_lost': snap.lb_lost,
        'lb_cancelled': snap.lb_cancelled,
        'sf_opportunity_status': snap.sf_opportunity_status,
        'sf_booked': snap.sf_booked,
        'sf_completed': snap.sf_completed,
        'sf_cancelled': snap.sf_cancelled,
        'sf_revenue_cents': snap.sf_revenue_cents,
        'sf_recurring': snap.sf_recurring,
        'sf_job_count': snap.sf_job_count,
    }
