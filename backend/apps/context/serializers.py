"""Request serializer for POST /v1/context.

Response is hand-shaped in the view — it's a fixed schema and we don't
want DRF field metadata (help_text, style hints) leaking to runtime callers.

Field naming: the canonical field names are `organizationId` and `product`
(matches the Phase-1 Callio integration contract). The legacy names
`tenantId` and `runtime` are still accepted for backward compatibility
with the LeadBridge shadow client — both shapes normalize to the same
internal `ContextRequest`. New callers should use the canonical names.
"""

from __future__ import annotations

from rest_framework import serializers


VALID_PRODUCTS = ('leadbridge', 'callio', 'serviceflow')
VALID_CHANNELS = ('sms', 'voice', 'browser', 'email', '')
VALID_EVENT_TYPES = (
    'new_lead', 'customer_reply', 'outbound_call', 'inbound_call',
    'call_completed', '',
)

# The endpoint is one door with two modes. `lookup` (default) runs the full
# context pipeline and returns advisory context. `report` runs the same
# ingestion pipeline (evidence persisted, aggregates updated, hooks fire)
# but skips context generation — used by runtimes to feed back post-call
# outcomes. Same auth, same telemetry, same rate limiting.
MODE_LOOKUP = 'lookup'
MODE_REPORT = 'report'
VALID_MODES = (MODE_LOOKUP, MODE_REPORT)


class ContextRequestSerializer(serializers.Serializer):
    # --- Canonical identity fields (preferred) --------------------------
    organizationId = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default='',
    )
    product = serializers.CharField(
        max_length=32, required=False, allow_blank=True, default='',
    )
    workspaceId = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default='',
    )
    sourceSystem = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default='',
    )
    sourceAccount = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default='',
    )

    # --- Legacy identity fields (backward-compatible aliases) -----------
    tenantId = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default='',
    )
    runtime = serializers.CharField(
        max_length=32, required=False, allow_blank=True, default='',
    )

    # --- Mode + correlation --------------------------------------------
    mode = serializers.CharField(
        max_length=16, required=False, allow_blank=True, default='',
    )
    contextRequestId = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default='',
    )

    # --- Interaction fields (unchanged) ---------------------------------
    channel = serializers.CharField(
        max_length=32, required=False, allow_blank=True, default='',
    )
    eventType = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default='',
    )
    customerId = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default='',
    )
    leadId = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default='',
    )
    conversationId = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default='',
    )
    message = serializers.CharField(
        required=False, allow_blank=True, default='',
        # No length cap — runtimes can send full customer messages.
        # Storage is bounded by the request body itself, not this field.
        trim_whitespace=False,
    )
    metadata = serializers.DictField(
        required=False, default=dict, allow_empty=True,
    )

    def validate_channel(self, value: str) -> str:
        return value.lower().strip()

    def validate_eventType(self, value: str) -> str:
        return value.strip()

    def validate(self, attrs):
        # Normalize canonical vs legacy identity fields. Canonical (organizationId,
        # product) wins if both are provided.
        canonical_tenant = (attrs.get('organizationId') or '').strip() \
            or (attrs.get('tenantId') or '').strip()
        if not canonical_tenant:
            raise serializers.ValidationError({
                'organizationId': 'This field is required (legacy alias: tenantId).',
            })
        attrs['tenantId'] = canonical_tenant

        canonical_product = (attrs.get('product') or '').strip().lower() \
            or (attrs.get('runtime') or '').strip().lower()
        if not canonical_product:
            raise serializers.ValidationError({
                'product': 'This field is required (legacy alias: runtime).',
            })
        # Accept unknown values — contract is "never error because context
        # doesn't exist." Unknown product just means no source matches.
        attrs['runtime'] = canonical_product

        # Fold new top-level metadata into the metadata dict so downstream
        # consumers (ContextRequest.metadata, Sources, ContextRequestLog)
        # see a single flat namespace. Existing metadata keys are not
        # overwritten.
        metadata = dict(attrs.get('metadata') or {})
        for key in ('workspaceId', 'sourceSystem', 'sourceAccount'):
            value = (attrs.get(key) or '').strip()
            if value:
                metadata.setdefault(key, value)
        attrs['metadata'] = metadata

        # Normalize mode. Empty ⇒ lookup (backward-compat). Unknown values
        # are rejected — the report path materially changes the response
        # shape, so silently defaulting an unknown mode to lookup would
        # be worse than a 400.
        mode = (attrs.get('mode') or '').strip().lower() or MODE_LOOKUP
        if mode not in VALID_MODES:
            raise serializers.ValidationError({
                'mode': f'Must be one of {VALID_MODES}.',
            })
        attrs['mode'] = mode

        # Correlation ID is caller-supplied or generated in the view.
        # Trim here; the view fills in a default when blank.
        attrs['contextRequestId'] = (attrs.get('contextRequestId') or '').strip()

        return attrs
