"""POST /v1/context — the single endpoint (Phase 1 / 2 / 3 / 4).

Phase 4 addition: `mode` field. `lookup` (default) preserves the existing
Phase-1 contract byte-for-byte. `report` runs the same ingestion pipeline
(evidence persisted, aggregates updated, hooks fire) but skips context
build — used by runtimes to feed back post-call outcomes through the
same authenticated, telemetered door.

Also new in Phase 4: `contextRequestId` correlation ID. Callers may
supply their own; when absent the view generates one. Every response
body — lookup or report, `no_context` or `context` or `reported` —
echoes it back so runtime logs, ContextRequestLog rows, evidence events,
and downstream reports can be joined without reverse-engineering.

Contract:
- Always returns a valid response. Never errors because "no context exists."
- Bodies:
    lookup: { "status": "no_context", "contextRequestId": "..." }
    lookup: { "status": "context", "confidence": 0.74, "context": {...},
              "contextRequestId": "..." }
    report: { "status": "reported", "contextRequestId": "...",
              "evidenceEventId": "..." | null }
- Shadow mode: when BEHAVIOR_CONTEXT_ENABLED is False, sources run + logs
  persist but the runtime sees `no_context` regardless. Report mode is
  unaffected — reports are ingestion, not response gating.
- No LLM. No inference. Sources read from existing evidence only.
"""

from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Organization
from apps.context.auth import ServiceTokenAuthentication
from apps.context.engine import ContextRequest, log_context_request
from apps.context.pipeline import EvidencePipeline
from apps.context.pipeline.events import evidence_from_context_request
from apps.context.serializers import (
    MODE_LOOKUP,
    MODE_REPORT,
    ContextRequestSerializer,
)


logger = logging.getLogger(__name__)


def _new_context_request_id() -> str:
    """Server-side correlation ID. Prefixed for grep-ability."""
    return 'ctx_' + uuid.uuid4().hex[:24]


def _resolve_org(tenant_id: str):
    """Map a runtime's tenantId to an Organization.

    Phase 1 convention: tenantId is the Organization.id (UUID) as a string.
    A missing / unknown tenant DOES NOT raise — it just means no evidence
    will match and the response naturally becomes `no_context`.
    """
    if not tenant_id:
        return None
    try:
        return Organization.objects.filter(pk=tenant_id).first()
    except (ValueError, TypeError, ValidationError):
        # Non-UUID string — no match, but the endpoint still succeeds.
        return None


