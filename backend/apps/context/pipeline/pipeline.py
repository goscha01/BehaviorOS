"""EvidencePipeline — Phase 3 orchestrator.

Every /v1/context call now flows through here. The spec:

    Receive Evidence → Persist → Update Knowledge → Run Learning Hooks
                                                → Build Context → Return

The Pipeline owns the sequence. ContextEngine owns context generation.
Aggregate helpers own aggregate updates. Learning hooks are pluggable.
None of them touch each other directly — the Pipeline threads outputs
through the flow.

Contract carried forward from earlier phases:

- Never raises. Every stage is wrapped in defensive error handling so
  the wire response is stable even when persistence or aggregates break.
- Wire contract unchanged. `EngineResult` comes back unmodified; the
  view still translates it into the same `no_context` / `context`
  response body.
- Historical imports use the same pipeline via `build_context=False` —
  they persist + aggregate + hook, but skip the context build (there's
  no runtime waiting for a response).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from apps.context.engine import ContextEngine, ContextRequest, EngineResult
from apps.context.engine.learning_hooks import notify_evidence_event
from apps.context.models import EvidenceEvent
from apps.context.pipeline.aggregates import (
    persist_evidence_event,
    update_customer_history,
    update_org_statistics,
)
from apps.context.pipeline.events import EvidenceEventDTO, evidence_from_context_request


logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """Everything the caller might want to inspect.

    The view reads `engine_result` to build the wire response. Tests +
    admin tooling read the other fields to verify the pipeline actually
    persisted + updated aggregates.
    """

    evidence_event: Optional[EvidenceEvent] = None
    persisted: bool = False
    engine_result: Optional[EngineResult] = None
    aggregate_errors: list[str] = field(default_factory=list)


class EvidencePipeline:
    """Runs the six-stage evidence flow.

    Public entry points:
    - `handle_runtime_request(request, request_payload)` — one live
      /v1/context call. Runs everything, returns a PipelineResult with
      a populated `engine_result` for the view to serialize.
    - `handle_evidence_dto(dto, *, build_context=True, engine=None)` —
      the shared internal path. Callers with an already-built DTO
      (historical import, admin tooling) go through this.
    """

    def __init__(self, engine: ContextEngine | None = None):
        self._engine = engine or ContextEngine()

    def handle_runtime_request(
        self,
        request: ContextRequest,
        request_payload: dict,
    ) -> PipelineResult:
        dto = evidence_from_context_request(request, request_payload)
        return self.handle_evidence_dto(
            dto, request=request, build_context=True,
        )

    def handle_evidence_dto(
        self,
        dto: EvidenceEventDTO,
        *,
        request: ContextRequest | None = None,
        build_context: bool = True,
    ) -> PipelineResult:
        result = PipelineResult()

        # Stage 1-2: persist. `persist_evidence_event` returns None on
        # failure and logs the error — we never propagate.
        result.evidence_event = persist_evidence_event(dto)
        result.persisted = result.evidence_event is not None

        # Stage 3: aggregate updates. Same "log, don't raise" contract.
        for name, fn in (
            ('customer_history', update_customer_history),
            ('org_statistics', update_org_statistics),
        ):
            try:
                fn(dto)
            except Exception as exc:
                # Aggregate helpers already catch inside — this is a belt
                # for any that a future edit lets slip.
                result.aggregate_errors.append(f'{name}: {type(exc).__name__}')

        # Stage 4: evidence-event learning hooks. Isolated per-hook.
        try:
            notify_evidence_event(dto, result.evidence_event)
        except Exception:
            logger.exception('notify_evidence_event failed')

        # Stage 5: context generation. Only when a runtime is waiting.
        if build_context:
            if request is None:
                # Rebuild a ContextRequest from the DTO — supports the
                # "historical import wants a context response" case
                # even though the primary use is runtime-driven.
                request = _context_request_from_dto(dto)
            try:
                result.engine_result = self._engine.build(request)
            except Exception:
                logger.exception('ContextEngine.build failed inside pipeline')
                # ContextEngine.build is itself defensive; if we still
                # land here, leave engine_result=None and let the view
                # fall through to a no_context response.

        return result


def _context_request_from_dto(dto: EvidenceEventDTO) -> ContextRequest:
    return ContextRequest(
        tenant_id=str(getattr(dto.org, 'id', '') or ''),
        runtime=dto.runtime,
        channel=dto.channel,
        event_type=dto.event_type,
        customer_id=dto.customer_id,
        lead_id=dto.lead_id,
        conversation_id=dto.conversation_id,
        message='',
        metadata={},
        org=dto.org,
    )
