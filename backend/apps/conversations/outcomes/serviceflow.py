"""ServiceFlow outcome fetcher.

Required ServiceFlow endpoint (not yet implemented on SF side):

    POST /api/v1/learning/entities/outcomes
    Authorization: Bearer <SERVICEFLOW_LEARNING_TOKEN>
    Content-Type: application/json

    Body:
      {"entities": [
        {"type": "opportunity", "id": "sf-opp-uuid"},
        {"type": "job",         "id": "sf-job-uuid"},
        ...
      ]}

    Response 200:
      {
        "outcomes": [
          {
            "type":  "opportunity",
            "id":    "sf-opp-uuid",
            "opportunity_status": "won" | "lost" | "open" | ...,
            "booked":     true,
            "completed":  false,
            "cancelled":  false,
            "revenue_cents": 22000,     // integer cents (not dollars)
            "recurring":     true,
            "job_count":     3
          }
        ]
      }

    Missing entities are absent from `outcomes`. Never 404 for "not found".

Fields that only make sense at aggregate level (job_count, recurring)
are typically set on the top-level customer or opportunity outcome, not
each downstream job. Callers merge across returned rows into a single
OutcomeSnapshot.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings

from apps.conversations.outcomes.base import (
    BaseServiceFlowOutcomeFetcher,
    ServiceFlowOutcome,
)

logger = logging.getLogger(__name__)


class InMemoryServiceFlowOutcomeFetcher(BaseServiceFlowOutcomeFetcher):
    def __init__(self):
        # Keyed by (entity_type, entity_id).
        self._store: dict[tuple[str, str], ServiceFlowOutcome] = {}

    def register(self, outcome: ServiceFlowOutcome) -> None:
        self._store[(outcome.sf_entity_type, outcome.sf_entity_id)] = outcome

    def fetch_outcomes(
        self, entities: list[tuple[str, str]]
    ) -> list[ServiceFlowOutcome]:
        results = []
        for key in entities:
            if key in self._store:
                results.append(self._store[key])
        return results


class HttpServiceFlowOutcomeFetcher(BaseServiceFlowOutcomeFetcher):
    ENDPOINT_PATH = '/api/v1/learning/entities/outcomes'

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self._base_url = base_url or getattr(settings, 'SERVICEFLOW_LEARNING_URL', '')
        self._token = token or getattr(settings, 'SERVICEFLOW_LEARNING_TOKEN', '')
        self._timeout = timeout

    def fetch_outcomes(
        self, entities: list[tuple[str, str]]
    ) -> list[ServiceFlowOutcome]:
        if not entities or not self._base_url or not self._token:
            return []

        payload_entities = [
            {'type': etype, 'id': eid} for (etype, eid) in entities
        ]

        try:
            import requests

            resp = requests.post(
                self._base_url.rstrip('/') + self.ENDPOINT_PATH,
                json={'entities': payload_entities},
                headers={
                    'Authorization': f'Bearer {self._token}',
                    'X-BehaviorOS-Client': 'conversations-sf-outcomes',
                    'Accept': 'application/json',
                },
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning('sf outcome fetch failed: %s', exc)
            return []

        if resp.status_code == 404:
            logger.warning(
                'sf outcome fetcher: %s returned 404 — endpoint not yet '
                'implemented on ServiceFlow side. All snapshots will show '
                'empty SF fields until it ships. Contract in this file.',
                self._base_url + self.ENDPOINT_PATH,
            )
            return []
        if resp.status_code >= 400:
            logger.warning(
                'sf outcome fetcher: HTTP %d, returning no outcomes',
                resp.status_code,
            )
            return []

        try:
            body = resp.json()
        except ValueError:
            return []

        results: list[ServiceFlowOutcome] = []
        for row in body.get('outcomes', []):
            if not isinstance(row, dict) or 'id' not in row or 'type' not in row:
                continue
            results.append(ServiceFlowOutcome(
                sf_entity_type=str(row['type']),
                sf_entity_id=str(row['id']),
                opportunity_status=str(row.get('opportunity_status') or ''),
                booked=_coerce_bool(row.get('booked')),
                completed=_coerce_bool(row.get('completed')),
                cancelled=_coerce_bool(row.get('cancelled')),
                revenue_cents=_coerce_int(row.get('revenue_cents')),
                recurring=_coerce_bool(row.get('recurring')),
                job_count=_coerce_int(row.get('job_count')),
                raw=row,
            ))
        return results


def _coerce_bool(value) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.lower().strip()
        if v in ('true', 'yes', '1'):
            return True
        if v in ('false', 'no', '0'):
            return False
    return None


def _coerce_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
