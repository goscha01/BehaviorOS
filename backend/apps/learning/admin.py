from django.contrib import admin

from apps.learning.models import (
    CandidateRecommendation,
    EvidenceInsight,
    LearningJob,
    LearningSuggestion,
    RejectedSuggestionSignature,
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


class CandidateRecommendationInline(admin.TabularInline):
    model = CandidateRecommendation
    fk_name = 'suggestion'
    extra = 0
    fields = ('kind', 'category', 'title', 'llm_confidence', 'outcome_signal')
    readonly_fields = ('kind', 'category', 'title', 'llm_confidence', 'outcome_signal')
    can_delete = False
    show_change_link = True


@admin.register(LearningSuggestion)
class LearningSuggestionAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'category', 'status', 'confidence',
        'supporting_count', 'reviewed_by', 'reviewed_at', 'created_at',
    )
    list_filter = ('status', 'category')
    search_fields = ('title', 'description', 'org__name', 'fingerprint')
    readonly_fields = ('created_at', 'updated_at', 'supporting_count', 'fingerprint')
    inlines = [CandidateRecommendationInline]
    actions = ['mark_approved', 'mark_archived']

    @admin.action(description='Mark selected suggestions as approved')
    def mark_approved(self, request, queryset):
        queryset.update(status=LearningSuggestion.Status.APPROVED)

    @admin.action(description='Archive selected suggestions')
    def mark_archived(self, request, queryset):
        # Rejection is a distinct action that requires a reason — do it
        # through the API/UI, not the admin bulk action, so the
        # RejectedSuggestionSignature gets populated correctly.
        queryset.update(status=LearningSuggestion.Status.ARCHIVED)


@admin.register(CandidateRecommendation)
class CandidateRecommendationAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'kind', 'category', 'outcome_signal',
        'llm_confidence', 'suggestion', 'clustered_at',
    )
    list_filter = ('kind', 'category', 'outcome_signal', 'clustered_at')
    search_fields = ('title', 'description', 'fingerprint')
    autocomplete_fields = ('evidence', 'suggestion')
    readonly_fields = ('fingerprint', 'tokens', 'clustered_at')


@admin.register(RejectedSuggestionSignature)
class RejectedSuggestionSignatureAdmin(admin.ModelAdmin):
    list_display = ('org', 'category', 'rejection_reason_preview', 'expires_at', 'created_at')
    list_filter = ('category',)
    search_fields = ('signature', 'rejection_reason', 'org__name')
    date_hierarchy = 'expires_at'
    readonly_fields = ('signature', 'tokens')

    @admin.display(description='Reason')
    def rejection_reason_preview(self, obj):
        return (obj.rejection_reason or '')[:80]
