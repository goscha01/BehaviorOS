"""LeadBridge adapter — Thumbtack/Yelp chat conversations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from django.utils.dateparse import parse_datetime

from apps.learning.adapters._http import http_fetch_list, load_fixture
from apps.learning.adapters.base import EvidenceSourceAdapter, register, resolve_credentials
from apps.learning.adapters.dto import Evidence


@register
class LeadBridgeAdapter(EvidenceSourceAdapter):
    source_system = 'leadbridge'
    fixture_filename = 'leadbridge_sample.json'

    def fetch_since(self, since: datetime | None, *, org=None) -> Iterable[Evidence]:
        creds = resolve_credentials(self.source_system, org=org)
        if creds.source in ('integration', 'settings'):
            records = http_fetch_list(creds.url, creds.token, since)
            source_url = creds.url
        else:
            records = load_fixture(self.fixture_filename)
            source_url = f'fixture://{self.fixture_filename}'
        for record in records:
            yield self._to_evidence(record, source_url)

    def _to_evidence(self, record: dict[str, Any], source_url: str) -> Evidence:
        return Evidence(
            source_system=self.source_system,
            evidence_type='conversation',
            external_id=str(record['conversation_id']),
            occurred_at=parse_datetime(record.get('closed_at') or record.get('created_at') or ''),
            source_payload={
                'channel': record.get('channel', ''),
                'lead': record.get('lead', {}),
                'messages': record.get('messages', []),
            },
            outcome=record.get('outcome'),
            metadata={'source_url': source_url},
            business_rules_version=record.get('playbook_version', ''),
        )
