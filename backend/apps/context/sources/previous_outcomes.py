"""previous_outcomes — outcome breakdown across the customer's prior evidence."""

from __future__ import annotations

from collections import Counter

from apps.context.engine import register
from apps.context.engine.base import (
    BUSINESS_INSIGHTS,
    BaseContextSource,
    ContextRequest,
    SourceOutput,
)
from apps.context.sources._lookup import match_evidence


@register
class PreviousOutcomesSource(BaseContextSource):
    name = 'previous_outcomes'
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
            outcomes = [row.outcome for row in qs if row.outcome]
            if not outcomes:
                return empty

            counts = Counter(outcomes)
            total = sum(counts.values())
            most_common, most_common_count = counts.most_common(1)[0]
            dominant_ratio = most_common_count / total if total else 0.0

            breakdown = {
                'total': total,
                'counts': dict(counts),
                'dominant': most_common,
                'dominant_ratio': round(dominant_ratio, 3),
            }
            confidence = min(0.85, 0.3 + 0.5 * dominant_ratio)
            return SourceOutput(
                source=self.name,
                priority=self.priority,
                confidence=confidence,
                facts={BUSINESS_INSIGHTS: {'outcome_breakdown': breakdown}},
            )
        except Exception:
            return empty
