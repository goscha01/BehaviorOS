"""Review-queue API for LearningSuggestions.

One endpoint per lifecycle transition — conventional DRF `@action`
pattern. Keeps auditability simpler than a single POST-transition
endpoint (each side effect is a discrete URL that can be authorized
and logged independently).

Ordering matters: this ViewSet is where the API contract is anchored.
The `approve` endpoint accepts `publish_to`, and `mark_implemented`
accepts `publish_receipts` — even though Phase 1 doesn't publish
anywhere, so the contract is stable when the publisher lands.
"""

from __future__ import annotations

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsOrgMember
from apps.common.mixins import OrgQuerySetMixin
from apps.learning.models import CandidateRecommendation, LearningJob, LearningSuggestion
from apps.learning.serializers import (
    ApproveSerializer,
    LearningJobSerializer,
    LearningSuggestionDetailSerializer,
    LearningSuggestionListSerializer,
    MarkImplementedSerializer,
    MarkMeasuredSerializer,
    RejectSerializer,
    SupportingEvidenceSerializer,
)
from apps.learning.services import lifecycle


class LearningSuggestionViewSet(
    OrgQuerySetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Review-queue endpoints. Suggestions are not created via the API —
    the ingestion + analysis + clustering + synthesis pipeline creates them.
    """

    permission_classes = [IsAuthenticated, IsOrgMember]
    queryset = LearningSuggestion.objects.all()

    def get_serializer_class(self):
        if self.action == 'list':
            return LearningSuggestionListSerializer
        return LearningSuggestionDetailSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        status_filter = self.request.query_params.get('status')
        if status_filter:
            statuses = [s.strip() for s in status_filter.split(',') if s.strip()]
            qs = qs.filter(status__in=statuses)
        category = self.request.query_params.get('category')
        if category:
            qs = qs.filter(category=category)
        return qs

    def _run(self, transition_fn, *args, **kwargs):
        """Run a lifecycle transition, translating TransitionError → 400."""
        try:
            result = transition_fn(*args, **kwargs)
        except lifecycle.TransitionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'suggestion': LearningSuggestionDetailSerializer(result.suggestion).data,
            'side_effects': result.side_effects,
        })

    @action(detail=True, methods=['post'], url_path='start-review')
    def start_review(self, request, pk=None):
        suggestion = self.get_object()
        return self._run(lifecycle.start_review, suggestion, user=request.user)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        suggestion = self.get_object()
        serializer = ApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._run(
            lifecycle.approve,
            suggestion,
            user=request.user,
            note=serializer.validated_data.get('note', ''),
            publish_to=serializer.validated_data.get('publish_to', []),
        )

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        suggestion = self.get_object()
        serializer = RejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._run(
            lifecycle.reject,
            suggestion,
            user=request.user,
            reason=serializer.validated_data['reason'],
        )

    @action(detail=True, methods=['post'], url_path='mark-implemented')
    def mark_implemented(self, request, pk=None):
        suggestion = self.get_object()
        serializer = MarkImplementedSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._run(
            lifecycle.mark_implemented,
            suggestion,
            user=request.user,
            publish_receipts=serializer.validated_data.get('publish_receipts', {}),
        )

    @action(detail=True, methods=['post'], url_path='mark-measured')
    def mark_measured(self, request, pk=None):
        suggestion = self.get_object()
        serializer = MarkMeasuredSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._run(
            lifecycle.mark_measured,
            suggestion,
            user=request.user,
            impact=serializer.validated_data['impact'],
        )

    @action(detail=True, methods=['post'])
    def archive(self, request, pk=None):
        suggestion = self.get_object()
        return self._run(lifecycle.archive, suggestion, user=request.user)

    @action(detail=True, methods=['get'], url_path='supporting-evidence')
    def supporting_evidence(self, request, pk=None):
        suggestion = self.get_object()
        qs = (
            CandidateRecommendation.objects
            .filter(suggestion=suggestion)
            .select_related('evidence')
            .order_by('-llm_confidence', '-created_at')
        )
        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = SupportingEvidenceSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = SupportingEvidenceSerializer(qs, many=True)
        return Response(serializer.data)


class LearningJobViewSet(
    OrgQuerySetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Read-only view of recent jobs — for an ops sanity dashboard."""

    permission_classes = [IsAuthenticated, IsOrgMember]
    queryset = LearningJob.objects.all()
    serializer_class = LearningJobSerializer
