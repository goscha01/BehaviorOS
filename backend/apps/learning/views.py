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

from dataclasses import asdict

from django.utils.dateparse import parse_datetime
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsOrgMember
from apps.common.mixins import OrgQuerySetMixin
from apps.learning.adapters import iter_registered
from apps.learning.models import (
    CandidateRecommendation,
    LearningJob,
    LearningSuggestion,
    SourceIntegration,
)
from apps.learning.serializers import (
    ApproveSerializer,
    LearningJobSerializer,
    LearningSuggestionDetailSerializer,
    LearningSuggestionListSerializer,
    MarkImplementedSerializer,
    MarkMeasuredSerializer,
    RejectSerializer,
    SourceIntegrationSerializer,
    SourceIntegrationUpsertSerializer,
    SupportingEvidenceSerializer,
)
from apps.learning.services import lifecycle
from apps.learning.services.ingestion import EvidenceIngestionService
from apps.learning.services.trends import TrendsService


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
        created_after = self.request.query_params.get('created_after')
        if created_after:
            parsed = parse_datetime(created_after)
            if parsed is not None:
                qs = qs.filter(created_at__gte=parsed)
        ordering = self.request.query_params.get('ordering')
        if ordering:
            # Whitelist to prevent injection into ORDER BY.
            allowed = {'created_at', '-created_at', 'confidence', '-confidence',
                       'supporting_count', '-supporting_count'}
            if ordering in allowed:
                qs = qs.order_by(ordering)
        return qs

    @action(detail=False, methods=['get'], url_path='morning-brief')
    def morning_brief(self, request):
        """Compact summary of what BehaviorOS learned recently.

        One request powers the /dashboard/learning homepage — avoids
        four separate round-trips for last-job / todays-suggestions /
        category-counts / trends.
        """
        window_days = _int_param(request, 'window_days', default=30, minimum=1, maximum=365)
        brief = TrendsService(org=request.org).morning_brief(window_days=window_days)
        return Response(asdict(brief))

    @action(detail=False, methods=['get'])
    def trends(self, request):
        window_days = _int_param(request, 'window_days', default=30, minimum=1, maximum=365)
        result = TrendsService(org=request.org).trends(window_days=window_days)
        return Response(asdict(result))

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


class SourceIntegrationViewSet(
    OrgQuerySetMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """CRUD for per-org source system credentials.

    List returns one row per REGISTERED adapter (leadbridge / callio /
    serviceflow / ...), synthesizing an "unconfigured" placeholder when
    no SourceIntegration row exists yet. Keeps the frontend simple —
    a stable list of cards regardless of which are wired up.

    Writes go through `POST /` which upserts on (org, source_system).
    """

    permission_classes = [IsAuthenticated, IsOrgMember]
    queryset = SourceIntegration.objects.all()
    serializer_class = SourceIntegrationSerializer

    def list(self, request, *args, **kwargs):
        existing = {
            row.source_system: row
            for row in self.get_queryset()
        }
        payload = []
        for source_system, _cls in iter_registered():
            row = existing.get(source_system)
            if row is not None:
                payload.append(SourceIntegrationSerializer(row).data)
            else:
                payload.append({
                    'id': None,
                    'source_system': source_system,
                    'url': '',
                    'token_preview': '',
                    'is_active': False,
                    'last_synced_at': None,
                    'last_sync_status': 'never',
                    'last_sync_error': '',
                    'last_sync_created': 0,
                    'last_sync_updated': 0,
                    'created_at': None,
                    'updated_at': None,
                })
        return Response(payload)

    def create(self, request, *args, **kwargs):
        """Upsert one integration by (org, source_system)."""
        serializer = SourceIntegrationUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        org = request.org

        integration, _created = SourceIntegration.objects.get_or_create(
            org=org, source_system=data['source_system']
        )
        integration.url = data.get('url', '')
        # Blank token on update = keep existing (don't blank secrets accidentally).
        new_token = data.get('token', '')
        if new_token:
            integration.token = new_token
        integration.is_active = data.get('is_active', True)
        integration.save()
        return Response(
            SourceIntegrationSerializer(integration).data,
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=['post'], url_path='(?P<source_system>[^/.]+)/test')
    def test_connection(self, request, source_system=None):
        """Ping the source's evidence endpoint. Records success/failure
        on the integration row but does NOT persist any evidence."""
        integration = SourceIntegration.objects.filter(
            org=request.org, source_system=source_system, is_active=True
        ).first()
        if integration is None or not integration.url or not integration.token:
            return Response(
                {'ok': False, 'detail': 'Integration not configured.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from apps.learning.adapters._http import DEFAULT_TIMEOUT_SECONDS
        import requests
        try:
            r = requests.get(
                integration.url,
                headers={
                    'Authorization': f'Bearer {integration.token}',
                    'X-BehaviorOS-Client': 'learning-engine-test',
                },
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            r.raise_for_status()
            payload = r.json() if r.headers.get('content-type', '').startswith('application/json') else None
            record_count = len(payload) if isinstance(payload, list) else None
            return Response({
                'ok': True,
                'http_status': r.status_code,
                'record_count': record_count,
            })
        except Exception as exc:
            return Response(
                {'ok': False, 'detail': f'{type(exc).__name__}: {exc}'},
                status=status.HTTP_200_OK,
            )

    @action(detail=False, methods=['post'], url_path='(?P<source_system>[^/.]+)/run-sync')
    def run_sync(self, request, source_system=None):
        """Run one ingestion pass against this source — no analysis or
        clustering. Persists new EvidenceInsight rows and updates the
        integration's last_sync_* fields.
        """
        from apps.learning.adapters import get_adapter
        try:
            adapter = get_adapter(source_system)
        except LookupError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_404_NOT_FOUND)

        service = EvidenceIngestionService(org=request.org)
        result = service.ingest_source(adapter)

        # Persist result on the integration row (if one exists).
        integration = SourceIntegration.objects.filter(
            org=request.org, source_system=source_system
        ).first()
        if integration is not None:
            from django.utils import timezone
            integration.last_synced_at = timezone.now()
            integration.last_sync_created = result.created
            integration.last_sync_updated = result.updated
            if result.error:
                integration.last_sync_status = SourceIntegration.SyncStatus.ERROR
                integration.last_sync_error = result.error[:2000]
            else:
                integration.last_sync_status = SourceIntegration.SyncStatus.OK
                integration.last_sync_error = ''
            integration.save(update_fields=[
                'last_synced_at', 'last_sync_status', 'last_sync_error',
                'last_sync_created', 'last_sync_updated', 'updated_at',
            ])

        return Response({
            'ok': not bool(result.error),
            'created': result.created,
            'updated': result.updated,
            'error': result.error,
        })


def _int_param(request, name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))
