"""previous_objections — loss reasons the customer expressed before.

Emits one hint per distinct prior objection reason (e.g. "price",
"unclear_service_fit") so the runtime LLM can proactively address them.
Reads only `outcome_metadata.loss_reason` that adapters already persist.
"""

from __future__ import annotations

from apps.context.engine import register
from apps.context.engine.base import (
    CONVERSATION_HINTS,
    BaseContextSource,
    ContextRequest,
    SourceOutput,
)
from apps.context.sources._lookup import match_evidence


@register
class PreviousObjectionsSource(BaseContextSource):
    name = 'previous_objections'
    priority = 50

    def provide(self, request: ContextRequest) -> SourceOutput:
        empty = SourceOutput(source=self.name, priority=self.priority, confidence=0.0)
        try:
            qs = match_evidence(
                org=request.org,
                customer_id=request.customer_id,
                lead_id=request.lead_id,
                conversation_id=request.conversation_id,
            )
            hints: list[dict] = []
            seen: set[str] = set()
            for row in qs:
                loss_reason = (row.outcome_metadata or {}).get('loss_reason')
                if not loss_reason or loss_reason in seen:
                    continue
                seen.add(loss_reason)
                hints.append({
                    'kind': 'previous_objection',
                    'reason': loss_reason,
                    'source_system': row.source_system,
                    'occurred_at': row.occurred_at.isoformat() if row.occurred_at else None,
                })
            if not hints:
                return empty

            confidence = min(0.85, 0.5 + 0.1 * len(hints))
            return SourceOutput(
                source=self.name,
                priority=self.priority,
                confidence=confidence,
                facts={CONVERSATION_HINTS: hints},
            )
        except Exception:
            return empty
