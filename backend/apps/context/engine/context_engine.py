"""ContextEngine — the facade the view calls.

Wraps ContextMerger + registry + learning hooks + wire projection into a
single `build(request) -> EngineResult` entry point. Never raises — the
view can call it unconditionally and always get back a shapely EngineResult.

Wire projection lives here because it's the boundary between "provenance-
carrying internal package" and "wire-compatible response body." Keeping it
in one place makes it easy to audit that provenance never leaks to
runtimes.
"""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal

from django.utils import timezone

from apps.context.engine.base import (
    BUSINESS_INSIGHTS,
    CONVERSATION_HINTS,
    CUSTOMER_PROFILE,
    ContextRequest,
    EngineResult,
    MergedContext,
    RECOMMENDED_STRATEGY,
    WARNINGS,
)
from apps.context.engine.learning_hooks import mutate_result, notify_context_built
from apps.context.engine.merger import ContextMerger
from apps.context.engine.registry import iter_registered
from apps.context.models import ContextRequestLog


logger = logging.getLogger(__name__)


class ContextEngine:
    """The runtime-facing composition brain.

    Public methods:
    - `build(request)` — runs all Sources, merges, applies learning hooks,
      returns EngineResult. Never raises.
    - `wire_projection(merged)` — strips provenance and shapes into the
      five wire slots. Kept as a class method so external code (dashboards,
      shadow analysis) can call it against a stored MergedContext without
      re-running the engine.
    """

    # Bumped when the algorithm changes in a way that would make a stored
    # MergedContext incomparable with a fresh one. Also exported at the
    # engine-package `__init__` as CONTEXT_VERSION.
    CONTEXT_VERSION = '2.0'

    def build(self, request: ContextRequest) -> EngineResult:
        started = time.perf_counter()
        try:
            merger = ContextMerger()
            merged, source_results, overall_confidence = merger.run(
                request, iter_registered(),
            )
        except Exception:
            # Defense in depth — merger shouldn't raise, but if a future
            # bug slips through we return a clean empty result.
            logger.exception('ContextMerger.run failed')
            merged = MergedContext()
            source_results = []
            overall_confidence = 0.0

        wire_context = self.wire_projection(merged)
        status = 'context' if wire_context else 'no_context'
        # An empty wire_context means status = no_context. Normalize so
        # downstream logging + wire response agree.
        if not wire_context:
            wire_context = {}

        latency_ms = int((time.perf_counter() - started) * 1000)

        result = EngineResult(
            status=status,
            confidence=overall_confidence if status == 'context' else 0.0,
            wire_context=wire_context,
            merged=merged,
            source_results=source_results,
            latency_ms=latency_ms,
            context_version=self.CONTEXT_VERSION,
            generated_at=timezone.now(),
        )

        # Learning module extension points. Both are no-ops today.
        notify_context_built(request, result)
        try:
            result = mutate_result(request, result)
        except Exception:
            logger.exception('mutate_result failed')

        return result

    # --- Wire projection --------------------------------------------------

    @classmethod
    def wire_projection(cls, merged: MergedContext) -> dict:
        """Strip provenance, produce the five documented wire slots.

        Returns an empty dict when the MergedContext has nothing — callers
        use that to decide status = no_context.
        """
        if merged.is_empty():
            return {}

        wire: dict = {
            CUSTOMER_PROFILE: {},
            BUSINESS_INSIGHTS: {},
            CONVERSATION_HINTS: [],
            WARNINGS: [],
            RECOMMENDED_STRATEGY: {},
        }

        for slot in (CUSTOMER_PROFILE, BUSINESS_INSIGHTS):
            slot_dict = merged.facts.get(slot) or {}
            for fact_name, wrapped in slot_dict.items():
                wire[slot][fact_name] = _unwrap_value(wrapped)

        hints = merged.facts.get(CONVERSATION_HINTS) or []
        for item in hints:
            wire[CONVERSATION_HINTS].append(_strip_provenance_from_list_item(item))

        rec = merged.recommendations.get(RECOMMENDED_STRATEGY) or {}
        for rec_name, wrapped in rec.items():
            wire[RECOMMENDED_STRATEGY][rec_name] = _unwrap_value(wrapped)

        for item in merged.warnings:
            wire[WARNINGS].append(_strip_provenance_from_list_item(item))

        return wire


def _unwrap_value(wrapped):
    """Return `wrapped["value"]` if present; otherwise the wrapper itself.

    Defensive — a hand-built MergedContext might not have provenance-
    wrapped values, and we shouldn't lose data just because.
    """
    if isinstance(wrapped, dict) and 'value' in wrapped and 'source' in wrapped:
        return wrapped['value']
    return wrapped


_RESERVED_PROVENANCE_KEYS = ('_source', '_confidence', '_generated_at')


def _strip_provenance_from_list_item(item):
    if not isinstance(item, dict):
        return item
    return {k: v for k, v in item.items() if k not in _RESERVED_PROVENANCE_KEYS}


# --- Logging helper --------------------------------------------------------

def log_context_request(
    *,
    request: ContextRequest,
    result: EngineResult,
    request_payload: dict,
    returned_to_runtime: bool,
) -> ContextRequestLog | None:
    """Persist one ContextRequestLog row. Never raises.

    Logging failure MUST NOT bubble to the runtime — a DB blip should not
    turn a `no_context` into a 500. Fall through to None; Grafana request
    logs will still show the endpoint hit.
    """
    try:
        context_size = 0
        if result.wire_context:
            context_size = len(json.dumps(result.wire_context, default=str).encode('utf-8'))

        source_results_payload = [
            {
                'name': r.source,
                'priority': r.priority,
                'contributed': r.contributed,
                'confidence': r.confidence,
                'latency_ms': r.latency_ms,
                'error': r.error,
            }
            for r in result.source_results
        ]

        return ContextRequestLog.objects.create(
            org=request.org,
            tenant_id=request.tenant_id or '',
            runtime=request.runtime or '',
            channel=request.channel or '',
            event_type=request.event_type or '',
            customer_id=request.customer_id or '',
            lead_id=request.lead_id or '',
            conversation_id=request.conversation_id or '',
            request_payload=request_payload or {},
            status=result.status,
            confidence=Decimal(str(result.confidence)),
            context_size_bytes=context_size,
            latency_ms=result.latency_ms,
            source_results=source_results_payload,
            returned_to_runtime=returned_to_runtime,
            context_version=result.context_version,
            source_count=result.source_count,
        )
    except Exception:
        logger.exception('Failed to persist ContextRequestLog')
        return None
