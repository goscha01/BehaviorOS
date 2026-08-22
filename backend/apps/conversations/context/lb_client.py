"""HTTP client for LB's canonical lead-context endpoint.

Mirrors the pattern of `apps.conversations.outcomes.leadbridge` —
same bearer-token auth (`LEADBRIDGE_LEARNING_TOKEN`), same batch
shape, same never-raise fault handling. Contract lives on the LB
side at `src/learning/lead-context-extractor.ts` + the controller
route `POST /api/v1/learning/leads/context`.

Design invariants:
  * NEVER re-parse Thumbtack/Yelp payloads on the BOS side. This
    file only knows how to call the endpoint and normalize its
    response into BOS-side dataclasses.
  * A missing lead in the response is not an error — the resolver
    treats it as "no source-derived attributes for that lead."
  * Failures (network, 4xx/5xx, malformed JSON) → empty list +
    WARN log. Pricing / analyzers keep functioning against
    conversation-only evidence.
  * Batching: callers assemble one `fetch(lead_ids=[...])` per
    reconstruction run rather than one call per conversation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from django.conf import settings

from apps.conversations.context.types import (
    Attr,
    Authority,
    Observation,
    _parse_iso,
)


logger = logging.getLogger(__name__)


ENDPOINT_PATH = '/api/v1/learning/leads/context'


@dataclass
class LbLeadContext:
    """One lead's canonical context as returned by LB. Observations are
    materialized in `observations_for()` so we can plug attributes
    directly into the precedence resolver without an intermediate
    Python-shape juggle.
    """

    lead_id: str
    platform: str
    external_request_id: Optional[str]
    observed_at: datetime
    updated_at: datetime
    mapping_version: str
    # Raw attribute envelopes as returned by LB, keyed by canonical
    # attribute name. Value is `None` when LB had no source data for
    # that attribute — carry the null through so the resolver can
    # distinguish "LB knows nothing" from "we didn't call LB".
    attributes_raw: dict[str, Optional[dict[str, Any]]]

    def observations_for(self, lb_user_id: str) -> list[Observation]:
        """Materialize this lead's attributes as `Observation`s, ready to
        flow into the precedence resolver. Each observation carries the
        LB `updated_at` as its `observed_at` so time-aware precedence
        can compare LB updates to a later conversation correction.
        """
        results: list[Observation] = []
        for attr, envelope in self.attributes_raw.items():
            if envelope is None:
                continue
            value = envelope.get('value')
            if value is None:
                continue
            source_field = envelope.get('source_field') or 'unknown'
            # LB's `source_field` starts with either 'lead_details:...'
            # (Thumbtack survey or Yelp survey parsed by LB), 'raw.*'
            # (top-level payload field), or 'raw.project.*' (Yelp
            # project block). All three are structured source data as
            # far as BOS is concerned — LB's parser handled them.
            authority = Authority.SOURCE_STRUCTURED
            if 'project.job_names' in source_field:
                # Yelp job_names is derived (LB inferred the tier from
                # the category label) rather than a direct survey answer.
                authority = Authority.SOURCE_DERIVED
            results.append(Observation(
                attribute=attr,
                value=value,
                source='leadbridge',
                source_field=f'lb_lead:{self.lead_id}#{source_field}',
                observed_at=self.updated_at,
                authority=authority,
                raw_value=(
                    str(envelope.get('raw_value'))
                    if envelope.get('raw_value') is not None else None
                ),
                derivation=envelope.get('derivation'),
                source_version=(
                    f'lb-user:{lb_user_id}|mapping:{self.mapping_version}'
                ),
            ))
        return results


class LeadBridgeContextClient:
    """HTTP client for the LB `/leads/context` endpoint."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        lb_user_id: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self._base_url = base_url or getattr(
            settings, 'LEADBRIDGE_LEARNING_URL', '',
        )
        self._token = token or getattr(
            settings, 'LEADBRIDGE_LEARNING_TOKEN', '',
        )
        self._lb_user_id = lb_user_id
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._token and self._lb_user_id)

    def fetch(self, lead_ids: Iterable[str]) -> list[LbLeadContext]:
        ids = [lid for lid in lead_ids if lid]
        if not ids or not self.configured:
            return []

        try:
            import requests

            resp = requests.post(
                self._base_url.rstrip('/') + ENDPOINT_PATH,
                json={'userId': self._lb_user_id, 'lead_ids': ids},
                headers={
                    'Authorization': f'Bearer {self._token}',
                    'X-BehaviorOS-Client': 'conversations-lb-context',
                    'Accept': 'application/json',
                },
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — never raise from a resolver
            logger.warning('lb-context fetch failed: %s', exc)
            return []

        if resp.status_code == 404:
            logger.warning(
                'lb-context: %s returned 404 — endpoint not yet '
                'deployed on LB main. All conversations will use '
                'conversation-only evidence until LB ships.',
                self._base_url + ENDPOINT_PATH,
            )
            return []
        if resp.status_code >= 400:
            logger.warning(
                'lb-context: HTTP %d, returning no contexts', resp.status_code,
            )
            return []

        try:
            body = resp.json()
        except ValueError:
            logger.warning('lb-context: response not JSON')
            return []

        results: list[LbLeadContext] = []
        for row in body.get('leads', []):
            if not isinstance(row, dict) or 'lead_id' not in row:
                continue
            try:
                results.append(LbLeadContext(
                    lead_id=str(row['lead_id']),
                    platform=str(row.get('platform') or ''),
                    external_request_id=(
                        row.get('external_request_id') or None
                    ),
                    observed_at=_parse_iso(row['observed_at']),
                    updated_at=_parse_iso(row['updated_at']),
                    mapping_version=str(
                        row.get('mapping_version') or 'unknown'
                    ),
                    attributes_raw=dict(row.get('attributes') or {}),
                ))
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    'lb-context: malformed row for lead=%s: %s',
                    row.get('lead_id'), exc,
                )
                continue
        return results


class InMemoryLeadBridgeContextClient:
    """Test double — callers register lead contexts by lead_id."""

    def __init__(self):
        self._store: dict[str, LbLeadContext] = {}

    def register(self, ctx: LbLeadContext) -> None:
        self._store[ctx.lead_id] = ctx

    @property
    def configured(self) -> bool:
        return True

    def fetch(self, lead_ids: Iterable[str]) -> list[LbLeadContext]:
        results = []
        for lid in lead_ids:
            if lid in self._store:
                results.append(self._store[lid])
        return results


# Convenience: for each canonical attribute name, which JSON key does
# LB use? For the current v1 mapping these are 1:1, but keeping this
# indirection means BOS can adapt if the mapping evolves.
_LB_ATTRIBUTE_MAP: dict[str, str] = {
    Attr.SERVICE: 'service',
    Attr.SERVICE_TIER: 'service_tier',
    Attr.BEDROOMS: 'bedrooms',
    Attr.BATHROOMS: 'bathrooms',
    Attr.SQUARE_FOOTAGE: 'square_footage',
    Attr.FREQUENCY: 'frequency',
    Attr.ADDONS: 'addons',
}
