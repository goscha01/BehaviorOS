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


class TenantConfigAuditView(APIView):
    """POST /api/v1/insights/audit/config-vs-extracted?tenantId=<uuid>

    Read-only audit that assembles the field/rule-level diff between:
      - LB-side normalized rules (BehavioralPolicy from
        TenantConfigSnapshot via config_normalizer)
      - Conversation-derived observed rules (ConditionalActionPattern
        from LearningCorpus via SemanticExtractionRun + prompt-v3)

    Emits MATCH / CONFLICT / MISSING_IN_LB / MISSING_IN_EXTRACTED /
    LOW_CONFIDENCE buckets plus content-evidence excerpts and a
    filter of onboarding-suitable proposals. Never modifies anything.

    Ship A extension: composes Pipeline 1D `structured_facts.pricing`
    section from the latest ObservedFactExtractionRun and
    ConfiguredFactParserRun (both domain=pricing) plus the
    deterministic diff.

    Service-token authenticated. See config_diff.py for the full
    semantics + invariant enforcement.
    """

    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.conversations.audit.config_diff import (
            build_audit, report_to_dict,
        )
        from apps.conversations.audit.structured_facts_composer import (
            build_structured_facts_section,
        )
        tenant = (request.query_params.get('tenantId') or '').strip()
        if not tenant:
            raise ValidationError({'tenantId': 'required'})
        report = build_audit(tenant)
        if report is None:
            return Response(
                {'detail': f'no TenantConfigSnapshot for tenant {tenant}'},
                status=http_status.HTTP_404_NOT_FOUND,
            )
        payload = report_to_dict(report)
        payload['structured_facts'] = build_structured_facts_section(tenant)
        return Response(payload)


class ObservedPricingRunView(APIView):
    """POST /api/v1/insights/audit/observed-pricing/run?tenantId=<uuid>[&limit=N]

    Ship A.1: async execution. Creates or reuses a PENDING run row,
    queues the Celery task on behavioros-worker, returns 202 with
    the run_id immediately. Poll GET /audit/extraction-runs/<run_id>
    for status.

    Idempotent per (org, corpus, extractor_version): if a
    PENDING/RUNNING run already exists, returns its id (does NOT
    queue a duplicate task).
    """

    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.conversations.models import (
            LearningCorpus, TenantConfigSnapshot as _TCS,
        )
        from apps.conversations.observed_config.pricing.extractor import (
            create_or_reuse_run,
        )
        from apps.conversations.tasks import (
            observed_pricing_extraction_task,
        )
        tenant = (request.query_params.get('tenantId') or '').strip()
        if not tenant:
            raise ValidationError({'tenantId': 'required'})
        limit = request.query_params.get('limit')
        try:
            limit_int = int(limit) if limit else None
        except ValueError:
            raise ValidationError({'limit': 'must be integer'})

        snap = (
            _TCS.objects.filter(
                source_system='leadbridge', tenant_external_id=tenant,
            ).order_by('-created_at').first()
        )
        if snap is None:
            raise NotFound({'detail': f'no snapshot for tenant {tenant}'})
        corpus = (
            LearningCorpus.objects
            .filter(org=snap.org)
            .order_by('-created_at').first()
        )
        if corpus is None:
            raise NotFound(
                {'detail': f'no LearningCorpus for tenant {tenant}'}
            )
        run, created = create_or_reuse_run(
            org=snap.org, corpus=corpus,
        )
        if created:
            observed_pricing_extraction_task.delay(
                str(run.id), 'gpt-4o-mini', limit_int,
            )
        return Response(
            {
                'run_id': str(run.id),
                'status': run.status,
                'created': created,
                'note': (
                    'Queued on behavioros-worker. Poll '
                    'GET /audit/extraction-runs/<run_id>.'
                    if created else
                    'Existing PENDING/RUNNING run reused.'
                ),
            },
            status=http_status.HTTP_202_ACCEPTED,
        )


