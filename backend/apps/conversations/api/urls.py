"""BehaviorOS Insights API URL routes.

Mounted under `/api/v1/insights/` from config/urls.py.
"""

from __future__ import annotations

from django.urls import path
from rest_framework.routers import SimpleRouter

from apps.conversations.api.views import (
    AddCustomBusinessRuleView,
    ApproveCommunicationDiffView, ApproveReconstructedFactView,
    BootstrapOrgView,
    CanonicalContextTraceView,
    CommunicationProfileLatestView, CommunicationProfileRunView,
    ConfiguredFaqRunView, ConfiguredPricingRunView,
    ConfiguredQualificationRunView, ConfiguredServiceScopeRunView,
    DismissCommunicationDiffView,
    ExtractionRunStatusView,
    FreezeCorpusView,
    IngestCorpusView, IngestStatusView,
    ObservedFaqRunView, ObservedPricingRunView,
    ObservedQualificationRunView, ObservedServiceScopeRunView,
    LeadMetadataCoverageView,
    OwnerReviewPayloadView,
    PricingAcceptanceReportView,
    ReconstructionReportView, ReconstructionRunView,
    RecommendationDetailView, RecommendationLifecycleView,
    RecommendationMeasurementView,
    RecommendationProposalStatusView, RecommendationProposalView,
    RecommendationRunViewSet, RomV1BenchmarkView,
    SnapshotLbConfigView,
    TenantBehaviorProfileEffectiveView,
    TenantCandidatesView, TenantConfigAuditView,
    TenantEvidenceSummaryView,
)


_router = SimpleRouter(trailing_slash=False)
_router.register(r'runs', RecommendationRunViewSet, basename='insights-runs')


urlpatterns = _router.urls + [
    path(
        'recommendations/<uuid:pk>',
        RecommendationDetailView.as_view(),
        name='insights-recommendation-detail',
    ),
    path(
        'recommendations/<uuid:pk>/lifecycle',
        RecommendationLifecycleView.as_view(),
        name='insights-recommendation-lifecycle',
    ),
    path(
        'recommendations/<uuid:pk>/proposal',
        RecommendationProposalView.as_view(),
        name='insights-recommendation-proposal',
    ),
    path(
        'recommendations/<uuid:pk>/proposal/status',
        RecommendationProposalStatusView.as_view(),
        name='insights-recommendation-proposal-status',
    ),
    path(
        'recommendations/<uuid:pk>/measurement',
        RecommendationMeasurementView.as_view(),
        name='insights-recommendation-measurement',
    ),
    path(
        'rom/benchmark',
        RomV1BenchmarkView.as_view(),
        name='insights-rom-benchmark',
    ),
    path(
        'audit/config-vs-extracted',
        TenantConfigAuditView.as_view(),
        name='insights-audit-config-vs-extracted',
    ),
    path(
        'audit/observed-pricing/run',
        ObservedPricingRunView.as_view(),
        name='insights-audit-observed-pricing-run',
    ),
    path(
        'audit/configured-pricing/run',
        ConfiguredPricingRunView.as_view(),
        name='insights-audit-configured-pricing-run',
    ),
    path(
        'audit/extraction-runs/<uuid:run_id>',
        ExtractionRunStatusView.as_view(),
        name='insights-audit-extraction-run-status',
    ),
    path(
        'audit/observed-qualification/run',
        ObservedQualificationRunView.as_view(),
        name='insights-audit-observed-qualification-run',
    ),
    path(
        'audit/configured-qualification/run',
        ConfiguredQualificationRunView.as_view(),
        name='insights-audit-configured-qualification-run',
    ),
    path(
        'audit/observed-faq/run',
        ObservedFaqRunView.as_view(),
        name='insights-audit-observed-faq-run',
    ),
    path(
        'audit/configured-faq/run',
        ConfiguredFaqRunView.as_view(),
        name='insights-audit-configured-faq-run',
    ),
    path(
        'audit/observed-service-scope/run',
        ObservedServiceScopeRunView.as_view(),
        name='insights-audit-observed-service-scope-run',
    ),
    path(
        'audit/configured-service-scope/run',
        ConfiguredServiceScopeRunView.as_view(),
        name='insights-audit-configured-service-scope-run',
    ),
    path(
        'audit/tenants',
        TenantCandidatesView.as_view(),
        name='insights-audit-tenants',
    ),
    path(
        'audit/tenant-evidence-summary',
        TenantEvidenceSummaryView.as_view(),
        name='insights-audit-tenant-evidence-summary',
    ),
    path(
        'audit/pricing-1d-acceptance',
        PricingAcceptanceReportView.as_view(),
        name='insights-audit-pricing-1d-acceptance',
    ),
    path(
        'audit/lead-metadata-coverage',
        LeadMetadataCoverageView.as_view(),
        name='insights-audit-lead-metadata-coverage',
    ),
    path(
        'audit/canonical-context/<uuid:conversation_id>',
        CanonicalContextTraceView.as_view(),
        name='insights-audit-canonical-context-trace',
    ),
    path(
        'audit/setup/bootstrap-org',
        BootstrapOrgView.as_view(),
        name='insights-audit-setup-bootstrap-org',
    ),
    path(
        'audit/setup/snapshot-config',
        SnapshotLbConfigView.as_view(),
        name='insights-audit-setup-snapshot-config',
    ),
    path(
        'audit/setup/ingest-corpus',
        IngestCorpusView.as_view(),
        name='insights-audit-setup-ingest-corpus',
    ),
    path(
        'audit/setup/ingest-status',
        IngestStatusView.as_view(),
        name='insights-audit-setup-ingest-status',
    ),
    path(
        'audit/setup/freeze-corpus',
        FreezeCorpusView.as_view(),
        name='insights-audit-setup-freeze-corpus',
    ),
    path(
        'audit/reconstruction/run',
        ReconstructionRunView.as_view(),
        name='insights-audit-reconstruction-run',
    ),
    path(
        'audit/reconstruction/latest',
        ReconstructionReportView.as_view(),
        name='insights-audit-reconstruction-latest',
    ),
    # ---- MVP: CommunicationProfile v1 + TenantBehaviorProfile v1 ---------
    path(
        'audit/communication-profile/run',
        CommunicationProfileRunView.as_view(),
        name='insights-audit-communication-profile-run',
    ),
    path(
        'audit/communication-profile/latest',
        CommunicationProfileLatestView.as_view(),
        name='insights-audit-communication-profile-latest',
    ),
    path(
        'audit/owner-review/latest',
        OwnerReviewPayloadView.as_view(),
        name='insights-audit-owner-review-latest',
    ),
    path(
        'audit/owner-review/comm-diff/approve',
        ApproveCommunicationDiffView.as_view(),
        name='insights-audit-owner-review-comm-diff-approve',
    ),
    path(
        'audit/owner-review/comm-diff/dismiss',
        DismissCommunicationDiffView.as_view(),
        name='insights-audit-owner-review-comm-diff-dismiss',
    ),
    path(
        'audit/owner-review/business-rule/approve',
        ApproveReconstructedFactView.as_view(),
        name='insights-audit-owner-review-business-rule-approve',
    ),
    path(
        'audit/owner-review/custom-rule',
        AddCustomBusinessRuleView.as_view(),
        name='insights-audit-owner-review-custom-rule',
    ),
    path(
        'tenant-behavior-profile/effective',
        TenantBehaviorProfileEffectiveView.as_view(),
        name='insights-tenant-behavior-profile-effective',
    ),
]
