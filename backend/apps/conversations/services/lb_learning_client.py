"""HTTP client for the LeadBridge `GET /api/v1/learning/leads` endpoint.

Used only by the LB-anchored importer (`import_lb_learning_dataset`).
Separate from `HttpLeadBridgeResolver` (which is per-conversation lookup);
this one is a bulk cursor-paginated fetch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Iterator, Optional

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LearningLeadOutcome:
    lead_id: str
    status: str
    engaged: bool
    booked: bool
    lost: bool
    cancelled: bool


@dataclass(frozen=True)
class LearningLead:
    lead_id: str
    user_id: str
    customer_phone: Optional[str]
    customer_phone_substitute: Optional[str]
    customer_email: Optional[str]
    platform: str
    external_request_id: str
    business_id: Optional[str]
    created_at: str
    updated_at: str
    outcome: LearningLeadOutcome
    raw: dict = field(default_factory=dict)


class LbLearningClient:
    ENDPOINT_PATH = '/api/v1/learning/leads'

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        lb_user_id: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self._base_url = (base_url or getattr(settings, 'LEADBRIDGE_LEARNING_URL', '')).rstrip('/')
        self._token = token or getattr(settings, 'LEADBRIDGE_LEARNING_TOKEN', '')
        self._lb_user_id = lb_user_id
        self._timeout = timeout

    def iter_leads(
        self,
        *,
        statuses: Optional[Iterable[str]] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        page_size: int = 100,
        max_leads: Optional[int] = None,
    ) -> Iterator[LearningLead]:
        """Paginate through eligible LB leads, yielding one at a time.

        `max_leads` short-circuits after N (useful for validation runs).
        `statuses` maps to repeated `?status=` query params; None uses
        LB's DEFAULT_LEARNING_STATUSES.
        """
        if not (self._base_url and self._token and self._lb_user_id):
            logger.warning(
                'lb learning client: not configured '
                '(base_url/token/lb_user_id missing)'
            )
            return

        import requests

        session = requests.Session()
        headers = {
            'Authorization': f'Bearer {self._token}',
            'X-BehaviorOS-Client': 'conversations-lb-learning-client',
            'Accept': 'application/json',
        }
        base_params = [
            ('userId', self._lb_user_id),
            ('limit', str(page_size)),
        ]
        if since:
            base_params.append(('since', since.isoformat()))
        if until:
            base_params.append(('until', until.isoformat()))
        if statuses:
            for s in statuses:
                base_params.append(('status', s))

        cursor: Optional[str] = None
        yielded = 0
        while True:
            params = list(base_params)
            if cursor:
                params.append(('cursor', cursor))
            try:
                resp = session.get(
                    self._base_url + self.ENDPOINT_PATH,
                    params=params, headers=headers, timeout=self._timeout,
                )
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning('lb learning client: request failed: %s', exc)
                return

            for row in body.get('leads', []):
                lead = _to_lead(row)
                if lead is None:
                    continue
                yield lead
                yielded += 1
                if max_leads is not None and yielded >= max_leads:
                    return

            cursor = body.get('next_cursor')
            if not cursor:
                return


def _to_lead(row: dict) -> Optional[LearningLead]:
    try:
        oc = row.get('outcome') or {}
        return LearningLead(
            lead_id=str(row['lead_id']),
            user_id=str(row.get('user_id') or ''),
            customer_phone=row.get('customer_phone'),
            customer_phone_substitute=row.get('customer_phone_substitute'),
            customer_email=row.get('customer_email'),
            platform=str(row.get('platform') or ''),
            external_request_id=str(row.get('external_request_id') or ''),
            business_id=row.get('business_id'),
            created_at=str(row.get('created_at') or ''),
            updated_at=str(row.get('updated_at') or ''),
            outcome=LearningLeadOutcome(
                lead_id=str(oc.get('lead_id') or row['lead_id']),
                status=str(oc.get('status') or ''),
                engaged=bool(oc.get('engaged')),
                booked=bool(oc.get('booked')),
                lost=bool(oc.get('lost')),
                cancelled=bool(oc.get('cancelled')),
            ),
            raw=row,
        )
    except (KeyError, TypeError) as exc:
        logger.warning('lb learning client: dropping malformed row: %s', exc)
        return None
