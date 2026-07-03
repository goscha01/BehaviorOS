from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.training.views import (
    BusinessProfileViewSet,
    ScenarioTemplateViewSet,
    ScriptViewSet,
    TrainingSessionViewSet,
)

router = DefaultRouter()
router.register('business-profiles', BusinessProfileViewSet, basename='business-profile')
router.register('scenarios', ScenarioTemplateViewSet, basename='scenario')
router.register('scripts', ScriptViewSet, basename='script')
router.register('sessions', TrainingSessionViewSet, basename='session')

urlpatterns = [
    path('', include(router.urls)),
]
