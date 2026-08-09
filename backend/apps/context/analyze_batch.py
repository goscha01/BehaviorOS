"""POST /v1/context/analyze-batch — on-demand conversation analysis.

Thin wrapper around the existing `ConversationAnalyzer`. Given a batch of
normalized conversations, runs the analyzer on each and returns the raw
per-conversation analysis JSON.

Unlike `/v1/context` (which persists EvidenceEvents and updates aggregates),
this endpoint DOES NOT WRITE to the durable learning dataset. Every
`EvidenceInsight` used to feed the analyzer is instantiated in memory and
discarded after the call.

The caller (currently MockCustomer's Quo import) folds these per-conversation
analyses into whatever aggregate shape it needs — this endpoint is not
opinionated about that.
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

from django.conf import settings
from rest_framework import serializers, status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.context.auth import ServiceTokenAuthentication
from apps.learning.analyzers.base import get_analyzer_for
from apps.learning.models import EvidenceInsight
from apps.learning.services.llm_client import LearningLLMClient

logger = logging.getLogger(__name__)

MAX_CONVERSATIONS = 100
MAX_MESSAGES_PER_CONVERSATION = 500
# Parallel worker cap. The Anthropic SDK is sync, so we thread-fan-out to
# avoid the naive N×~5s serial timeline that trips gunicorn's timeout on
# batches of 10+ real conversations. Anthropic Tier-2 has plenty of RPM
# headroom for concurrency of this size; if we hit 429s we'll back off.
ANALYZER_MAX_WORKERS = 10


class MessageSerializer(serializers.Serializer):
    role = serializers.CharField(max_length=32)
    text = serializers.CharField(allow_blank=True, trim_whitespace=False)


class ConversationSerializer(serializers.Serializer):
    id = serializers.CharField(max_length=128, required=False, allow_blank=True)
    channel = serializers.CharField(max_length=32, required=False, allow_blank=True)
    source_system = serializers.CharField(max_length=64, required=False, allow_blank=True)
    business_rules_version = serializers.CharField(
        max_length=64, required=False, allow_blank=True,
    )
    outcome = serializers.CharField(max_length=64, required=False, allow_blank=True)
    outcome_metadata = serializers.DictField(required=False)
    lead = serializers.DictField(required=False)
    messages = MessageSerializer(many=True)

    def validate_messages(self, value):
        if len(value) > MAX_MESSAGES_PER_CONVERSATION:
            raise serializers.ValidationError(
                f'Too many messages ({len(value)}); max {MAX_MESSAGES_PER_CONVERSATION}.',
            )
        return value


class AnalyzeBatchSerializer(serializers.Serializer):
    conversations = ConversationSerializer(many=True)

    def validate_conversations(self, value):
        if not value:
            raise serializers.ValidationError('conversations must be non-empty.')
        if len(value) > MAX_CONVERSATIONS:
            raise serializers.ValidationError(
                f'Too many conversations ({len(value)}); max {MAX_CONVERSATIONS}.',
            )
        return value


class AnalyzeBatchView(APIView):
    """POST /v1/context/analyze-batch — analyze N conversations, persist nothing.

    Response invariants:
    - 200 with `{results: [...], total_cost_usd, model_used}`.
    - 400 if the request body doesn't validate.
    - 401 if the service token is misconfigured.
    - Individual conversation failures do NOT fail the batch; each result
      carries either `analysis` or `error`.
    """

    authentication_classes = [ServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = AnalyzeBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversations = serializer.validated_data['conversations']

        model = getattr(
            settings, 'LEARNING_ANALYSIS_MODEL', 'claude-haiku-4-5-20251001',
        )
        analyzer = get_analyzer_for('conversation')
        llm = LearningLLMClient()

        batch_id = 'anab_' + uuid.uuid4().hex[:16]
        started = time.monotonic()
        total_cost = Decimal('0')
        successes = 0
        failures = 0

        # Pre-build ephemeral insights per conversation. Deterministic order
        # preserved via idx→client_id map so results ship back in input order.
        tasks = []
        for idx, conv in enumerate(conversations):
            client_id = conv.get('id') or f'conv_{idx}'
            insight = self._build_ephemeral_insight(conv)
            tasks.append((idx, client_id, insight))

        def analyze_one(item):
            idx, client_id, insight = item
            try:
                res = analyzer.analyze(insight, llm, model)
                return idx, client_id, res, None
            except Exception as exc:  # noqa: BLE001
                return idx, client_id, None, exc

        # Fan out — Anthropic calls dominate the batch wall time; running
        # them sequentially blows past gunicorn's timeout on real batches.
        max_workers = min(ANALYZER_MAX_WORKERS, len(tasks))
        indexed_results = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for future in as_completed(pool.submit(analyze_one, t) for t in tasks):
                idx, client_id, res, exc = future.result()
                if exc is not None:
                    failures += 1
                    indexed_results[idx] = {
                        'id': client_id,
                        'error': type(exc).__name__,
                    }
                    logger.warning(
                        'analyze-batch conversation failed batch=%s idx=%d id=%s error=%s',
                        batch_id, idx, client_id, type(exc).__name__,
                    )
                else:
                    successes += 1
                    total_cost += res.cost_usd
                    indexed_results[idx] = {
                        'id': client_id,
                        'analysis': res.analysis_json,
                        'cost_usd': str(res.cost_usd),
                        'model_used': res.model_used,
                        'prompt_version': res.prompt_version,
                    }

        results = [r for r in indexed_results if r is not None]
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # Never log message bodies — only IDs, counts, timing, model, cost.
        logger.info(
            'analyze-batch batch=%s count=%d ok=%d fail=%d cost_usd=%s '
            'model=%s latency_ms=%d',
            batch_id, len(conversations), successes, failures,
            str(total_cost), model, elapsed_ms,
        )

        return Response(
            {
                'batchId': batch_id,
                'results': results,
                'totalCostUsd': str(total_cost),
                'modelUsed': model,
                'latencyMs': elapsed_ms,
            },
            status=http_status.HTTP_200_OK,
        )

    def _build_ephemeral_insight(self, conv: dict) -> EvidenceInsight:
        """Build an unsaved EvidenceInsight for the analyzer to read.

        `ConversationAnalyzer.build_prompts` reads only:
          - insight.source_payload (messages, channel, lead)
          - insight.source_system
          - insight.source_business_rules_version
          - insight.outcome_metadata

        Nothing else on the model is dereferenced by the analyzer path, so
        we can safely skip fields that would normally be required at
        persistence time (org, external_id, occurred_at).
        """
        payload = {
            'messages': [
                {'role': m.get('role', ''), 'text': m.get('text', '')}
                for m in conv.get('messages', [])
            ],
            'channel': conv.get('channel', ''),
            'lead': conv.get('lead', {}),
        }
        return EvidenceInsight(
            source_system=conv.get('source_system') or 'quo',
            source_payload=payload,
            outcome=conv.get('outcome', ''),
            outcome_metadata=conv.get('outcome_metadata', {}) or {},
            source_business_rules_version=conv.get('business_rules_version', ''),
            evidence_type=EvidenceInsight.EvidenceType.CONVERSATION,
        )
