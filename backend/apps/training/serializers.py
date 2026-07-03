from rest_framework import serializers
from apps.training.models import (
    BusinessProfile,
    ScenarioTemplate,
    Script,
    TrainingSession,
    SessionTurn,
    SessionResult,
)


class BusinessProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessProfile
        fields = (
            'id', 'name', 'service_desc', 'policies', 'pricing_notes',
            'hours', 'coverage_area', 'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')


class ScenarioTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScenarioTemplate
        fields = (
            'id', 'name', 'system_prompt', 'difficulty', 'intent',
            'rubric', 'is_default', 'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')


class ScriptSerializer(serializers.ModelSerializer):
    class Meta:
        model = Script
        fields = ('id', 'name', 'content', 'version', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')


class SessionTurnSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionTurn
        fields = ('id', 'speaker', 'text', 'audio_url', 'metadata', 'created_at')
        read_only_fields = fields


class SessionResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionResult
        fields = ('id', 'outcome', 'signals', 'notes', 'created_at')
        read_only_fields = fields


class TrainingSessionSerializer(serializers.ModelSerializer):
    turns = SessionTurnSerializer(many=True, read_only=True)
    result = SessionResultSerializer(read_only=True)
    business_profile_name = serializers.CharField(
        source='business_profile.name', read_only=True, default=None
    )
    scenario_name = serializers.CharField(
        source='scenario_template.name', read_only=True, default=None
    )
    script_name = serializers.CharField(
        source='script.name', read_only=True, default=None
    )

    class Meta:
        model = TrainingSession
        fields = (
            'id', 'business_profile', 'scenario_template', 'script',
            'business_profile_name', 'scenario_name', 'script_name',
            'status', 'started_at', 'ended_at', 'created_at',
            'turns', 'result',
        )
        read_only_fields = ('id', 'status', 'started_at', 'ended_at', 'created_at')


class TrainingSessionListSerializer(serializers.ModelSerializer):
    business_profile_name = serializers.CharField(
        source='business_profile.name', read_only=True, default=None
    )
    scenario_name = serializers.CharField(
        source='scenario_template.name', read_only=True, default=None
    )
    has_result = serializers.SerializerMethodField()

    class Meta:
        model = TrainingSession
        fields = (
            'id', 'business_profile_name', 'scenario_name',
            'status', 'started_at', 'ended_at', 'created_at', 'has_result',
        )

    def get_has_result(self, obj):
        return hasattr(obj, 'result')


class SubmitTurnSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=2000)
