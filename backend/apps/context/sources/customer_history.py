"""customer_history — prior evidence for the same customer.

Emits one fact under `customerProfile.history`: a compact rollup of how
many prior interactions we have on record, when the most recent one was,
and which source systems the customer has touched. Reads only what
adapters already persisted; no inference, no LLM.
"""

from __future__ import annotations

from apps.context.engine import register
from apps.context.engine.base import (
    CUSTOMER_PROFILE,
    BaseContextSource,
    ContextRequest,
    SourceOutput,
)
from apps.context.sources._lookup import match_evidence


MAX_RECENT = 5


@register
class CustomerHistorySource(BaseContextSource):
    name = 'customer_history'
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
            total = qs.count()
            if total == 0:
                return empty

            recent = list(qs[:MAX_RECENT])
            source_systems = sorted({row.source_system for row in recent})
            evidence_types = sorted({row.evidence_type for row in recent})

            history = {
                'total_prior_interactions': total,
                'source_systems': source_systems,
                'evidence_types': evidence_types,
                'most_recent_at': recent[0].occurred_at.isoformat() if recent[0].occurred_at else None,
                'recent': [
                    {
                        'source_system': row.source_system,
                        'evidence_type': row.evidence_type,
                        'external_id': row.external_id,
                        'occurred_at': row.occurred_at.isoformat() if row.occurred_at else None,
                        'outcome': row.outcome or '',
                    }
                    for row in recent
                ],
            }
            # Confidence scales with history. Capped at 0.9; a pattern-
            # matching source never claims 1.0.
            confidence = min(0.9, 0.3 + 0.15 * total)
            return SourceOutput(
                source=self.name,
                priority=self.priority,
                confidence=confidence,
                facts={CUSTOMER_PROFILE: {'history': history}},
            )
        except Exception:
            return empty
