"""Serializers for the BehaviorOS Insights API.

Contract-shaped: everything an LB/runtime consumer needs to display a
recommendation, and NOTHING internal (no ontology-version, no
inference_version leak beyond a single label, no raw event ordinals,
no LLM cost figures).
"""

from __future__ import annotations

from rest_framework import serializers

from apps.conversations.models import (
    BehaviorRecommendation, RecommendationLifecycleState,
    RecommendationRun, TenantConfigSnapshot,
)


class LifecycleSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecommendationLifecycleState
        fields = [
            'state', 'dismissal_reason', 'dismissal_note',
            'state_changed_at', 'state_changed_by',
        ]
        read_only_fields = fields


class RecommendationSerializer(serializers.ModelSerializer):
    """Full read shape for one recommendation. Includes the lifecycle
    substructure (created lazily by the view if missing) so the LB UI
    can render Accept/Dismiss without a second round-trip."""

    id = serializers.UUIDField(read_only=True)
    lifecycle = serializers.SerializerMethodField()
    run_id = serializers.UUIDField(source='run.pk', read_only=True)

    class Meta:
        model = BehaviorRecommendation
        fields = [
            'id',
            'run_id',
            'recommendation_id',
            'rec_class',
            'confidence',
            'subject_state',
            'subject_signals',
            'linked_transition',
            'linked_policy_ids',
            'observation',
            'interpretation',
            'proposed_action_scope',
            'proposed_action',
            'limitations',
            'evidence',
            'supporting_conversation_ids',
            'created_at',
            'lifecycle',
        ]
        read_only_fields = fields

    def get_lifecycle(self, obj):
        # Show the lifecycle state if it exists. Never trigger a
        # write here (that happens in the dedicated lifecycle
        # endpoint); serialize a NEW-state stub if absent.
        lc = getattr(obj, 'lifecycle', None)
        if lc is None:
            return {
                'state': RecommendationLifecycleState.State.NEW,
                'dismissal_reason': '',
                'dismissal_note': '',
                'state_changed_at': None,
                'state_changed_by': '',
            }
        return LifecycleSerializer(lc).data


class RecommendationSummarySerializer(serializers.ModelSerializer):
    """Compact shape for list views. Skips the heavier evidence blob
    (still available via detail retrieve) to keep list responses small."""

    id = serializers.UUIDField(read_only=True)
    lifecycle_state = serializers.SerializerMethodField()

    class Meta:
        model = BehaviorRecommendation
        fields = [
            'id',
            'recommendation_id',
            'rec_class',
            'confidence',
            'subject_state',
            'subject_signals',
            'proposed_action_scope',
            'observation',
            'lifecycle_state',
        ]
        read_only_fields = fields

    def get_lifecycle_state(self, obj):
        lc = getattr(obj, 'lifecycle', None)
        return lc.state if lc else RecommendationLifecycleState.State.NEW


class ConfigSnapshotBriefSerializer(serializers.ModelSerializer):
    """Enough config-snapshot context to identify the run without
    exposing raw config contents."""
    class Meta:
        model = TenantConfigSnapshot
        fields = [
            'id', 'source_system', 'tenant_external_id',
            'service_group', 'contract_version', 'created_at',
        ]
        read_only_fields = fields


class RecommendationRunSummarySerializer(serializers.ModelSerializer):
    """List view of runs. Groups + counts by class so the client can
    render a quick summary without pulling all recommendations."""

    id = serializers.UUIDField(read_only=True)
    config_snapshot = ConfigSnapshotBriefSerializer(read_only=True)
    counts_by_class = serializers.SerializerMethodField()
    counts_by_confidence = serializers.SerializerMethodField()

    class Meta:
        model = RecommendationRun
        fields = [
            'id',
            'synthesizer_version',
            'llm_model',
            'status',
            'started_at',
            'completed_at',
            'recommendations_generated',
            'config_snapshot',
            'counts_by_class',
            'counts_by_confidence',
        ]
        read_only_fields = fields

    def get_counts_by_class(self, obj):
        from collections import Counter
        return dict(Counter(
            r.rec_class for r in obj.recommendations.all()
        ))

    def get_counts_by_confidence(self, obj):
        from collections import Counter
        return dict(Counter(
            r.confidence for r in obj.recommendations.all()
        ))


class RecommendationProposalSerializer(serializers.ModelSerializer):
    """Read shape for a proposal. The consumer (LB) uses this to
    render a Preview screen and compute its own config diff."""

    id = serializers.UUIDField(read_only=True)
    recommendation_id = serializers.UUIDField(source='recommendation.pk', read_only=True)
    tenant_external_id = serializers.CharField(
        source='config_snapshot.tenant_external_id', read_only=True,
    )
    config_snapshot_id = serializers.UUIDField(
        source='config_snapshot.pk', read_only=True,
    )

    class Meta:
        model = None   # set below
        fields = [
            'id',
            'recommendation_id',
            'tenant_external_id',
            'config_snapshot_id',
            'config_snapshot_hash',
            'target_system',
            'change_type',
            'condition',
            'scope',
            'proposed_behavior_summary',
            'proposed_behavior_detail',
            'status',
            'consumer_applied_at',
            'consumer_error',
            'generator_version',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields


# Fill in the model reference here (import cycle avoidance)
from apps.conversations.models import RecommendationProposal as _RP  # noqa: E402
RecommendationProposalSerializer.Meta.model = _RP


class ProposalStatusUpdateRequestSerializer(serializers.Serializer):
    """Consumer-side status update body. LB calls this after Apply
    or when it detects stale config."""

    status = serializers.ChoiceField(
        choices=_RP.Status.choices,
    )
    error = serializers.CharField(
        required=False, allow_blank=True, default='',
    )


class LifecycleTransitionRequestSerializer(serializers.Serializer):
    """Payload for POST /recommendations/<id>/lifecycle."""

    state = serializers.ChoiceField(
        choices=RecommendationLifecycleState.State.choices,
    )
    actor = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default='',
        help_text='Who is making the transition (usually LB user email)',
    )
    reason = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default='',
    )
    note = serializers.CharField(
        required=False, allow_blank=True, default='',
    )
