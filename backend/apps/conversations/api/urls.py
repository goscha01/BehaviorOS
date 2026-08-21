"""BehaviorOS Insights API URL routes.

Mounted under `/api/v1/insights/` from config/urls.py.
"""

from __future__ import annotations

from django.urls import path
from rest_framework.routers import SimpleRouter

from apps.conversations.api.views import (
    ConfiguredFaqRunView, ConfiguredPricingRunView,
    ConfiguredQualificationRunView, ConfiguredServiceScopeRunView,
    ExtractionRunStatusView,
    ObservedFaqRunView, ObservedPricingRunView,
    ObservedQualificationRunView, ObservedServiceScopeRunView,
    ReconstructionReportView, ReconstructionRunView,
    RecommendationDetailView, RecommendationLifecycleView,
    RecommendationMeasurementView,
    RecommendationProposalStatusView, RecommendationProposalView,
    RecommendationRunViewSet, RomV1BenchmarkView,
    TenantConfigAuditView,
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
        'audit/reconstruction/run',
        ReconstructionRunView.as_view(),
        name='insights-audit-reconstruction-run',
    ),
    path(
        'audit/reconstruction/latest',
        ReconstructionReportView.as_view(),
        name='insights-audit-reconstruction-latest',
    ),
]
