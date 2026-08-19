"""LB-anchored ingestion orchestrator (Pipeline 1A, LB-first variant).

Inverse of the Sigcore-first ConversationIngestionPipeline: given a
LeadBridge lead with a KNOWN outcome + phone, find the matching Sigcore
conversation and produce Conversation + Turns + EntityLink +
OutcomeSnapshot + EvidenceEvent.

Reuses:
  - normalize_quo_record          (unchanged Quo→normalized transform)
  - persist_normalized_conversation
  - EvidencePipeline.handle_evidence_dto
  - Conversation/ConversationTurn/EntityLink/OutcomeSnapshot models

Skips the HTTP resolver + outcome-fetcher entirely — the LB anchor
already carries all identity + outcome data.

Failure modes carry structured labels (per Phase 5 of the task spec):
  NO_PHONE            — LB lead had no customer_phone or _substitute
  INVALID_PHONE       — phone didn't normalize to E.164
  NO_CONVERSATION     — no matching Sigcore conversation for the phone
  AMBIGUOUS_MULTI_LEAD — this phone has >1 LB lead in the same corpus
                        (detected by caller before invoking)
  QUO_NORMALIZE_ERROR — Sigcore returned unusable data
  PIPELINE_ERROR      — anything else (logged, non-fatal)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from django.utils import timezone

from apps.context.models import EvidenceEvent as CtxEvidenceEvent
from apps.context.pipeline.events import EvidenceEventDTO
from apps.context.pipeline.pipeline import EvidencePipeline
from apps.conversations.adapters.quo import QuoAdapter, _transform_sigcore_message
from apps.conversations.models import (
    Conversation,
    EntityLink,
    IngestionStatus,
    MatchMethod,
    OutcomeSnapshot,
    TargetSystem,
    TargetType,
)
from apps.conversations.normalization.phone import normalize_e164
from apps.conversations.normalization.quo import (
    QuoNormalizationError,
    normalize_quo_record,
)
from apps.conversations.services.lb_learning_client import LearningLead
from apps.conversations.services.persistence import persist_normalized_conversation
from apps.conversations.services.sigcore_phone_index import SigcorePhoneIndex

logger = logging.getLogger(__name__)


LB_ANCHOR_REASON_NO_PHONE = 'NO_PHONE'
LB_ANCHOR_REASON_INVALID_PHONE = 'INVALID_PHONE'
LB_ANCHOR_REASON_NO_CONVERSATION = 'NO_CONVERSATION'
LB_ANCHOR_REASON_AMBIGUOUS = 'AMBIGUOUS_MULTI_LEAD'
LB_ANCHOR_REASON_NORMALIZE_ERROR = 'QUO_NORMALIZE_ERROR'
LB_ANCHOR_REASON_PIPELINE_ERROR = 'PIPELINE_ERROR'


@dataclass
class LbAnchoredOutcome:
    lb_lead_id: str
    normalized_phone: str = ''
    conversation_id: Optional[str] = None
    conversation_created: bool = False
    turns_created: int = 0
    turns_already_present: int = 0
    entity_link_created: bool = False
    outcome_snapshot_created: bool = False
    evidence_event_id: Optional[str] = None
    included: bool = False
    reason: str = ''  # one of LB_ANCHOR_REASON_* when not included
    error: str = ''
    stages_completed: list[str] = field(default_factory=list)


class LbAnchoredIngestionService:
    """Ingest one LB lead's conversation. Callers wire the phone index +
    QuoAdapter + EvidencePipeline; this class contains the orchestration
    only (no I/O beyond what those collaborators do).
    """

    def __init__(
        self,
        *,
        org,
        phone_index: SigcorePhoneIndex,
        quo_adapter: Optional[QuoAdapter] = None,
        evidence_pipeline: Optional[EvidencePipeline] = None,
        import_run_id: str = '',
        lb_user_id: str = '',
    ):
        self.org = org
        self.phone_index = phone_index
        # QuoAdapter's HTTP backend is what turns a Sigcore conversation
        # summary into a full envelope (messages + calls + transcripts).
        # We only use its `_build_envelope` internals here — cheap access
        # to per-conversation hydration without duplicating the pagination
        # logic.
        self.quo_adapter = quo_adapter or QuoAdapter()
        self.evidence_pipeline = evidence_pipeline or EvidencePipeline()
        self.import_run_id = import_run_id
        self.lb_user_id = lb_user_id

    def ingest_lead(
        self, lead: LearningLead, *, is_ambiguous: bool = False,
    ) -> LbAnchoredOutcome:
        """Ingest one LB lead. `is_ambiguous=True` is set by the caller
        when this phone has multiple LB leads in the current corpus —
        we still record the LB EntityLink + OutcomeSnapshot but mark the
        row so downstream analysis can exclude it."""
        outcome = LbAnchoredOutcome(lb_lead_id=lead.lead_id)

        # Phone: try customer_phone, fall back to substitute.
        raw_phone = lead.customer_phone or lead.customer_phone_substitute
        if not raw_phone:
            outcome.reason = LB_ANCHOR_REASON_NO_PHONE
            return outcome
        e164 = normalize_e164(raw_phone)
        if not e164:
            outcome.reason = LB_ANCHOR_REASON_INVALID_PHONE
            return outcome
        outcome.normalized_phone = e164

        # Ambiguity check — caller already grouped leads by phone.
        if is_ambiguous:
            outcome.reason = LB_ANCHOR_REASON_AMBIGUOUS
            return outcome

        # Sigcore lookup via phone index.
        sig_summaries = self.phone_index.lookup(e164)
        if not sig_summaries:
            outcome.reason = LB_ANCHOR_REASON_NO_CONVERSATION
            return outcome
        # If Sigcore has multiple conversations for the same phone, take
        # the FIRST — the phone index preserves API return order (recency
        # descending). Rare (audit found 0/25). Recorded in metadata.
        sig_summary = sig_summaries[0]

        # Hydrate the envelope. Reuses QuoAdapter._build_envelope so
        # message pagination, call fetching, and transcript-flag gating
        # all work identically to the Sigcore-first path.
        try:
            envelope = self._build_envelope(sig_summary)
            outcome.stages_completed.append('sigcore_hydrate')
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                'lb-anchored: hydrate failed for lead=%s sig_conv=%s',
                lead.lead_id, sig_summary.get('id'),
            )
            outcome.reason = LB_ANCHOR_REASON_PIPELINE_ERROR
            outcome.error = f'hydrate: {type(exc).__name__}'
            return outcome
        if envelope is None:
            outcome.reason = LB_ANCHOR_REASON_PIPELINE_ERROR
            outcome.error = 'envelope was None'
            return outcome

        # Normalize.
        try:
            normalized = normalize_quo_record(envelope)
            outcome.stages_completed.append('normalize')
        except QuoNormalizationError as exc:
            outcome.reason = LB_ANCHOR_REASON_NORMALIZE_ERROR
            outcome.error = str(exc)
            return outcome

        # Persist Conversation + Turns.
        try:
            persisted = persist_normalized_conversation(
                normalized, org=self.org, import_run_id=self.import_run_id,
            )
            outcome.stages_completed.append('persist')
        except Exception as exc:  # noqa: BLE001
            logger.exception('lb-anchored: persist failed for lead=%s', lead.lead_id)
            outcome.reason = LB_ANCHOR_REASON_PIPELINE_ERROR
            outcome.error = f'persist: {type(exc).__name__}'
            return outcome

        conv = persisted.conversation
        outcome.conversation_id = str(conv.id)
        outcome.conversation_created = persisted.conversation_created
        outcome.turns_created = persisted.turns_created
        outcome.turns_already_present = persisted.turns_already_present

        # EntityLink to LB lead — deterministic, phone_exact, provenance
        # tagged as lb_anchored so downstream can distinguish this path
        # from the Sigcore-first pipeline's inferred matches.
        link, link_created = EntityLink.objects.get_or_create(
            conversation=conv,
            target_system=TargetSystem.LEADBRIDGE,
            target_type=TargetType.LEAD,
            target_id=lead.lead_id,
            match_method=MatchMethod.PHONE_EXACT,
            defaults={
                'confidence': 1.0,
                'metadata': {
                    'source': 'lb_anchored',
                    'import_run_id': self.import_run_id,
                    'lb_platform': lead.platform,
                    'lb_external_request_id': lead.external_request_id,
                    'lb_user_id': lead.user_id,
                    'lb_business_id': lead.business_id,
                    'matched_value': e164,
                    'sigcore_multi_conv_count': len(sig_summaries),
                },
            },
        )
        outcome.entity_link_created = link_created
        outcome.stages_completed.append('link')

        conv.ingestion_status = IngestionStatus.LINKED
        conv.save(update_fields=['ingestion_status', 'updated_at'])

        # OutcomeSnapshot — populated directly from the anchor's outcome.
        captured_at = timezone.now().replace(microsecond=0)
        snap, snap_created = OutcomeSnapshot.objects.get_or_create(
            conversation=conv,
            captured_at=captured_at,
            defaults={
                'lb_status': lead.outcome.status,
                'lb_engaged': lead.outcome.engaged,
                'lb_booked': lead.outcome.booked,
                'lb_lost': lead.outcome.lost,
                'lb_cancelled': lead.outcome.cancelled,
                # SF fields intentionally null in this pipeline
                'source_payload': {
                    'lb_lead': dict(lead.raw),
                },
                'metadata': {
                    'source': 'lb_anchored',
                    'import_run_id': self.import_run_id,
                    'lb_lead_id': lead.lead_id,
                    'lb_platform': lead.platform,
                    'lb_status_source': 'listLeads endpoint (b69eae1b LB commit)',
                },
            },
        )
        outcome.outcome_snapshot_created = snap_created
        outcome.stages_completed.append('outcome')

        conv.ingestion_status = IngestionStatus.OUTCOMES_RESOLVED
        conv.save(update_fields=['ingestion_status', 'updated_at'])

        # EvidenceEvent via the existing pipeline (build_context=False —
        # this is a historical import, no runtime waiting).
        try:
            dto = self._build_evidence_dto(conv, lead, snap)
            result = self.evidence_pipeline.handle_evidence_dto(
                dto, build_context=False,
            )
            if result.evidence_event:
                outcome.evidence_event_id = str(result.evidence_event.id)
            outcome.stages_completed.append('evidence_emit')
        except Exception as exc:  # noqa: BLE001
            logger.exception('lb-anchored: evidence emit failed for lead=%s', lead.lead_id)
            outcome.error = f'evidence: {type(exc).__name__}'
            # Not fatal — the row is still valid learning evidence in the DB.

        conv.ingestion_status = IngestionStatus.EMITTED
        conv.save(update_fields=['ingestion_status', 'updated_at'])

        outcome.included = True
        return outcome

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_envelope(self, sig_summary: dict) -> Optional[dict]:
        """Delegate to QuoAdapter's internal envelope builder — reuses
        message pagination, call fetching, and SIGCORE_FETCH_TRANSCRIPTS
        gating without duplicating that logic.
        """
        # Build a session + headers the same way QuoAdapter does. This is
        # intentionally coupled to QuoAdapter's HTTP path — if that
        # changes, this needs to move too.
        if not self.quo_adapter._sigcore_url or not self.quo_adapter._sigcore_api_key:  # noqa: SLF001
            raise RuntimeError('QuoAdapter not configured for HTTP backend')
        import requests
        session = requests.Session()
        headers = {
            'x-api-key': self.quo_adapter._sigcore_api_key,  # noqa: SLF001
            'X-BehaviorOS-Client': 'conversations-lb-anchored',
            'Accept': 'application/json',
        }
        base = self.quo_adapter._sigcore_url.rstrip('/')  # noqa: SLF001
        return self.quo_adapter._build_envelope(session, base, headers, sig_summary)  # noqa: SLF001

    def _build_evidence_dto(
        self, conv: Conversation, lead: LearningLead, snap: OutcomeSnapshot,
    ) -> EvidenceEventDTO:
        links = list(conv.entity_links.values(
            'target_system', 'target_type', 'target_id', 'match_method', 'confidence',
        ))
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
            'outcome_snapshot': {
                'captured_at': snap.captured_at.isoformat(),
                'lb_status': snap.lb_status,
                'lb_engaged': snap.lb_engaged,
                'lb_booked': snap.lb_booked,
                'lb_lost': snap.lb_lost,
                'lb_cancelled': snap.lb_cancelled,
            },
            'lb_anchor': {
                'lb_lead_id': lead.lead_id,
                'lb_platform': lead.platform,
                'lb_external_request_id': lead.external_request_id,
                'lb_business_id': lead.business_id,
                'lb_created_at': lead.created_at,
            },
            'provenance': {
                'import_run_id': self.import_run_id,
                'pipeline': 'conversations-1a-lb-anchored',
                'lb_user_id': self.lb_user_id,
            },
        }
        # external_id must be stable across reruns — combine conv + lead
        # so the same (lead, conversation) pair always dedupes.
        external_id = (
            f'conv:{conv.source}:{conv.source_conversation_id}:lb:{lead.lead_id}'
        )
        return EvidenceEventDTO(
            org=self.org,
            source_kind=CtxEvidenceEvent.SourceKind.HISTORICAL,
            runtime=conv.source,
            channel=conv.channel,
            event_type='conversation',
            customer_id=conv.customer_phone or '',
            lead_id=lead.lead_id,
            conversation_id=str(conv.id),
            external_id=external_id,
            occurred_at=conv.started_at,
            message_excerpt=_first_turn_excerpt(conv),
            payload=payload,
        )


def _first_turn_excerpt(conv: Conversation) -> str:
    t = conv.turns.order_by('occurred_at').first()
    return (t.text or '')[:500] if t else ''


# Suppress an unused-import lint on _transform_sigcore_message — kept
# accessible in case future callers want raw sigcore→envelope transforms.
__all__ = [
    'LbAnchoredIngestionService', 'LbAnchoredOutcome',
    'LB_ANCHOR_REASON_NO_PHONE', 'LB_ANCHOR_REASON_INVALID_PHONE',
    'LB_ANCHOR_REASON_NO_CONVERSATION', 'LB_ANCHOR_REASON_AMBIGUOUS',
    'LB_ANCHOR_REASON_NORMALIZE_ERROR', 'LB_ANCHOR_REASON_PIPELINE_ERROR',
    '_transform_sigcore_message',
]
