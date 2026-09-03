from django.urls import path

from .views import GenerateProposalView

urlpatterns = [
    path('generate', GenerateProposalView.as_view(), name='business-config-generate'),
]