class ObservedFaqRunView(APIView):
    """POST /api/v1/insights/audit/observed-faq/run?tenantId=<uuid>[&limit=N]
    Ship C. Async, 202 + run_id."""
    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.conversations.models import (
            LearningCorpus, TenantConfigSnapshot as _TCS,
        )
        from apps.conversations.observed_config.faq.extractor import (
            create_or_reuse_run,
        )
        from apps.conversations.tasks import (
            observed_faq_extraction_task,
        )
        tenant = (request.query_params.get('tenantId') or '').strip()
        if not tenant:
            raise ValidationError({'tenantId': 'required'})
        limit = request.query_params.get('limit')
        try:
            limit_int = int(limit) if limit else None
        except ValueError:
            raise ValidationError({'limit': 'must be integer'})
        snap = (
            _TCS.objects.filter(
                source_system='leadbridge', tenant_external_id=tenant,
            ).order_by('-created_at').first()
        )
        if snap is None:
            raise NotFound({'detail': f'no snapshot for tenant {tenant}'})
        corpus = (
            LearningCorpus.objects.filter(org=snap.org)
            .order_by('-created_at').first()
        )
        if corpus is None:
            raise NotFound(
                {'detail': f'no LearningCorpus for tenant {tenant}'}
            )
        run, created = create_or_reuse_run(
            org=snap.org, corpus=corpus,
        )
        if created:
            observed_faq_extraction_task.delay(
                str(run.id), 'gpt-4o-mini', limit_int,
            )
        return Response(
            {
                'run_id': str(run.id),
                'status': run.status,
                'created': created,
                'note': (
                    'Queued on behavioros-worker. Poll '
                    'GET /audit/extraction-runs/<run_id>.'
                    if created else
                    'Existing PENDING/RUNNING run reused.'
                ),
            },
            status=http_status.HTTP_202_ACCEPTED,
        )


class ConfiguredFaqRunView(APIView):
    """POST /api/v1/insights/audit/configured-faq/run?tenantId=<uuid>
    Ship C. Synchronous — LB config parsing is fast."""
    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.conversations.models import (
            TenantConfigSnapshot as _TCS,
        )
        from apps.conversations.observed_config.faq.config_parser import (
            parse_snapshot,
        )
        tenant = (request.query_params.get('tenantId') or '').strip()
        if not tenant:
            raise ValidationError({'tenantId': 'required'})
        snap = (
            _TCS.objects.filter(
                source_system='leadbridge', tenant_external_id=tenant,
            ).order_by('-created_at').first()
        )
        if snap is None:
            raise NotFound({'detail': f'no snapshot for tenant {tenant}'})
        run = parse_snapshot(
            snapshot=snap, llm_client=LearningLLMClient(),
        )
        return Response({
            'run_id': str(run.id),
            'status': run.status,
            'snapshot_id': str(snap.id),
            'facts_emitted': run.facts_emitted,
            'llm_cost_usd': str(run.llm_cost_usd),
        })


