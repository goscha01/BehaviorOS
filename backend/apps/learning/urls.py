from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.learning.views import (
    LearningJobViewSet,
    LearningSuggestionViewSet,
    SourceIntegrationViewSet,
)

router = DefaultRouter()
router.register('suggestions', LearningSuggestionViewSet, basename='suggestion')
router.register('jobs', LearningJobViewSet, basename='job')
router.register('integrations', SourceIntegrationViewSet, basename='integration')

urlpatterns = [
    path('', include(router.urls)),
]
