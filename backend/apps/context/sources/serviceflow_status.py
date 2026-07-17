"""serviceflow_status — most recent ServiceFlow job for this customer.

Priority is a touch higher than the customer-history sources: job status
is authoritative operational data (ServiceFlow is the system of record
for jobs), and if two sources ever touch the same fact this should win.
"""

from __future__ import annotations

from apps.context.engine import register
from apps.context.engine.base import (
    BUSINESS_INSIGHTS,
    BaseContextSource,
    ContextRequest,
    SourceOutput,
)
from apps.context.sources._lookup import match_evidence


@register
class ServiceFlowStatusSource(BaseContextSource):
    name = 'serviceflow_status'
    priority = 60

    def provide(self, request: ContextRequest) -> SourceOutput:
        empty = SourceOutput(source=self.name, priority=self.priority, confidence=0.0)
        try:
            qs = match_evidence(
                org=request.org,
                customer_id=request.customer_id,
                lead_id=request.lead_id,
                conversation_id=request.conversation_id,
            ).filter(source_system='serviceflow')

            latest = qs.first()
            if latest is None:
                return empty

            payload = latest.source_payload or {}
            sf_status = {
                'job_id': payload.get('job_id') or latest.external_id,
                'service_type': payload.get('service_type', ''),
                'status': payload.get('status', latest.outcome or ''),
                'revenue': payload.get('revenue'),
                'recurring': payload.get('recurring'),
                'cancelled_reason': payload.get('cancelled_reason'),
                'occurred_at': latest.occurred_at.isoformat() if latest.occurred_at else None,
            }

            warnings = []
            cancelled_reason = payload.get('cancelled_reason')
            if cancelled_reason:
                warnings.append({
                    'kind': 'prior_cancellation',
                    'detail': f'Previous ServiceFlow job cancelled: {cancelled_reason}.',
                })

            return SourceOutput(
                source=self.name,
                priority=self.priority,
                confidence=0.7,
                facts={BUSINESS_INSIGHTS: {'serviceflow_status': sf_status}},
                warnings=warnings,
            )
        except Exception:
            return empty