class ObservedQualificationRunView(APIView):
    """POST /api/v1/insights/audit/observed-qualification/run?tenantId=<uuid>[&limit=N]

    Ship B counterpart of the pricing trigger. Async — returns 202
    with run_id; poll GET /audit/extraction-runs/<uuid>.
    Idempotent per (org, corpus, extractor_version).
    """
    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.conversations.models import (
            LearningCorpus, TenantConfigSnapshot as _TCS,
        )
        from apps.conversations.observed_config.qualification.extractor import (
            create_or_reuse_run,
        )
        from apps.conversations.tasks import (
            observed_qualification_extraction_task,
        )
        tenant = (request.query_params.get('tenantId') or '').strip()
        if not tenant:
            raise ValidationError({'tenantId': 'required'})
        limit = request.query_params.get('limit')
        try:
            limit_int = int(limit) if limit else None
        except ValueError:
            raise ValidationError({'limit': 'must be integer'})
        snap = (
            _TCS.objects.filter(
                source_system='leadbridge', tenant_external_id=tenant,
            ).order_by('-created_at').first()
        )
        if snap is None:
            raise NotFound({'detail': f'no snapshot for tenant {tenant}'})
        corpus = (
            LearningCorpus.objects.filter(org=snap.org)
            .order_by('-created_at').first()
        )
        if corpus is None:
            raise NotFound(
                {'detail': f'no LearningCorpus for tenant {tenant}'}
            )
        run, created = create_or_reuse_run(
            org=snap.org, corpus=corpus,
        )
        if created:
            observed_qualification_extraction_task.delay(
                str(run.id), 'gpt-4o-mini', limit_int,
            )
        return Response(
            {
                'run_id': str(run.id),
                'status': run.status,
                'created': created,
                'note': (
                    'Queued on behavioros-worker. Poll '
                    'GET /audit/extraction-runs/<run_id>.'
                    if created else
                    'Existing PENDING/RUNNING run reused.'
                ),
            },
            status=http_status.HTTP_202_ACCEPTED,
        )


class ConfiguredQualificationRunView(APIView):
    """POST /api/v1/insights/audit/configured-qualification/run?tenantId=<uuid>

    Synchronous run — LB config parsing is fast (single LLM pass per
    source, ~1-2 seconds). Idempotent per (snapshot, parser_version).
    """
    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.conversations.models import (
            TenantConfigSnapshot as _TCS,
        )
        from apps.conversations.observed_config.qualification.config_parser import (
            parse_snapshot,
        )
        tenant = (request.query_params.get('tenantId') or '').strip()
        if not tenant:
            raise ValidationError({'tenantId': 'required'})
        snap = (
            _TCS.objects.filter(
                source_system='leadbridge', tenant_external_id=tenant,
            ).order_by('-created_at').first()
        )
        if snap is None:
            raise NotFound({'detail': f'no snapshot for tenant {tenant}'})
        run = parse_snapshot(
            snapshot=snap, llm_client=LearningLLMClient(),
        )
        return Response({
            'run_id': str(run.id),
            'status': run.status,
            'snapshot_id': str(snap.id),
            'facts_emitted': run.facts_emitted,
            'llm_cost_usd': str(run.llm_cost_usd),
        })


class ExtractionRunStatusView(APIView):
    """GET /api/v1/insights/audit/extraction-runs/<uuid>

    Read-only status endpoint for the async 1D extraction runs.
    Returns run counters + LLM cost + terminal status. Poll from the
    caller after POST /audit/observed-pricing/run.
    """

    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, run_id):
        from apps.conversations.models import (
            ObservedFactExtractionRun as _R,
        )
        try:
            run = _R.objects.get(pk=run_id)
        except _R.DoesNotExist:
            raise NotFound({'detail': f'no run {run_id}'})
        return Response({
            'run_id': str(run.id),
            'org_id': str(run.org_id),
            'corpus_id': str(run.corpus_id),
            'domain': run.domain,
            'extractor_version': run.extractor_version,
            'model': run.model,
            'status': run.status,
            'started_at': (
                run.started_at.isoformat() if run.started_at else None
            ),
            'completed_at': (
                run.completed_at.isoformat()
                if run.completed_at else None
            ),
            'conversations_processed': run.conversations_processed,
            'facts_emitted': run.facts_emitted,
            'ontology_review_candidates_emitted': (
                run.ontology_review_candidates_emitted
            ),
            'llm_cost_usd': str(run.llm_cost_usd),
            'stats': run.stats_json,
            'error_message': run.error_message,
        })


