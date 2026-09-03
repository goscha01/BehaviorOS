"""POST /api/business-config/generate — BusinessConfigProposal endpoint.

Synchronous. LB supplies template + tenant snapshots inline; BehaviorOS does
NOT call back into LB for configuration. Idempotent from LB's perspective —
same inputs produce a fresh proposal (evidence corpus is the only shared
state, and it's mutated only via /api/context/v1/context in report mode).

Auth: same ServiceTokenAuthentication as /api/context — no new secret.
"""

from __future__ import annotations

import logging
import traceback

from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.context.auth import ServiceTokenAuthentication

from .serializers import GenerateProposalSerializer
from .services import BusinessConfigProposalSynthesizer, ProposalRequest

logger = logging.getLogger(__name__)


class GenerateProposalView(APIView):
    authentication_classes = [ServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = GenerateProposalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            req = ProposalRequest(
                tenant_id=data['tenantId'],
                template_key=data['templateKey'],
                template_id=data['templateId'],
                template_snapshot=data['templateSnapshot'],
                current_tenant_snapshot=data['currentTenantSnapshot'],
                domains=list(data['domains']),
            )
        except ValueError as exc:
            return Response(
                {'error': 'invalid_request', 'detail': str(exc)},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        synth = BusinessConfigProposalSynthesizer()
        try:
            result = synth.synthesize(req)
        except Exception as exc:  # noqa: BLE001 — this endpoint should never 500 silently
            logger.exception('business_config synth failed tenant=%s', req.tenant_id)
            return Response(
                {
                    'error': 'synthesis_failed',
                    'detail': str(exc),
                    'traceback': traceback.format_exc().splitlines()[-10:],
                },
                status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        logger.info(
            'business_config proposal generated tenant=%s template=%s evidence=%d cost=$%s',
            req.tenant_id, req.template_key, result.evidence_count, result.llm_costs_usd,
        )

        return Response(result.proposal, status=http_status.HTTP_200_OK)
