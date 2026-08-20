"""BehaviorOS Insights read API + lifecycle transitions.

Endpoints (all under `/api/v1/insights/`):

    GET  runs?tenantId=<lb-user-uuid>[&limit=]
    GET  runs/<uuid>
    GET  runs/<uuid>/recommendations
    GET  recommendations/<uuid>
    POST recommendations/<uuid>/lifecycle

Tenant scoping: every read is filtered by the `tenantId` query
parameter which MUST match the `tenant_external_id` on the run's
config snapshot. There is no cross-tenant fallback — if a caller
passes a tenantId whose config snapshot isn't in the DB, the response
is empty (never other tenants' data).

Recommendations remain IMMUTABLE. Only the `RecommendationLifecycleState`
row mutates via the lifecycle POST.
"""

from __future__ import annotations

import logging

from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.conversations.api.auth import InsightsServiceTokenAuthentication
from apps.conversations.api.serializers import (
    LifecycleSerializer, LifecycleTransitionRequestSerializer,
    RecommendationRunSummarySerializer, RecommendationSerializer,
    RecommendationSummarySerializer,
)
from apps.conversations.models import (
    BehaviorRecommendation, RecommendationLifecycleState,
    RecommendationRun, TenantConfigSnapshot,
)

logger = logging.getLogger(__name__)


class _TenantScopedMixin:
    """Every insights endpoint MUST filter by tenantId. Reject calls
    that don't provide one — no silent cross-tenant behavior."""

    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get_tenant_id(self) -> str:
        tid = (self.request.query_params.get('tenantId') or '').strip()
        if not tid:
            raise ValidationError(
                {'tenantId': 'query parameter is required'}
            )
        return tid


class RecommendationRunViewSet(_TenantScopedMixin, ReadOnlyModelViewSet):
    """List + retrieve RecommendationRuns for one tenant."""

    serializer_class = RecommendationRunSummarySerializer
    lookup_field = 'pk'

    def get_queryset(self):
        tid = self.get_tenant_id()
        # Filter runs whose config snapshot points at this tenant.
        # Prefetch recommendations for the summary counters.
        return (
            RecommendationRun.objects
            .filter(config_snapshot__tenant_external_id=tid)
            .select_related('config_snapshot')
            .prefetch_related('recommendations')
            .order_by('-created_at')
        )

    @action(detail=True, url_path='recommendations',
            methods=['get'])
    def recommendations(self, request, pk=None):
        run = self.get_object()
        recs = (
            run.recommendations
            .select_related('lifecycle')
            .order_by('recommendation_id')
        )
        # Optional class filter for list narrowing
        cls = request.query_params.get('class')
        if cls:
            recs = recs.filter(rec_class=cls)
        data = RecommendationSummarySerializer(recs, many=True).data
        return Response({'run_id': str(run.pk), 'recommendations': data})


class RecommendationDetailView(_TenantScopedMixin, APIView):
    """Retrieve one recommendation by UUID (tenant-scoped via query
    parameter)."""

    def get(self, request, pk):
        tid = self.get_tenant_id()
        try:
            rec = (
                BehaviorRecommendation.objects
                .select_related('run', 'run__config_snapshot', 'lifecycle')
                .get(pk=pk)
            )
        except BehaviorRecommendation.DoesNotExist:
            raise NotFound()
        if rec.run.config_snapshot.tenant_external_id != tid:
            # Do NOT leak "exists but not yours" — 404 is safer
            raise NotFound()
        return Response(RecommendationSerializer(rec).data)


class RecommendationLifecycleView(_TenantScopedMixin, APIView):
    """POST /api/v1/insights/recommendations/<uuid>/lifecycle
    Transition a recommendation's user-lifecycle state. Never writes
    to LB configuration — this only records the tenant user's choice
    for later feedback learning.

    Payload:
        { "state": "viewed"|"accepted"|"dismissed",
          "actor": "user@example.com",       # optional
          "reason": "not_applicable",        # optional
          "note": "free-form explanation" }  # optional
    """

    def post(self, request, pk):
        tid = self.get_tenant_id()
        try:
            rec = (
                BehaviorRecommendation.objects
                .select_related('run', 'run__config_snapshot')
                .get(pk=pk)
            )
        except BehaviorRecommendation.DoesNotExist:
            raise NotFound()
        if rec.run.config_snapshot.tenant_external_id != tid:
            raise NotFound()

        serializer = LifecycleTransitionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        lifecycle, _ = RecommendationLifecycleState.objects.get_or_create(
            recommendation=rec,
        )
        lifecycle.transition_to(
            payload['state'],
            actor=payload.get('actor', ''),
            reason=payload.get('reason', ''),
            note=payload.get('note', ''),
        )
        lifecycle.save()

        return Response(
            LifecycleSerializer(lifecycle).data,
            status=http_status.HTTP_200_OK,
        )