class ConfiguredPricingRunView(APIView):
    """POST /api/v1/insights/audit/configured-pricing/run?tenantId=<uuid>

    Triggers a Pipeline 1D LB pricing parser run over the tenant's
    latest TenantConfigSnapshot. Idempotent per (snapshot,
    parser_version).
    """

    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.conversations.models import (
            TenantConfigSnapshot as _TCS,
        )
        from apps.conversations.observed_config.pricing.config_parser import (
            parse_snapshot,
        )
        tenant = (request.query_params.get('tenantId') or '').strip()
        if not tenant:
            raise ValidationError({'tenantId': 'required'})
        snap = (
            _TCS.objects.filter(
                source_system='leadbridge', tenant_external_id=tenant,
            ).order_by('-created_at').first()
        )
        if snap is None:
            raise NotFound({'detail': f'no snapshot for tenant {tenant}'})
        run = parse_snapshot(
            snapshot=snap, llm_client=LearningLLMClient(),
        )
        return Response({
            'run_id': str(run.id),
            'status': run.status,
            'snapshot_id': str(snap.id),
            'facts_emitted': run.facts_emitted,
            'llm_cost_usd': str(run.llm_cost_usd),
        })


class RomV1BenchmarkView(APIView):
    """POST /api/v1/insights/rom/benchmark?tenantId=<uuid>

    Service-token-authenticated (uses the same InsightsServiceTokenAuthentication
    the other insights endpoints use — no LB JWT needed since this is a
    verification endpoint invoked by tooling / operator during ROM v1
    historical acceptance testing, NOT by the LB frontend).

    Runs the same three checks as the benchmark_rom_v1 management command
    and returns the results as JSON. Safe against production: no lifecycle
    transitions, no LB writes, no proposal generation. The only side
    effect is a synthetic measurement row (unique id
    'benchmark-<ts>-<rec8>' so it never collides with a real apply).

    Passing ?dry_run=1 skips the measurement-row persistence.
    """

    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from dataclasses import replace as _replace
        from django.utils import timezone as _tz
        from apps.conversations.measurement.creation import (
            LbApplyContext as _LbCtx,
            MeasurementCreationError as _MCE,
            _compute_baseline_cohort as _cbc,
            create_measurement as _cm,
        )
        from apps.conversations.measurement.effective_config_contract import (
            EFFECTIVE_CONFIG_SCHEMA_VERSION as _SCHEMA,
        )
        from apps.conversations.measurement.specs import (
            HIGH_INTENT_SIGNALS as _HI,
            HIGH_INTENT_SIGNAL_COVERAGE_V1 as _SPEC,
            FrozenMeasurementSpec as _FMS,
        )
        from apps.conversations.models import (
            BehaviorRecommendation as _BR,
            TenantConfigSnapshot as _TCS,
        )

        tenant = (request.query_params.get('tenantId') or '').strip()
        if not tenant:
            raise ValidationError({'tenantId': 'required'})
        dry_run = str(request.query_params.get('dry_run', '')).lower() in (
            '1', 'true', 'yes',
        )
        lookback = request.query_params.get('lookback_days')
        rec_override = request.query_params.get('rec_uuid')

        snap = (
            _TCS.objects
            .filter(source_system='leadbridge', tenant_external_id=tenant)
            .order_by('-created_at').first()
        )
        if snap is None:
            return Response(
                {'detail': f'no TenantConfigSnapshot for tenant {tenant}'},
                status=http_status.HTTP_404_NOT_FOUND,
            )
        org = snap.org
        applied_at = _tz.now()

        outcome = _SPEC.primary_outcome
        if lookback:
            outcome = _replace(outcome, baseline_window_days=int(lookback))

        # Check 1: per-signal baseline
        per_signal: dict = {}
        for signal in sorted(_HI):
            frozen = _FMS(
                spec_key=_SPEC.spec_key, version=_SPEC.version,
                family=_SPEC.family, description=_SPEC.description,
                cohort_entry=_replace(_SPEC.cohort_entry, signal=signal),
                primary_outcome=outcome,
                exclusions=_SPEC.exclusions,
                verdict_gates=_SPEC.verdict_gates,
            )
            ids, pos, neg, matured, unresolved = _cbc(
                org=org, tenant_external_id=tenant,
                target_signal=signal, applied_at=applied_at,
                freeze_time=applied_at,
                spec=frozen,
            )
            n = pos + neg
            per_signal[signal] = {
                'cohort_membership_n': len(ids),
                'matured_n': matured,
                'resolved_n': n,
                'positive_n': pos,
                'negative_n': neg,
                'unresolved_n': unresolved,
                'positive_rate': (pos / n) if n > 0 else None,
                'outcome_resolution_coverage': (
                    (n / matured) if matured > 0 else None
                ),
            }

        # Check 2: end-to-end persistence
        e2e = None
        if not dry_run:
            rec = self._pick_rec(tenant, rec_override, _BR, _HI)
            if rec is not None:
                synthetic_id = (
                    f'benchmark-{applied_at.strftime("%Y%m%d%H%M%S")}-'
                    f'{str(rec.pk)[:8]}'
                )
                ctx = _LbCtx(
                    lb_recommendation_application_id=synthetic_id,
                    applied_at=applied_at,
                    pre_effective_config_hash='benchmark_pre_' + '0' * 50,
                    treatment_effective_config_hash=(
                        'benchmark_treatment_' + '0' * 44
                    ),
                    treatment_managed_hash=(
                        'benchmark_managed_' + '0' * 46
                    ),
                    effective_config_schema_version=_SCHEMA,
                )
                try:
                    row = _cm(rec, ctx)
                    e2e = {
                        'rom_id': str(row.id),
                        'lb_recommendation_application_id': (
                            row.lb_recommendation_application_id
                        ),
                        'rec_id': rec.recommendation_id,
                        'target_signal': row.target_signal,
                        'pre_cohort_ids_len': len(
                            row.pre_cohort_conversation_ids
                        ),
                        'pre_n': row.pre_n,
                        'pre_positive_n': row.pre_positive_n,
                        'pre_rate': row.pre_rate,
                        'status': row.status,
                        'measurement_deadline_at': (
                            row.measurement_deadline_at.isoformat()
                        ),
                    }
                    ref = per_signal.get(row.target_signal)
                    if ref is not None:
                        e2e['cohort_agreement_with_check1'] = (
                            row.pre_n == ref['resolved_n']
                            and row.pre_positive_n == ref['positive_n']
                        )
                except _MCE as exc:
                    e2e = {'error': str(exc), 'reason': 'creation_failed'}

        any_nonzero = any(
            r['resolved_n'] > 0 for r in per_signal.values()
        )
        e2e_ok = (
            e2e is not None
            and 'error' not in e2e
            and e2e.get('pre_cohort_ids_len', 0) > 0
        )
        signals_over_floor = [
            s for s, r in per_signal.items() if r['resolved_n'] >= 30
        ]

        # Diagnostic counts — help distinguish "no data" from "wrong
        # scoping" from "wrong signal names". Displayed independently
        # of the per_signal cohort computation.
        from apps.conversations.models import (
            Conversation as _Conv,
            ConversationSemanticEvent as _CSE,
            OutcomeSnapshot as _OS,
        )
        from django.db.models import Count, Min, Max
        from django.db.models import Q as _Q
        conv_by_source = list(
            _Conv.objects.filter(org=org)
            .values('source')
            .annotate(
                n=Count('id'),
                first=Min('started_at'),
                last=Max('started_at'),
            ).order_by('-n')
        )
        # Which OutcomeSnapshots actually carry a terminal marker?
        # This distinguishes "snapshots exist but empty" from "snapshots
        # are populated but attribution window is too narrow".
        outcome_terminal_dist = {
            'total_snapshots': _OS.objects.filter(
                conversation__org=org,
            ).count(),
            'with_lb_booked_true': _OS.objects.filter(
                conversation__org=org, lb_booked=True,
            ).count(),
            'with_lb_lost_true': _OS.objects.filter(
                conversation__org=org, lb_lost=True,
            ).count(),
            'with_sf_booked_true': _OS.objects.filter(
                conversation__org=org, sf_booked=True,
            ).count(),
            'with_sf_completed_true': _OS.objects.filter(
                conversation__org=org, sf_completed=True,
            ).count(),
            'with_any_terminal': _OS.objects.filter(
                conversation__org=org,
            ).filter(
                _Q(lb_booked=True) | _Q(lb_lost=True) | _Q(lb_cancelled=True)
                | _Q(sf_booked=True) | _Q(sf_completed=True) | _Q(sf_cancelled=True),
            ).count(),
            'distinct_convs_with_any_terminal': _OS.objects.filter(
                conversation__org=org,
            ).filter(
                _Q(lb_booked=True) | _Q(lb_lost=True) | _Q(lb_cancelled=True)
                | _Q(sf_booked=True) | _Q(sf_completed=True) | _Q(sf_cancelled=True),
            ).values('conversation_id').distinct().count(),
        }
        conv_by_source_out = [
            {
                'source': r['source'], 'n': r['n'],
                'first': r['first'].isoformat() if r['first'] else None,
                'last': r['last'].isoformat() if r['last'] else None,
            } for r in conv_by_source
        ]
        hi_event_counts = dict(
            _CSE.objects.filter(
                org=org, event_type__in=list(_HI),
            ).values('event_type').annotate(n=Count('id')).values_list(
                'event_type', 'n',
            )
        )
        outcome_snapshot_count = _OS.objects.filter(
            conversation__org=org,
        ).count()
        # Existing top event types (helpful when HIGH_INTENT counts are 0
        # — shows what event_type strings actually exist)
        top_event_types = list(
            _CSE.objects.filter(org=org)
            .values('event_type')
            .annotate(n=Count('id'))
            .order_by('-n')[:15]
            .values_list('event_type', 'n')
        )

        return Response({
            'tenant_external_id': tenant,
            'org_id': str(org.pk),
            'snapshot_id': str(snap.pk),
            'snapshot_sha_prefix': snap.raw_config_sha256[:12],
            'applied_at_used': applied_at.isoformat(),
            'baseline_window_days_used': outcome.baseline_window_days,
            'diagnostics': {
                'conversations_by_source': conv_by_source_out,
                'high_intent_event_counts': hi_event_counts,
                'outcome_snapshot_count': outcome_snapshot_count,
                'outcome_terminal_distribution': outcome_terminal_dist,
                'top_event_types_org_wide': [
                    {'event_type': t, 'n': n} for t, n in top_event_types
                ],
            },
            'per_signal': per_signal,
            'end_to_end': e2e,
            'summary': {
                'signals_with_nonzero_baseline': [
                    s for s, r in per_signal.items() if r['resolved_n'] > 0
                ],
                'signals_meeting_v1_sample_floor_30': signals_over_floor,
                'end_to_end_persistence_ok': (
                    None if dry_run else e2e_ok
                ),
                'acceptance': (
                    'READY' if (any_nonzero and (dry_run or e2e_ok))
                    else 'NOT_READY'
                ),
            },
        })

    def _pick_rec(self, tenant, rec_uuid, _BR, _HI):
        if rec_uuid:
            try:
                return _BR.objects.select_related(
                    'run', 'run__config_snapshot',
                ).get(pk=rec_uuid)
            except _BR.DoesNotExist:
                return None
        candidates = _BR.objects.filter(
            run__config_snapshot__tenant_external_id=tenant,
            rec_class__in=[
                'STATE_COVERAGE_GAP', 'STATE_PARTIAL_COVERAGE',
            ],
        ).select_related('run', 'run__config_snapshot').order_by(
            'run__created_at', 'recommendation_id',
        )
        for c in candidates:
            if c.subject_signals and c.subject_signals[0] in _HI:
                return c
        return None


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
