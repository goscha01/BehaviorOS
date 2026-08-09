from django.urls import path

from apps.context.analyze_batch import AnalyzeBatchView
from apps.context.views import ContextView


urlpatterns = [
    path('v1/context', ContextView.as_view(), name='context-v1'),
    path('v1/context/', ContextView.as_view(), name='context-v1-trailing'),
    path('v1/context/analyze-batch', AnalyzeBatchView.as_view(), name='context-v1-analyze-batch'),
    path('v1/context/analyze-batch/', AnalyzeBatchView.as_view(), name='context-v1-analyze-batch-trailing'),
]
