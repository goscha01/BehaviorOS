from django.urls import path

from .admin_views import PurgeTenantEvidenceView
from .views import GenerateProposalView

urlpatterns = [
    path('generate', GenerateProposalView.as_view(), name='business-config-generate'),
    path('admin/purge-tenant-evidence', PurgeTenantEvidenceView.as_view(),
         name='business-config-admin-purge'),
]
