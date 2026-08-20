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
from apps.conversations.api.proposal_synthesis import (
    ProposalIneligible, generate_proposal,
)
from apps.conversations.api.serializers import (
    LifecycleSerializer, LifecycleTransitionRequestSerializer,
    MeasurementCreateRequestSerializer,
    ProposalStatusUpdateRequestSerializer,
    RecommendationOutcomeMeasurementSerializer,
    RecommendationProposalSerializer,
    RecommendationRunSummarySerializer, RecommendationSerializer,
    RecommendationSummarySerializer,
)
from apps.conversations.measurement.creation import (
    LbApplyContext, MeasurementCreationError, create_measurement,
)
from apps.conversations.models import (
    BehaviorRecommendation, RecommendationLifecycleState,
    RecommendationOutcomeMeasurement, RecommendationProposal,
    RecommendationRun, TenantConfigSnapshot,
)
from apps.learning.services.llm_client import LearningLLMClient

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


class RecommendationProposalView(_TenantScopedMixin, APIView):
    """POST → generate (or regenerate) a proposal from an accepted
    recommendation. GET → retrieve the existing proposal.

    v1 only supports STATE_COVERAGE_GAP / STATE_PARTIAL_COVERAGE recs
    with lifecycle state = ACCEPTED. Anything else returns 422 with a
    structured reason, so LB can render "not applicable" cleanly.
    """

    def _load(self, request, pk):
        tid = self.get_tenant_id()
        try:
            rec = (
                BehaviorRecommendation.objects
                .select_related('run', 'run__config_snapshot', 'lifecycle',
                                 'proposal', 'proposal__config_snapshot')
                .get(pk=pk)
            )
        except BehaviorRecommendation.DoesNotExist:
            raise NotFound()
        if rec.run.config_snapshot.tenant_external_id != tid:
            raise NotFound()
        return rec

    def get(self, request, pk):
        rec = self._load(request, pk)
        proposal = getattr(rec, 'proposal', None)
        if proposal is None:
            raise NotFound({'detail': 'no proposal generated yet'})
        return Response(RecommendationProposalSerializer(proposal).data)

    def post(self, request, pk):
        rec = self._load(request, pk)
        try:
            proposal = generate_proposal(rec, llm_client=LearningLLMClient())
        except ProposalIneligible as exc:
            return Response(
                {'detail': str(exc), 'reason': 'ineligible'},
                status=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except Exception as exc:
            logger.exception('proposal synthesis failed for rec=%s', pk)
            return Response(
                {'detail': f'proposal synthesis error: {exc!r}'},
                status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            RecommendationProposalSerializer(proposal).data,
            status=http_status.HTTP_200_OK,
        )


class RecommendationProposalStatusView(_TenantScopedMixin, APIView):
    """POST /recommendations/<id>/proposal/status — consumer reports
    the outcome of its Apply attempt. Status transitions:
      applied  → LB successfully wrote config
      stale    → LB detected drift; regenerate
      failed   → LB's write failed; error recorded

    BehaviorOS does not decide these; it only records what the
    consumer reports."""

    def post(self, request, pk):
        tid = self.get_tenant_id()
        try:
            rec = (
                BehaviorRecommendation.objects
                .select_related('run__config_snapshot', 'proposal')
                .get(pk=pk)
            )
        except BehaviorRecommendation.DoesNotExist:
            raise NotFound()
        if rec.run.config_snapshot.tenant_external_id != tid:
            raise NotFound()
        proposal = getattr(rec, 'proposal', None)
        if proposal is None:
            raise NotFound({'detail': 'no proposal to update'})
        payload = ProposalStatusUpdateRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        v = payload.validated_data
        from django.utils import timezone
        proposal.status = v['status']
        proposal.consumer_error = v.get('error', '') or ''
        if v['status'] == RecommendationProposal.Status.APPLIED:
            proposal.consumer_applied_at = timezone.now()
        proposal.save()
        return Response(
            RecommendationProposalSerializer(proposal).data,
            status=http_status.HTTP_200_OK,
        )


class RecommendationMeasurementView(_TenantScopedMixin, APIView):
    """POST /recommendations/<uuid>/measurement — create the outcome
    measurement for an applied recommendation. Idempotent per
    lb_recommendation_application_id.

    GET → return the existing measurement (single row per rec since v1
    is 1:1). 404 when no measurement has been created yet.

    LB calls POST immediately after a successful Apply, providing the
    treatment config hashes and the LB application id. BehaviorOS
    freezes the MeasurementSpec + baseline cohort deterministically
    and never mutates that frozen contract afterward — only the
    accumulated post counters + verdict update over time.
    """

    def _load_rec(self, request, pk):
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
        return rec

    def get(self, request, pk):
        rec = self._load_rec(request, pk)
        row = (
            RecommendationOutcomeMeasurement.objects
            .filter(recommendation=rec)
            .order_by('-created_at')
            .first()
        )
        if row is None:
            raise NotFound({'detail': 'no measurement created yet'})
        return Response(
            RecommendationOutcomeMeasurementSerializer(row).data,
        )

    def post(self, request, pk):
        rec = self._load_rec(request, pk)
        payload = MeasurementCreateRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        v = payload.validated_data
        ctx = LbApplyContext(
            lb_recommendation_application_id=(
                v['lb_recommendation_application_id']
            ),
            applied_at=v['applied_at'],
            pre_effective_config_hash=v.get(
                'pre_effective_config_hash', ''
            ),
            treatment_effective_config_hash=(
                v['treatment_effective_config_hash']
            ),
            treatment_managed_hash=v['treatment_managed_hash'],
            effective_config_schema_version=(
                v['effective_config_schema_version']
            ),
        )
        try:
            row = create_measurement(rec, ctx)
        except MeasurementCreationError as exc:
            return Response(
                {'detail': str(exc), 'reason': 'creation_failed'},
                status=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except Exception as exc:
            logger.exception(
                'measurement creation failed for rec=%s', pk,
            )
            return Response(
                {'detail': f'measurement creation error: {exc!r}'},
                status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            RecommendationOutcomeMeasurementSerializer(row).data,
            status=http_status.HTTP_201_CREATED,
        )
