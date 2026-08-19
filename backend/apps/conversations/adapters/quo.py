"""Quo conversation source adapter.

Two backends supported today:

1. **Fixture** (default when no LB proxy URL configured) — reads JSON
   files from `apps/conversations/adapters/fixtures/quo/`. This is what
   Phase 3 ships with and what the automated test suite uses.

2. **HTTP (via LeadBridge proxy)** — wired in Phase 10 once the LB
   endpoint `GET /api/v1/learning/quo/conversations` is live. BehaviorOS
   never talks to Quo directly; LB owns the per-user Quo credentials.

Selection is driven by settings:

    LEADBRIDGE_QUO_PROXY_URL   — empty → fixture mode, set → HTTP mode
    LEADBRIDGE_QUO_PROXY_TOKEN — bearer token for the LB proxy endpoint

Fixture directory layout:

    fixtures/quo/
        voice_call_with_transcript.json
        inbound_sms.json
        outbound_sms.json
        multi_message_thread.json
        missing_transcript.json
        missing_phone.json
        duplicate_source_record.json    # deliberately duplicates another
        partial_record.json             # deliberately missing fields
        _index.json                     # optional: overrides load order
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator, Mapping, Optional

from django.conf import settings

from apps.conversations.adapters.base import ConversationSourceAdapter

logger = logging.getLogger(__name__)

FIXTURE_ROOT = Path(__file__).resolve().parent / 'fixtures' / 'quo'


class QuoAdapter(ConversationSourceAdapter):
    source = 'quo'

    def __init__(
        self,
        *,
        proxy_url: Optional[str] = None,
        proxy_token: Optional[str] = None,
        fixture_root: Optional[Path] = None,
    ):
        # Empty string sentinel — treat as "not configured" so an unset
        # env var and an explicitly empty override behave the same.
        self._proxy_url = (
            proxy_url
            if proxy_url is not None
            else getattr(settings, 'LEADBRIDGE_QUO_PROXY_URL', '')
        )
        self._proxy_token = (
            proxy_token
            if proxy_token is not None
            else getattr(settings, 'LEADBRIDGE_QUO_PROXY_TOKEN', '')
        )
        self._fixture_root = fixture_root or FIXTURE_ROOT

    def fetch_records(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Iterator[Mapping]:
        if self._proxy_url:
            yield from self._fetch_http(
                since=since, until=until, limit=limit, cursor=cursor,
            )
        else:
            yield from self._fetch_fixture(
                since=since, until=until, limit=limit,
            )

    # ------------------------------------------------------------------
    # Fixture backend
    # ------------------------------------------------------------------

    def _fetch_fixture(
        self,
        *,
        since: Optional[datetime],
        until: Optional[datetime],
        limit: Optional[int],
    ) -> Iterator[Mapping]:
        if not self._fixture_root.exists():
            logger.warning(
                'quo adapter: fixture root %s does not exist, no records yielded',
                self._fixture_root,
            )
            return

        yielded = 0
        for path in sorted(self._fixture_root.glob('*.json')):
            if path.name.startswith('_'):
                continue
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    'quo adapter: skipping unreadable fixture %s: %s',
                    path.name, exc,
                )
                continue

            # Fixture files may hold a single record or a list.
            records = data if isinstance(data, list) else [data]
            for record in records:
                if not isinstance(record, Mapping):
                    continue
                if not _matches_window(record, since=since, until=until):
                    continue
                yield record
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

    # ------------------------------------------------------------------
    # HTTP backend (Phase 10 will exercise this end-to-end)
    # ------------------------------------------------------------------

    def _fetch_http(
        self,
        *,
        since: Optional[datetime],
        until: Optional[datetime],
        limit: Optional[int],
        cursor: Optional[str],
    ) -> Iterator[Mapping]:
        # Deferred import — `requests` isn't needed by the fixture path
        # and keeping it out of module import time keeps unit tests fast.
        import requests

        session = requests.Session()
        headers = {
            'Authorization': f'Bearer {self._proxy_token}',
            'X-BehaviorOS-Client': 'conversations-quo-adapter',
            'Accept': 'application/json',
        }
        params: dict = {}
        if since is not None:
            params['since'] = since.isoformat()
        if until is not None:
            params['until'] = until.isoformat()
        if limit is not None:
            params['limit'] = limit
        if cursor:
            params['cursor'] = cursor

        yielded = 0
        while True:
            resp = session.get(
                self._proxy_url, headers=headers, params=params, timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
            for record in body.get('data', []):
                if not isinstance(record, Mapping):
                    continue
                yield record
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

            next_cursor = (body.get('meta') or {}).get('nextCursor')
            if not next_cursor:
                return
            params['cursor'] = next_cursor


def _matches_window(
    record: Mapping,
    *,
    since: Optional[datetime],
    until: Optional[datetime],
) -> bool:
    """Client-side date filter — used by the fixture backend and as a
    defence-in-depth check for the HTTP backend."""
    if since is None and until is None:
        return True
    ts_str = record.get('lastActivityAt') or record.get('createdAt')
    if not ts_str:
        # Records without a timestamp always pass — filtering them out
        # would drop legitimate data if the source omits the field.
        return True
    from apps.conversations.normalization.quo import _coerce_datetime  # noqa: E402
    ts = _coerce_datetime(ts_str)
    if ts is None:
        return True
    if since is not None and ts < since:
        return False
    if until is not None and ts >= until:
        return False
    return True
