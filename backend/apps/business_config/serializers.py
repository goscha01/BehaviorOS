from rest_framework import serializers


class GenerateProposalSerializer(serializers.Serializer):
    tenantId = serializers.CharField(max_length=128)
    templateKey = serializers.CharField(max_length=128)
    templateId = serializers.CharField(max_length=128)
    templateSnapshot = serializers.DictField()
    currentTenantSnapshot = serializers.DictField()
    domains = serializers.ListField(
        child=serializers.CharField(max_length=32),
        required=False,
        default=list,
    )

    def validate_domains(self, value):
        if not value:
            return ['pricing', 'faq', 'services', 'policies']
        allowed = {'pricing', 'faq', 'services', 'policies'}
        bad = [d for d in value if d not in allowed]
        if bad:
            raise serializers.ValidationError(
                f'unsupported domain(s): {bad} (allowed: {sorted(allowed)})'
            )
        return value
