"""One-time-per-import Sigcore phone → conversation index.

Sigcore's `/conversations` endpoint doesn't support server-side phone
filtering (verified via probes 2026-08-19: `search=` 500s and other
attempted params are ignored). Rather than add a new Sigcore endpoint
just for BehaviorOS, we paginate `/conversations` ONCE per import run
and build an in-memory index:

    normalized_e164 -> [sigcore_conv_summary, sigcore_conv_summary, ...]

For a 4394-conversation Spotless workspace this is:
    - 44 pages × 100/page = ~30-60 seconds one-time cost
    - ~660 KB memory (avg ~150 bytes per summary)
    - O(1) lookup per LB lead

If Sigcore grows past ~50k conversations per tenant OR we scale to
many concurrent BehaviorOS orgs, add a server-side filter to Sigcore.
Not needed today.

The index also naturally detects "multiple Sigcore conversations for
same phone" — the value is a LIST for that reason. Rare (audit found
zero cases in the 25-record sample) but the caller needs to know.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from django.conf import settings

from apps.conversations.normalization.phone import normalize_e164

logger = logging.getLogger(__name__)


@dataclass
class SigcorePhoneIndex:
    by_phone: dict[str, list[dict]] = field(default_factory=dict)
    total_conversations: int = 0
    phones_with_conversation: int = 0
    phones_with_multiple: int = 0

    def lookup(self, phone_e164: str) -> list[dict]:
        return self.by_phone.get(phone_e164, [])


def build_sigcore_phone_index(
    *,
    sigcore_url: Optional[str] = None,
    sigcore_api_key: Optional[str] = None,
    provider: Optional[str] = None,
    max_pages: int = 1000,
    page_size: int = 100,
) -> SigcorePhoneIndex:
    """Paginate `/conversations` and build the phone-to-conversations map.

    Returns an empty index on any error — caller must inspect
    `total_conversations` to distinguish "no data" from "not configured."
    """
    base = (sigcore_url or getattr(settings, 'SIGCORE_URL', '')).rstrip('/')
    key = sigcore_api_key or getattr(settings, 'SIGCORE_API_KEY', '')
    if not base or not key:
        logger.warning('sigcore phone index: not configured; empty index returned')
        return SigcorePhoneIndex()

    import requests

    session = requests.Session()
    headers = {
        'x-api-key': key,
        'X-BehaviorOS-Client': 'conversations-phone-index',
        'Accept': 'application/json',
    }

    index = SigcorePhoneIndex()
    page = 1
    while page <= max_pages:
        params = {'page': page, 'limit': page_size}
        if provider:
            params['provider'] = provider
        try:
            resp = session.get(f'{base}/conversations', headers=headers,
                               params=params, timeout=30)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                'sigcore phone index: page %d failed: %s (returning partial)',
                page, exc,
            )
            break

        rows = body.get('data', [])
        if not rows:
            break

        for row in rows:
            if not isinstance(row, dict):
                continue
            index.total_conversations += 1
            participant = row.get('participantPhoneNumber')
            e164 = normalize_e164(participant)
            if not e164:
                continue
            existing = index.by_phone.setdefault(e164, [])
            existing.append(row)

        meta = body.get('meta') or {}
        total_pages = meta.get('totalPages')
        if total_pages is not None and page >= int(total_pages):
            break
        if len(rows) < page_size:
            break
        page += 1

    # Post-index stats.
    for phone, convs in index.by_phone.items():
        index.phones_with_conversation += 1
        if len(convs) > 1:
            index.phones_with_multiple += 1

    logger.info(
        'sigcore phone index built: total=%d unique_phones=%d multi_conv_phones=%d',
        index.total_conversations,
        index.phones_with_conversation,
        index.phones_with_multiple,
    )
    return index
