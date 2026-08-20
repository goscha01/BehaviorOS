"""BehaviorOS Insights API URL routes.

Mounted under `/api/v1/insights/` from config/urls.py.
"""

from __future__ import annotations

from django.urls import path
from rest_framework.routers import SimpleRouter

from apps.conversations.api.views import (
    RecommendationDetailView, RecommendationLifecycleView,
    RecommendationRunViewSet,
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
]
