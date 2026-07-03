from rest_framework import serializers
from apps.billing.models import Subscription


class SubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subscription
        fields = (
            'id', 'plan', 'status', 'current_period_end',
            'cancel_at_period_end', 'created_at',
        )
        read_only_fields = fields


class CreateCheckoutSerializer(serializers.Serializer):
    plan = serializers.ChoiceField(choices=['starter', 'pro'])
    success_url = serializers.URLField()
    cancel_url = serializers.URLField()


class CreatePortalSerializer(serializers.Serializer):
    return_url = serializers.URLField()
