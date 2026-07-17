from django.urls import path

from apps.context.views import ContextView


urlpatterns = [
    path('v1/context', ContextView.as_view(), name='context-v1'),
    path('v1/context/', ContextView.as_view(), name='context-v1-trailing'),
]
