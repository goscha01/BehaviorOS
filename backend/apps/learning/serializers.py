"""DRF serializers for the review-queue API.

Two shapes for suggestions:
- `LearningSuggestionListSerializer`: compact card for the list view.
- `LearningSuggestionDetailSerializer`: full detail that answers the
  four questions the UI must present on every suggestion page:
    1. Why was this generated?  → reason_generated
    2. How many supporting conversations?  → supporting_conversations
    3. What business outcome supports it?  → outcome_distribution
    4. What would change if implemented?  → proposed_changes

A separate `SupportingEvidenceSerializer` powers
`/supporting-evidence/` — never named "examples" in the API.

Request serializers (Approve/Reject/Measure) live here too so the
view is thin. RejectSerializer enforces the required reason at the
API boundary; the lifecycle service enforces it again as a belt.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.learning.models import (
    CandidateRecommendation,
    EvidenceInsight,
    LearningJob,
    LearningSuggestion,
    SourceIntegration,
)


class LearningSuggestionListSerializer(serializers.ModelSerializer):
    class Meta:
        model = LearningSuggestion
        fields = (
            'id', 'title', 'category', 'status',
            'confidence', 'supporting_count',
            'created_at', 'updated_at',
        )
        read_only_fields = fields


class SupportingEvidenceSerializer(serializers.ModelSerializer):
    """One CandidateRecommendation + its source evidence, shaped for the
    dashboard's "Supporting Evidence" section (never called "examples")."""

    source_system = serializers.CharField(source='evidence.source_system', read_only=True)
    evidence_type = serializers.CharField(source='evidence.evidence_type', read_only=True)
    external_id = serializers.CharField(source='evidence.external_id', read_only=True)
    occurred_at = serializers.DateTimeField(source='evidence.occurred_at', read_only=True)
    business_rules_version = serializers.CharField(
        source='evidence.source_business_rules_version', read_only=True
    )
    evidence_summary = serializers.CharField(source='evidence.ai_summary', read_only=True)

    class Meta:
        model = CandidateRecommendation
        fields = (
            'id', 'kind', 'category',
            'title', 'description', 'llm_confidence', 'outcome_signal',
            'source_system', 'evidence_type', 'external_id',
            'occurred_at', 'business_rules_version', 'evidence_summary',
            'created_at',
        )
        read_only_fields = fields


class LearningSuggestionDetailSerializer(serializers.ModelSerializer):
    """Answers the four questions every suggestion page must present.

    Not just the model dump — includes derived fields the UI would
    otherwise have to compute per-page.
    """

    reason_generated = serializers.SerializerMethodField()
    supporting_conversations = serializers.SerializerMethodField()
    outcome_distribution = serializers.SerializerMethodField()
    proposed_changes = serializers.SerializerMethodField()

    class Meta:
        model = LearningSuggestion
        fields = (
            'id', 'title', 'description', 'category', 'status',
            'confidence', 'confidence_breakdown',
            'supporting_count', 'representative_examples',
            'synthesis_json', 'synthesis_model', 'synthesis_prompt_version',
            'publish_targets', 'impact_json',
            'review_note', 'reviewed_at',
            'created_at', 'updated_at',
            # Derived — the four questions.
            'reason_generated',
            'supporting_conversations',
            'outcome_distribution',
            'proposed_changes',
        )
        read_only_fields = fields

    def get_reason_generated(self, obj: LearningSuggestion) -> dict:
        breakdown = obj.confidence_breakdown or {}
        return {
            'summary': (obj.synthesis_json or {}).get('supporting_evidence_summary', ''),
            'confidence_breakdown': breakdown,
            'synthesis_model': obj.synthesis_model,
            'synthesis_prompt_version': obj.synthesis_prompt_version,
        }

    def get_supporting_conversations(self, obj: LearningSuggestion) -> dict:
        candidates_qs = obj.candidate_recommendations.select_related('evidence')
        distinct_evidence = (
            candidates_qs.values_list('evidence_id', flat=True).distinct().count()
        )
        return {
            'candidate_count': obj.supporting_count,
            'distinct_evidence_count': distinct_evidence,
            'representative': obj.representative_examples or {},
        }

    def get_outcome_distribution(self, obj: LearningSuggestion) -> dict:
        distribution = {'positive': 0, 'negative': 0, 'neutral': 0}
        for signal in obj.candidate_recommendations.values_list('outcome_signal', flat=True):
            distribution[signal] = distribution.get(signal, 0) + 1
        total = sum(distribution.values())
        majority = max(distribution.values()) if distribution else 0
        consistency = (majority / total) if total else 0.0
        return {
            'counts': distribution,
            'consistency': round(consistency, 3),
        }

    def get_proposed_changes(self, obj: LearningSuggestion) -> dict:
        synth = obj.synthesis_json or {}
        return {
            'playbook_change': synth.get('suggested_playbook_change', ''),
            'faq_addition': synth.get('suggested_faq_addition', ''),
            'why_this_matters': synth.get('why_this_matters', ''),
            'publish_targets': obj.publish_targets or [],
        }


class LearningJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = LearningJob
        fields = (
            'id', 'status', 'triggered_by',
            'started_at', 'completed_at',
            'evidence_processed', 'evidence_skipped', 'suggestions_created',
            'cost_usd', 'error',
            'created_at',
        )
        read_only_fields = fields


# ---- Request bodies ----

class ApproveSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default='')
    publish_to = serializers.ListField(
        child=serializers.CharField(max_length=64),
        required=False,
        allow_empty=True,
        default=list,
        help_text='Channel keys, e.g. ["leadbridge_playbook", "callio_voice_rules"]. '
                  'Persisted on the suggestion so a future publisher can act on it.',
    )


class RejectSerializer(serializers.Serializer):
    reason = serializers.CharField(
        required=True, allow_blank=False, min_length=1, max_length=4000,
        help_text='Required. Persisted on the RejectedSuggestionSignature for '
                  'future analyzer prompting.',
    )


class MarkImplementedSerializer(serializers.Serializer):
    publish_receipts = serializers.DictField(
        required=False, allow_empty=True, default=dict,
        help_text='Opaque receipts from downstream publishers (or human-entered '
                  'confirmation for Phase 1).',
    )


class MarkMeasuredSerializer(serializers.Serializer):
    impact = serializers.DictField(
        required=True, allow_empty=False,
        help_text='Before/after metrics, e.g. '
                  '{"win_rate_before": 0.42, "win_rate_after": 0.51, "sample_size": 320}.',
    )


class SourceIntegrationSerializer(serializers.ModelSerializer):
    token_preview = serializers.SerializerMethodField()

    class Meta:
        model = SourceIntegration
        fields = (
            'id', 'source_system', 'url',
            # token itself is write-only; we surface only a preview on read.
            'token_preview',
            'is_active',
            'last_synced_at', 'last_sync_status', 'last_sync_error',
            'last_sync_created', 'last_sync_updated',
            'created_at', 'updated_at',
        )
        read_only_fields = (
            'id', 'token_preview',
            'last_synced_at', 'last_sync_status', 'last_sync_error',
            'last_sync_created', 'last_sync_updated',
            'created_at', 'updated_at',
        )

    def get_token_preview(self, obj: SourceIntegration) -> str:
        if not obj.token:
            return ''
        # Only expose the last 4 chars so the UI can confirm which token is
        # active without leaking the whole secret over the read path.
        return f'••••{obj.token[-4:]}'


class SourceIntegrationUpsertSerializer(serializers.Serializer):
    """Write-side serializer. Accepts a token; never echoes it back."""
    source_system = serializers.CharField(max_length=64)
    url = serializers.CharField(
        max_length=500, allow_blank=True, required=False, default='',
    )
    token = serializers.CharField(
        max_length=512, allow_blank=True, required=False, default='',
        # Blank on update = keep existing token.
    )
    is_active = serializers.BooleanField(required=False, default=True)
