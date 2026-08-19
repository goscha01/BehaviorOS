"""Quo conversation source adapter.

Two backends supported today:

1. **Fixture** (default when Sigcore is not configured) — reads JSON
   envelopes from `apps/conversations/adapters/fixtures/quo/`. This is
   what the automated test suite uses.

2. **HTTP (via Sigcore, the shared communications layer)** — Sigcore
   already ingests Quo/OpenPhone conversations, messages, calls, and
   participants. BehaviorOS consumes them directly from Sigcore using
   a workspace-scoped external API key. BehaviorOS never holds Quo
   credentials — Sigcore owns them.

Why Sigcore and not LeadBridge: per the source-ownership model, each
source connects independently. Quo data is owned by the shared
communications layer (Sigcore). Routing Quo through LeadBridge would
force customers to install LB just to analyze Quo conversations, which
breaks the product principle that sources are independently connectable.

Selection is driven by settings:

    SIGCORE_URL       — empty → fixture mode, set → HTTP mode
    SIGCORE_API_KEY   — workspace-scoped `sc_<hex>` external key
                        (x-api-key header — auto-scopes to one Sigcore
                        workspace, so no separate X-Workspace-Id needed)

Sigcore endpoints consumed (all already exist per audit 2026-08-19):

    GET /conversations?page&limit&startDate&endDate&provider=openphone
        → paginated list of conversations
    GET /conversations/:id/messages?limit&before
        → messages for one conversation (cursor pagination via `before`)
    GET /conversations/:id/calls
        → calls for one conversation
    GET /calls/:id/transcript
        → bulk transcript text (not per-segment)

Auth headers on every request:

    x-api-key:  <SIGCORE_API_KEY>   # workspace-scoped `sc_<hex>` key

Envelope shape produced by both backends (fed to the normalizer
unchanged — see `apps/conversations/normalization/quo.py`):

    {
      "id":               "<Sigcore externalId — the Quo native ID>",
      "channel":          "voice" | "sms" | "mixed",
      "workspaceNumber":  "<sigcore conversation.phoneNumber>",
      "participantNumber":"<sigcore conversation.participantPhoneNumber>",
      "createdAt":        "<sigcore conversation.createdAt>",
      "lastActivityAt":   "<sigcore conversation.lastMessageAt>",
      "messages":         [ {id, body, direction, fromNumber, toNumber, createdAt, status} ],
      "calls":            [ {id, direction, duration, recordingUrl, transcript: [...],
                             createdAt, answeredAt, endedAt} ],
      "raw":              { <sigcore internal ids kept for correlation> }
    }

Fixture files sit inline in this shape — no transformation needed for
the fixture path.

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
        sigcore_url: Optional[str] = None,
        sigcore_api_key: Optional[str] = None,
        fixture_root: Optional[Path] = None,
    ):
        # Empty string sentinel — treat as "not configured" so an unset
        # env var and an explicitly empty override behave the same.
        self._sigcore_url = (
            sigcore_url
            if sigcore_url is not None
            else getattr(settings, 'SIGCORE_URL', '')
        )
        self._sigcore_api_key = (
            sigcore_api_key
            if sigcore_api_key is not None
            else getattr(settings, 'SIGCORE_API_KEY', '')
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
        if self._sigcore_url and self._sigcore_api_key:
            yield from self._fetch_sigcore(
                since=since, until=until, limit=limit,
            )
        else:
            yield from self._fetch_fixture(
                since=since, until=until, limit=limit,
            )

    # ------------------------------------------------------------------
    # Fixture backend (unchanged from earlier phases — used by tests)
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
    # Sigcore HTTP backend
    # ------------------------------------------------------------------

    def _fetch_sigcore(
        self,
        *,
        since: Optional[datetime],
        until: Optional[datetime],
        limit: Optional[int],
    ) -> Iterator[Mapping]:
        # Deferred import — `requests` isn't needed by the fixture path.
        import requests

        session = requests.Session()
        headers = {
            'x-api-key': self._sigcore_api_key,
            'X-BehaviorOS-Client': 'conversations-quo-adapter',
            'Accept': 'application/json',
        }
        base = self._sigcore_url.rstrip('/')

        page = 1
        page_size = 100
        yielded = 0
        while True:
            params: dict = {
                'page': page,
                'limit': page_size,
                'provider': 'openphone',  # Sigcore's slug for Quo
            }
            if since is not None:
                params['startDate'] = since.isoformat()
            if until is not None:
                params['endDate'] = until.isoformat()

            try:
                resp = session.get(
                    f'{base}/conversations',
                    headers=headers, params=params, timeout=30,
                )
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    'quo adapter: sigcore list-conversations failed page=%d: %s',
                    page, exc,
                )
                return

            conversations = body.get('data', [])
            if not conversations:
                return

            for sc_conv in conversations:
                if not isinstance(sc_conv, dict):
                    continue
                try:
                    envelope = self._build_envelope(session, base, headers, sc_conv)
                except Exception:
                    logger.exception(
                        'quo adapter: failed to hydrate sigcore conv id=%s',
                        sc_conv.get('id'),
                    )
                    continue
                if envelope is None:
                    continue
                yield envelope
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

            meta = body.get('meta') or {}
            total_pages = meta.get('totalPages')
            if total_pages is not None and page >= int(total_pages):
                return
            if len(conversations) < page_size:
                return
            page += 1

    def _build_envelope(
        self,
        session,
        base: str,
        headers: dict,
        sc_conv: dict,
    ) -> Optional[dict]:
        """Turn one Sigcore conversation row into the envelope shape our
        normalizer expects, hydrating messages + calls + transcripts."""
        sigcore_id = sc_conv.get('id')
        external_id = sc_conv.get('externalId') or sigcore_id  # Quo native ID preferred
        if not external_id:
            return None

        # Fetch messages (cursor-paginated).
        messages: list[dict] = []
        message_cursor: Optional[str] = None
        while True:
            params = {'limit': 100}
            if message_cursor:
                params['before'] = message_cursor
            try:
                mresp = session.get(
                    f'{base}/conversations/{sigcore_id}/messages',
                    headers=headers, params=params, timeout=30,
                )
                mresp.raise_for_status()
                mbody = mresp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    'quo adapter: sigcore messages fetch failed conv=%s: %s',
                    sigcore_id, exc,
                )
                break
            batch = mbody.get('data', [])
            for m in batch:
                if isinstance(m, dict):
                    messages.append(_transform_sigcore_message(m))
            has_more = (mbody.get('meta') or {}).get('hasMore')
            if not has_more or not batch:
                break
            # Use oldest message's createdAt as next cursor (Sigcore uses `before`).
            last = batch[-1]
            message_cursor = last.get('createdAt') if isinstance(last, dict) else None
            if not message_cursor:
                break

        # Fetch calls.
        calls: list[dict] = []
        try:
            cresp = session.get(
                f'{base}/conversations/{sigcore_id}/calls',
                headers=headers, timeout=30,
            )
            cresp.raise_for_status()
            for c in cresp.json().get('data', []):
                if not isinstance(c, dict):
                    continue
                transformed = _transform_sigcore_call(c)
                # Sigcore stores bulk transcript text under a separate endpoint
                # per call — fetch and inject as a single-segment transcript
                # so the normalizer's segment loop still applies.
                call_sc_id = c.get('id')
                if call_sc_id:
                    try:
                        tresp = session.get(
                            f'{base}/calls/{call_sc_id}/transcript',
                            headers=headers, timeout=15,
                        )
                        if tresp.ok:
                            tdata = (tresp.json() or {}).get('data') or {}
                            text = (tdata.get('transcript') or '').strip()
                            if text:
                                transformed['transcript'] = [{
                                    'id': f'{transformed["id"]}:bulk',
                                    'speaker': 'unknown',
                                    'text': text,
                                    'startedAt': transformed.get('createdAt'),
                                    'confidence': None,
                                }]
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            'quo adapter: transcript fetch failed call=%s: %s',
                            call_sc_id, exc,
                        )
                calls.append(transformed)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                'quo adapter: sigcore calls fetch failed conv=%s: %s',
                sigcore_id, exc,
            )

        return {
            'id':                external_id,
            'channel':           _infer_channel_from_sigcore(sc_conv, messages, calls),
            'workspaceNumber':   sc_conv.get('phoneNumber', ''),
            'participantNumber': sc_conv.get('participantPhoneNumber', ''),
            'createdAt':         sc_conv.get('createdAt', ''),
            'lastActivityAt':    sc_conv.get('lastMessageAt') or sc_conv.get('lastActivityAt', ''),
            'messages':          messages,
            'calls':             calls,
            'raw': {
                'sigcore_conversation_id': sigcore_id,
                'sigcore_integration_id':  sc_conv.get('integrationId'),
                'sigcore_tenant_id':       sc_conv.get('tenantId'),
                'sigcore_workspace_id':    sc_conv.get('workspaceId'),
            },
        }


# ---------------------------------------------------------------------------
# Sigcore → envelope transformers
# ---------------------------------------------------------------------------


def _transform_sigcore_message(m: dict) -> dict:
    return {
        'id':          m.get('providerMessageId') or m.get('id'),
        'body':        m.get('body', ''),
        'direction':   m.get('direction', ''),
        'fromNumber':  m.get('fromNumber', ''),
        'toNumber':    m.get('toNumber', ''),
        'createdAt':   m.get('createdAt', ''),
        'status':      m.get('status', ''),
    }


def _transform_sigcore_call(c: dict) -> dict:
    return {
        'id':            c.get('providerCallId') or c.get('id'),
        'direction':     c.get('direction', ''),
        'duration':      c.get('duration'),
        'recordingUrl':  c.get('recordingUrl', ''),
        'createdAt':     c.get('createdAt', ''),
        'answeredAt':    c.get('answeredAt', ''),
        'endedAt':       c.get('endedAt', ''),
    }


def _infer_channel_from_sigcore(
    sc_conv: dict, messages: list, calls: list,
) -> str:
    # Sigcore's `channel` field on conversation is the most reliable signal.
    raw = (sc_conv.get('channel') or '').lower()
    if raw in ('voice', 'sms', 'chat'):
        return raw
    if calls and messages:
        return 'voice' if len(calls) >= len(messages) else 'sms'
    if calls:
        return 'voice'
    if messages:
        return 'sms'
    return 'unknown'


def _matches_window(
    record: Mapping,
    *,
    since: Optional[datetime],
    until: Optional[datetime],
) -> bool:
    """Client-side date filter — used by the fixture backend."""
    if since is None and until is None:
        return True
    ts_str = record.get('lastActivityAt') or record.get('createdAt')
    if not ts_str:
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
