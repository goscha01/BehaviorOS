"""Historical import — same pipeline, different entry point.

The Phase 3 spec insists that imported conversations produce IDENTICAL
Evidence Events to live runtime requests. Achieving that literally
means: no import-specific logic, just build DTOs and hand them to the
same `EvidencePipeline`.

Import records must supply an `external_id` — that's what makes reruns
idempotent. Aggregates auto-heal because they're rebuildable, but the
event table is authoritative; duplicating rows there would poison
future counts.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable, Mapping

from django.utils.dateparse import parse_datetime

from apps.context.models import EvidenceEvent
from apps.context.pipeline.events import EvidenceEventDTO, MESSAGE_EXCERPT_MAX
from apps.context.pipeline.pipeline import EvidencePipeline


logger = logging.getLogger(__name__)


class HistoricalImportService:
    """Feeds historical records through the same EvidencePipeline as live requests.

    `import_records(org, source_system, records)` accepts any iterable of
    dicts. Each dict must have at least `external_id` + `occurred_at`;
    everything else is optional and defaults sensibly.
    """

    def __init__(self, pipeline: EvidencePipeline | None = None):
        self._pipeline = pipeline or EvidencePipeline()

    def import_records(
        self,
        *,
        org,
        source_system: str,
        records: Iterable[Mapping],
    ) -> dict:
        """Returns a summary dict — {total, persisted, skipped, errors}."""
        summary = {'total': 0, 'persisted': 0, 'skipped': 0, 'errors': 0}
        for record in records:
            summary['total'] += 1
            try:
                dto = self._record_to_dto(org, source_system, record)
            except Exception as exc:
                summary['errors'] += 1
                logger.warning(
                    'HistoricalImportService: bad record for source=%s external_id=%s: %s',
                    source_system, record.get('external_id', '?'), exc,
                )
                continue

            # `build_context=False` — no runtime is waiting for a response
            # on historical replay, and we don't want log spam on the
            # ContextRequestLog table either.
            result = self._pipeline.handle_evidence_dto(
                dto, build_context=False,
            )
            if result.persisted:
                summary['persisted'] += 1
            else:
                summary['skipped'] += 1
        return summary

    @staticmethod
    def _record_to_dto(org, source_system: str, record: Mapping) -> EvidenceEventDTO:
        external_id = str(record.get('external_id') or '').strip()
        if not external_id:
            raise ValueError('external_id is required for historical imports')

        occurred_at = record.get('occurred_at')
        if isinstance(occurred_at, str):
            occurred_at = parse_datetime(occurred_at)
        if not isinstance(occurred_at, datetime):
            raise ValueError('occurred_at must be a datetime or ISO-8601 string')

        message = str(record.get('message') or '')[:MESSAGE_EXCERPT_MAX]

        return EvidenceEventDTO(
            org=org,
            source_kind=EvidenceEvent.SourceKind.HISTORICAL,
            runtime=str(record.get('runtime') or source_system),
            channel=str(record.get('channel') or ''),
            event_type=str(record.get('event_type') or ''),
            customer_id=str(record.get('customer_id') or ''),
            lead_id=str(record.get('lead_id') or ''),
            conversation_id=str(record.get('conversation_id') or ''),
            external_id=external_id,
            occurred_at=occurred_at,
            message_excerpt=message,
            payload=dict(record.get('payload') or record),
        )
