"""Callio adapter — voice call transcripts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from django.conf import settings
from django.utils.dateparse import parse_datetime

from apps.learning.adapters._http import http_fetch_list, load_fixture
from apps.learning.adapters.base import EvidenceSourceAdapter, register
from apps.learning.adapters.dto import Evidence


@register
class CallioAdapter(EvidenceSourceAdapter):
    source_system = 'callio'
    fixture_filename = 'callio_sample.json'

    def fetch_since(self, since: datetime | None) -> Iterable[Evidence]:
        url = getattr(settings, 'CALLIO_LEARNING_URL', '')
        token = getattr(settings, 'CALLIO_LEARNING_TOKEN', '')
        if url and token:
            records = http_fetch_list(url, token, since)
            source_url = url
        else:
            records = load_fixture(self.fixture_filename)
            source_url = f'fixture://{self.fixture_filename}'
        for record in records:
            yield self._to_evidence(record, source_url)

    def _to_evidence(self, record: dict[str, Any], source_url: str) -> Evidence:
        return Evidence(
            source_system=self.source_system,
            evidence_type='call',
            external_id=str(record['call_id']),
            occurred_at=parse_datetime(record.get('ended_at') or record.get('started_at') or ''),
            source_payload={
                'duration_seconds': record.get('duration_seconds'),
                'lead': record.get('lead', {}),
                'transcript': record.get('transcript', []),
            },
            outcome=record.get('outcome'),
            metadata={'source_url': source_url},
            business_rules_version=record.get('playbook_version', ''),
        )