class ContextView(APIView):
    """POST /v1/context — the Context Engine entrypoint.

    Response invariants:
    - 200 with `{"status": "no_context"}` OR `{"status": "context", ...}`.
    - 400 only if the body itself is unparseable (missing tenantId / runtime).
    - 401 only if the service token is misconfigured on the caller side.
    - Never 500 for a Source bug — the merger swallows exceptions per Source.
    """

    authentication_classes = [ServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ContextRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Correlation ID: caller-supplied wins so distributed traces (from
        # e.g. Callio's per-call request id) survive the hop. Server
        # generates one when absent so every response is joinable.
        context_request_id = data.get('contextRequestId') or _new_context_request_id()

        # Persist the correlation ID in the raw body so it lands in
        # ContextRequestLog.request_payload without a schema change. The
        # migration bootstrap on this repo makes adding a real column
        # riskier than it needs to be for a first-integration lands.
        raw_body = dict(request.data) if hasattr(request.data, 'items') else {}
        raw_body.setdefault('contextRequestId', context_request_id)

        org = _resolve_org(data['tenantId'])
        ctx_request = ContextRequest(
            tenant_id=data['tenantId'],
            runtime=data['runtime'],
            channel=data.get('channel', ''),
            event_type=data.get('eventType', ''),
            customer_id=data.get('customerId', ''),
            lead_id=data.get('leadId', ''),
            conversation_id=data.get('conversationId', ''),
            message=data.get('message', ''),
            metadata=data.get('metadata', {}),
            org=org,
        )
        # Correlation ID also lives in metadata so Sources + learning hooks
        # can read it if they ever want to link back.
        ctx_request.metadata.setdefault('contextRequestId', context_request_id)

        mode = data.get('mode') or MODE_LOOKUP
        if mode == MODE_REPORT:
            return self._handle_report(ctx_request, raw_body, context_request_id)
        return self._handle_lookup(ctx_request, raw_body, context_request_id)

    # -- lookup ---------------------------------------------------------

    def _handle_lookup(self, ctx_request, raw_body, context_request_id):
        pipeline = EvidencePipeline()
        try:
            pipeline_result = pipeline.handle_runtime_request(ctx_request, raw_body)
        except Exception:
            # Defense in depth — pipeline stages are already defensive,
            # but if a future edit lets an exception slip we still respond
            # cleanly.
            logger.exception('EvidencePipeline.handle_runtime_request failed')
            return Response(
                {'status': 'no_context', 'contextRequestId': context_request_id},
                status=http_status.HTTP_200_OK,
            )

        result = pipeline_result.engine_result
        if result is None:
            return Response(
                {'status': 'no_context', 'contextRequestId': context_request_id},
                status=http_status.HTTP_200_OK,
            )

        enabled = getattr(settings, 'BEHAVIOR_CONTEXT_ENABLED', False)
        returned_to_runtime = enabled and result.status == 'context'

        log_context_request(
            request=ctx_request,
            result=result,
            request_payload=raw_body,
            returned_to_runtime=returned_to_runtime,
        )

        logger.info(
            'context-engine mode=lookup runtime=%s tenant=%s event=%s status=%s '
            'confidence=%.3f latency_ms=%d version=%s sources=%d shadow=%s '
            'evidence_persisted=%s context_request_id=%s',
            ctx_request.runtime, ctx_request.tenant_id, ctx_request.event_type,
            result.status, result.confidence, result.latency_ms,
            result.context_version, result.source_count,
            'yes' if not enabled else 'no',
            'yes' if pipeline_result.persisted else 'no',
            context_request_id,
        )

        if not enabled or result.status == 'no_context':
            return Response(
                {'status': 'no_context', 'contextRequestId': context_request_id},
                status=http_status.HTTP_200_OK,
            )

        return Response(
            {
                'status': 'context',
                'confidence': result.confidence,
                'context': result.wire_context,
                'contextRequestId': context_request_id,
                'contextVersion': result.context_version,
            },
            status=http_status.HTTP_200_OK,
        )

    # -- report ---------------------------------------------------------

    def _handle_report(self, ctx_request, raw_body, context_request_id):
        # Reports share the same ingestion pipeline as lookups — evidence
        # persisted, aggregates updated, hooks fired — but skip the context
        # build. Same auth, telemetry, rate limits.
        pipeline = EvidencePipeline()
        dto = evidence_from_context_request(ctx_request, raw_body)
        try:
            pipeline_result = pipeline.handle_evidence_dto(
                dto, request=ctx_request, build_context=False,
            )
        except Exception:
            logger.exception('EvidencePipeline (report) failed')
            return Response(
                {
                    'status': 'reported',
                    'contextRequestId': context_request_id,
                    'evidenceEventId': None,
                },
                status=http_status.HTTP_200_OK,
            )

        evidence_event_id = None
        if pipeline_result.evidence_event is not None:
            evidence_event_id = str(pipeline_result.evidence_event.id)

        logger.info(
            'context-engine mode=report runtime=%s tenant=%s event=%s '
            'persisted=%s evidence_event=%s context_request_id=%s',
            ctx_request.runtime, ctx_request.tenant_id, ctx_request.event_type,
            'yes' if pipeline_result.persisted else 'no',
            evidence_event_id or '-', context_request_id,
        )

        return Response(
            {
                'status': 'reported',
                'contextRequestId': context_request_id,
                'evidenceEventId': evidence_event_id,
            },
            status=http_status.HTTP_200_OK,
        )
