"""ServiceFlow adapter — booking + operational outcomes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from django.conf import settings
from django.utils.dateparse import parse_datetime

from apps.learning.adapters._http import http_fetch_list, load_fixture
from apps.learning.adapters.base import EvidenceSourceAdapter, register
from apps.learning.adapters.dto import Evidence


@register
class ServiceFlowAdapter(EvidenceSourceAdapter):
    source_system = 'serviceflow'
    fixture_filename = 'serviceflow_sample.json'

    def fetch_since(self, since: datetime | None) -> Iterable[Evidence]:
        url = getattr(settings, 'SERVICEFLOW_LEARNING_URL', '')
        token = getattr(settings, 'SERVICEFLOW_LEARNING_TOKEN', '')
        if url and token:
            records = http_fetch_list(url, token, since)
            source_url = url
        else:
            records = load_fixture(self.fixture_filename)
            source_url = f'fixture://{self.fixture_filename}'
        for record in records:
            yield self._to_evidence(record, source_url)

    def _to_evidence(self, record: dict[str, Any], source_url: str) -> Evidence:
        outcome = {
            'status': record.get('status', ''),
            'recurring': record.get('recurring', False),
        }
        if 'revenue' in record:
            outcome['revenue'] = record['revenue']
        if record.get('cancelled_reason'):
            outcome['cancelled_reason'] = record['cancelled_reason']
        if record.get('review'):
            outcome['review'] = record['review']

        return Evidence(
            source_system=self.source_system,
            evidence_type='outcome',
            external_id=str(record['job_id']),
            occurred_at=parse_datetime(record.get('completed_at') or record.get('created_at') or ''),
            source_payload={
                'service_type': record.get('service_type', ''),
                'customer_id': record.get('customer_id', ''),
                'created_at': record.get('created_at'),
                'completed_at': record.get('completed_at'),
            },
            outcome=outcome,
            metadata={'source_url': source_url},
            business_rules_version=record.get('rules_version', ''),
        )
