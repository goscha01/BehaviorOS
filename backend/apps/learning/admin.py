from django.contrib import admin

from apps.learning.models import (
    EvidenceInsight,
    LearningJob,
    LearningSuggestion,
    RejectedSuggestionSignature,
    SuggestionEvidence,
)


@admin.register(LearningJob)
class LearningJobAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'org', 'status', 'triggered_by',
        'started_at', 'completed_at',
        'evidence_processed', 'suggestions_created', 'cost_usd',
    )
    list_filter = ('status', 'triggered_by')
    search_fields = ('org__name',)
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'started_at'


class SuggestionEvidenceInline(admin.TabularInline):
    model = SuggestionEvidence
    extra = 0
    fk_name = 'suggestion'
    autocomplete_fields = ('evidence',)


@admin.register(EvidenceInsight)
class EvidenceInsightAdmin(admin.ModelAdmin):
    list_display = (
        'source_system', 'external_id', 'evidence_type',
        'outcome', 'source_business_rules_version',
        'analyzed_at', 'occurred_at',
    )
    list_filter = ('source_system', 'evidence_type', 'outcome', 'analyzed_at')
    search_fields = (
        'external_id', 'source_system', 'ai_summary',
        'source_business_rules_version',
    )
    readonly_fields = ('created_at', 'updated_at', 'analyzed_at')
    date_hierarchy = 'occurred_at'


@admin.register(LearningSuggestion)
class LearningSuggestionAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'category', 'status', 'confidence',
        'supporting_count', 'reviewed_by', 'reviewed_at', 'created_at',
    )
    list_filter = ('status', 'category')
    search_fields = ('title', 'description', 'org__name')
    readonly_fields = ('created_at', 'updated_at', 'supporting_count')
    inlines = [SuggestionEvidenceInline]
    actions = ['mark_approved', 'mark_rejected']

    @admin.action(description='Mark selected suggestions as approved')
    def mark_approved(self, request, queryset):
        queryset.update(status=LearningSuggestion.Status.APPROVED)

    @admin.action(description='Mark selected suggestions as rejected')
    def mark_rejected(self, request, queryset):
        queryset.update(status=LearningSuggestion.Status.REJECTED)


@admin.register(SuggestionEvidence)
class SuggestionEvidenceAdmin(admin.ModelAdmin):
    list_display = ('suggestion', 'evidence', 'similarity_score', 'created_at')
    search_fields = ('suggestion__title',)
    autocomplete_fields = ('suggestion', 'evidence')


@admin.register(RejectedSuggestionSignature)
class RejectedSuggestionSignatureAdmin(admin.ModelAdmin):
    list_display = ('org', 'category', 'signature', 'expires_at', 'created_at')
    list_filter = ('category',)
    search_fields = ('signature', 'org__name')
    date_hierarchy = 'expires_at'
