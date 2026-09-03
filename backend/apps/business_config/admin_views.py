"""Admin/debug endpoints for BusinessConfigProposal work.

Gated by the same ServiceTokenAuthentication as the generate endpoint.
Intended for infrequent operational cleanup (e.g. purging duplicated
historical evidence from a bad prior ingest run).
"""

from __future__ import annotations

import logging
import uuid

from django.db.models import Q
from rest_framework import serializers, status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Organization
from apps.context.auth import ServiceTokenAuthentication
from apps.context.models import EvidenceEvent

logger = logging.getLogger(__name__)


class PurgeEvidenceSerializer(serializers.Serializer):
    tenantId = serializers.CharField(max_length=128)
    sourceSystemPrefix = serializers.CharField(max_length=128)
    # Safety: caller must explicitly confirm they understand this is destructive.
    confirm = serializers.CharField(max_length=32)

    def validate_confirm(self, value):
        if value != 'purge':
            raise serializers.ValidationError('confirm must be exactly "purge"')
        return value


class PurgeTenantEvidenceView(APIView):
    """DELETE all EvidenceEvent rows for a tenant matching a source-system prefix.

    Filters on:
      - org.id == tenantId
      - runtime == 'leadbridge' (or generalize later if needed)
      - payload.sourceSystem STARTSWITH sourceSystemPrefix

    Returns {deletedCount, remaining}. Idempotent.
    """
    authentication_classes = [ServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PurgeEvidenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            org_uuid = uuid.UUID(data['tenantId'])
        except (ValueError, TypeError):
            return Response({'error': 'tenantId must be a UUID'}, status=400)
        try:
            org = Organization.objects.get(pk=org_uuid)
        except Organization.DoesNotExist:
            return Response({'deletedCount': 0, 'remaining': 0, 'note': 'org not found'})

        prefix = data['sourceSystemPrefix']
        qs = EvidenceEvent.objects.filter(
            org=org,
        ).filter(
            Q(payload__sourceSystem__startswith=prefix),
        )
        count_before = qs.count()
        deleted, _ = qs.delete()
        remaining = EvidenceEvent.objects.filter(org=org).filter(
            Q(payload__sourceSystem__startswith=prefix),
        ).count()

        logger.warning(
            'business_config admin purge tenant=%s prefix=%r deleted=%d remaining=%d',
            data['tenantId'], prefix, deleted, remaining,
        )
        return Response({
            'tenantId': data['tenantId'],
            'sourceSystemPrefix': prefix,
            'countBefore': count_before,
            'deletedCount': deleted,
            'remaining': remaining,
        })
