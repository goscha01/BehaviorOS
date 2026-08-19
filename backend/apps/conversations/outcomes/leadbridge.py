"""LeadBridge outcome fetcher.

In-memory backend for tests / dev. HTTP backend calls the shipped LB
endpoint (commit b69eae1b in the LB repo, at `src/learning/`).

    POST /api/v1/learning/leads/outcomes
    Authorization: Bearer <LEADBRIDGE_LEARNING_TOKEN>
    Content-Type: application/json

    Body:  {"userId": "<lb-user-uuid>", "lead_ids": ["lb-uuid-1", ...]}

    Response 200:
      {
        "outcomes": [
          {
            "lead_id":  "lb-uuid-1",
            "status":   "booked" | "quoted" | "lost" | "new" | ...,
            "engaged":  true,
            "booked":   true,
            "lost":     false,
            "cancelled": false
          }
        ]
      }

    Missing leads are simply absent from `outcomes`. Never 404.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from django.conf import settings

from apps.conversations.outcomes.base import (
    BaseLeadBridgeOutcomeFetcher,
    LeadBridgeOutcome,
)

logger = logging.getLogger(__name__)


class InMemoryLeadBridgeOutcomeFetcher(BaseLeadBridgeOutcomeFetcher):
    def __init__(self):
        self._store: dict[str, LeadBridgeOutcome] = {}

    def register(self, outcome: LeadBridgeOutcome) -> None:
        self._store[outcome.lb_lead_id] = outcome

    def fetch_outcomes(self, lead_ids: Iterable[str]) -> list[LeadBridgeOutcome]:
        result = []
        for lid in lead_ids:
            if lid in self._store:
                result.append(self._store[lid])
        return result


class HttpLeadBridgeOutcomeFetcher(BaseLeadBridgeOutcomeFetcher):
    ENDPOINT_PATH = '/api/v1/learning/leads/outcomes'

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        lb_user_id: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self._base_url = base_url or getattr(settings, 'LEADBRIDGE_LEARNING_URL', '')
        self._token = token or getattr(settings, 'LEADBRIDGE_LEARNING_TOKEN', '')
        # LB's tenant key. See HttpLeadBridgeResolver docstring.
        self._lb_user_id = lb_user_id
        self._timeout = timeout

    def fetch_outcomes(self, lead_ids: Iterable[str]) -> list[LeadBridgeOutcome]:
        lead_ids = [lid for lid in lead_ids if lid]
        if not lead_ids or not self._base_url or not self._token or not self._lb_user_id:
            return []

        try:
            import requests

            resp = requests.post(
                self._base_url.rstrip('/') + self.ENDPOINT_PATH,
                json={'userId': self._lb_user_id, 'lead_ids': lead_ids},
                headers={
                    'Authorization': f'Bearer {self._token}',
                    'X-BehaviorOS-Client': 'conversations-lb-outcomes',
                    'Accept': 'application/json',
                },
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning('lb outcome fetch failed: %s', exc)
            return []

        if resp.status_code == 404:
            logger.warning(
                'lb outcome fetcher: %s returned 404 — endpoint not yet '
                'implemented on LB side. All snapshots will show empty LB '
                'fields until it ships. Contract in this file.',
                self._base_url + self.ENDPOINT_PATH,
            )
            return []
        if resp.status_code >= 400:
            logger.warning(
                'lb outcome fetcher: HTTP %d, returning no outcomes',
                resp.status_code,
            )
            return []

        try:
            body = resp.json()
        except ValueError:
            return []

        results: list[LeadBridgeOutcome] = []
        for row in body.get('outcomes', []):
            if not isinstance(row, dict) or 'lead_id' not in row:
                continue
            results.append(LeadBridgeOutcome(
                lb_lead_id=str(row['lead_id']),
                status=str(row.get('status') or ''),
                engaged=_coerce_bool(row.get('engaged')),
                booked=_coerce_bool(row.get('booked')),
                lost=_coerce_bool(row.get('lost')),
                cancelled=_coerce_bool(row.get('cancelled')),
                raw=row,
            ))
        return results


def _coerce_bool(value) -> Optional[bool]:
    """Return None only when the value is genuinely missing — accept
    False as a real answer, not "unknown."
    """
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
