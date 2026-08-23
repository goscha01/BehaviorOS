from django.contrib import admin

from apps.quality_manager.models import QualityEvaluation, QualityRun


@admin.register(QualityRun)
class QualityRunAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'org', 'qm_version', 'status',
        'conversations_evaluated', 'created_at', 'completed_at',
    )
    list_filter = ('status', 'qm_version')
    search_fields = ('org__name',)
    readonly_fields = (
        'org', 'reconstruction_run', 'stats_json',
        'started_at', 'completed_at', 'created_at', 'updated_at',
    )


@admin.register(QualityEvaluation)
class QualityEvaluationAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'dimension', 'state', 'severity',
        'reason_code', 'conversation', 'created_at',
    )
    list_filter = ('dimension', 'state', 'severity', 'reason_code')
    search_fields = ('rationale_text', 'reason_code')
    readonly_fields = ('created_at', 'updated_at')
