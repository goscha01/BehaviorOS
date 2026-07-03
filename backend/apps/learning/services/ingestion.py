"""Evidence ingestion — the only component that writes to EvidenceInsight.

Adapters yield Evidence DTOs; this service normalizes them into ORM rows,
upserts on (source_system, external_id), and isolates per-source failures
so one broken adapter doesn't stop the others.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from django.conf import settings
from django.db import transaction

from apps.accounts.models import Organization
from apps.learning.adapters import get_adapter, iter_registered
from apps.learning.adapters.base import EvidenceSourceAdapter
from apps.learning.adapters.dto import Evidence
from apps.learning.models import EvidenceInsight, LearningJob

logger = logging.getLogger(__name__)


@dataclass
class SourceResult:
    source_system: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    error: str = ''

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped


@dataclass
class IngestionRun:
    per_source: dict[str, SourceResult] = field(default_factory=dict)

    @property
    def total_created(self) -> int:
        return sum(r.created for r in self.per_source.values())

    @property
    def total_updated(self) -> int:
        return sum(r.updated for r in self.per_source.values())

    @property
    def total_skipped(self) -> int:
        return sum(r.skipped for r in self.per_source.values())


class EvidenceIngestionService:
    """Pulls evidence from adapters and persists it as EvidenceInsight rows.

    Contract:
    - Adapters are trusted to yield well-shaped Evidence.
    - This service is the only writer to EvidenceInsight.
    - Upserts on (source_system, external_id) — safe to re-run.
    - Adapter exceptions are caught per source; other adapters continue.
    """

    def __init__(self, org: Organization, job: LearningJob | None = None):
        self.org = org
        self.job = job

    def ingest_source(
        self,
        adapter: EvidenceSourceAdapter,
        since: datetime | None = None,
    ) -> SourceResult:
        result = SourceResult(source_system=adapter.source_system)
        try:
            evidences: Iterable[Evidence] = adapter.fetch_since(since)
            for evidence in evidences:
                created = self._upsert(evidence)
                if created:
                    result.created += 1
                else:
                    result.updated += 1
        except Exception as exc:  # per-source isolation; log + record + move on
            logger.exception('Adapter %s failed during ingestion', adapter.source_system)
            result.error = f'{type(exc).__name__}: {exc}'
        return result

    def ingest_all_enabled(self, since: datetime | None = None) -> IngestionRun:
        """Fetch from every source listed in LEARNING_ENABLED_SOURCES.

        Unknown source keys are logged and skipped (a typo shouldn't stop
        the whole run). Registered-but-not-enabled adapters are silently
        skipped — that's a config choice, not an error.
        """
        run = IngestionRun()
        enabled = getattr(settings, 'LEARNING_ENABLED_SOURCES', [])
        for source_system in enabled:
            try:
                adapter = get_adapter(source_system)
            except LookupError as exc:
                logger.warning('Skipping unknown source in LEARNING_ENABLED_SOURCES: %s', exc)
                run.per_source[source_system] = SourceResult(
                    source_system=source_system, error=str(exc)
                )
                continue
            run.per_source[source_system] = self.ingest_source(adapter, since=since)
        return run

    @staticmethod
    def list_registered_sources() -> list[str]:
        return [name for name, _cls in iter_registered()]

    @transaction.atomic
    def _upsert(self, evidence: Evidence) -> bool:
        """Return True if a new row was created, False if updated."""
        outcome_status = ''
        outcome_metadata: dict = {}
        if evidence.outcome:
            outcome_metadata = dict(evidence.outcome)
            outcome_status = str(outcome_metadata.get('status', ''))

        defaults = {
            'org': self.org,
            'evidence_type': evidence.evidence_type,
            'occurred_at': evidence.occurred_at,
            'outcome': outcome_status,
            'outcome_metadata': outcome_metadata,
            'source_business_rules_version': evidence.business_rules_version,
            'source_payload': dict(evidence.source_payload),
            'ingest_metadata': dict(evidence.metadata),
        }
        if self.job is not None:
            # Only stamp the job on first creation — don't rewrite the
            # provenance of a re-ingested record.
            defaults_on_create = {**defaults, 'job': self.job}
        else:
            defaults_on_create = defaults

        obj, created = EvidenceInsight.objects.update_or_create(
            source_system=evidence.source_system,
            external_id=evidence.external_id,
            defaults=defaults,
            create_defaults=defaults_on_create,
        )
        return created
