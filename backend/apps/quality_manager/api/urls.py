"""Quality Manager V1 URL routes — mounted under /api/v1/quality-manager/."""

from django.urls import path

from apps.quality_manager.api.views import (
    QualityManagerConversationReportView,
    QualityManagerFindingDetailView,
    QualityManagerFindingsListView,
    QualityManagerRunStatusView,
    QualityManagerRunView,
)


urlpatterns = [
    path(
        'run',
        QualityManagerRunView.as_view(),
        name='qm-run',
    ),
    path(
        'runs/<uuid:run_id>',
        QualityManagerRunStatusView.as_view(),
        name='qm-run-status',
    ),
    path(
        'findings',
        QualityManagerFindingsListView.as_view(),
        name='qm-findings-list',
    ),
    path(
        'findings/<uuid:evaluation_id>',
        QualityManagerFindingDetailView.as_view(),
        name='qm-finding-detail',
    ),
    path(
        'conversation/<uuid:conversation_id>',
        QualityManagerConversationReportView.as_view(),
        name='qm-conversation-report',
    ),
]
