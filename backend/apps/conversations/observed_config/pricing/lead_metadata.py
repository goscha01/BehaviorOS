"""Pull bed/bath/sqft context from a Conversation's linked LB
lead payload (Thumbtack / Yelp / Callio / etc.) so pricing quotes
extracted from the message body can be attributed to specific
LB pricing_table cells.

Motivation
----------
Operator directive (2026-08-22): almost every real quote HAS
request context — it's in the LB request payload, the Thumbtack /
Yelp lead payload, or the initial voice call. When the observed
extractor sees a price in the SMS body but the customer / agent
never restated "1 bedroom / 1 bath / 700 sqft" in the thread,
that dimension IS available elsewhere; not looking there produces
useless partial-evidence.

This module reads `OutcomeSnapshot.source_payload['lb_lead']` for
the conversation and returns a canonical
`{bedrooms, bathrooms, square_footage, service_hint}` dict for
every quote the extractor emits from that conversation to be
enriched with.

Never invents values. If a field isn't in the lead payload, the
key is omitted (matches the observed extractor's "unknown stays
unknown" invariant).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from apps.conversations.models import Conversation, OutcomeSnapshot


logger = logging.getLogger(__name__)


# Field-name aliases the LB `listLeads` payload uses across platforms.
# Kept broad so we tolerate Thumbtack + Yelp + custom variants without
# per-platform branches.
_BEDROOM_KEYS = (
    'bedrooms', 'numBedrooms', 'num_bedrooms', 'bedroomCount',
    'bedroom_count', 'beds', 'numBeds', 'bedroomsCount',
)
_BATHROOM_KEYS = (
    'bathrooms', 'numBathrooms', 'num_bathrooms', 'bathroomCount',
    'bathroom_count', 'baths', 'numBaths', 'bathroomsCount',
)
_SQFT_KEYS = (
    'squareFootage', 'square_footage', 'squareFeet', 'square_feet',
    'sqft', 'sqFt', 'sq_ft', 'homeSize', 'home_size', 'propertySize',
    'property_size', 'size',
)
_SERVICE_KEYS = (
    'serviceType', 'service_type', 'service', 'category',
    'jobType', 'job_type',
)


def extract_dimensions_from_lead(conv: Conversation) -> dict:
    """Return {bedrooms, bathrooms, square_footage, service_hint}
    derived from the conversation's latest LB lead payload. Missing
    fields are omitted (not set to None), so callers can `.update()`
    an existing subject_key without overwriting explicit values."""
    snap = _latest_outcome_with_payload(conv)
    if snap is None:
        return {}
    payload = snap.source_payload or {}
    lb_lead = payload.get('lb_lead') if isinstance(payload, dict) else None
    if not isinstance(lb_lead, dict):
        return {}
    out: dict = {}

    bed = _first_int(lb_lead, _BEDROOM_KEYS)
    if bed is None:
        # Some payloads nest the answers under `details` /
        # `questionResponses` / `requestData`.
        for wrapper in ('details', 'questionResponses', 'requestData', 'request'):
            nested = lb_lead.get(wrapper)
            if isinstance(nested, dict):
                bed = _first_int(nested, _BEDROOM_KEYS)
                if bed is not None:
                    break
    if bed is not None:
        out['bedrooms'] = bed

    bath = _first_int(lb_lead, _BATHROOM_KEYS)
    if bath is None:
        for wrapper in ('details', 'questionResponses', 'requestData', 'request'):
            nested = lb_lead.get(wrapper)
            if isinstance(nested, dict):
                bath = _first_int(nested, _BATHROOM_KEYS)
                if bath is not None:
                    break
    if bath is not None:
        out['bathrooms'] = bath

    sqft = _first_sqft(lb_lead)
    if sqft is None:
        for wrapper in ('details', 'questionResponses', 'requestData', 'request'):
            nested = lb_lead.get(wrapper)
            if isinstance(nested, dict):
                sqft = _first_sqft(nested)
                if sqft is not None:
                    break
    if sqft is not None:
        out['square_footage'] = sqft

    service = _first_str(lb_lead, _SERVICE_KEYS)
    if service:
        out['service_hint'] = service

    return out


# ─── Internals ─────────────────────────────────────────────────────

def _latest_outcome_with_payload(conv: Conversation) -> Optional[OutcomeSnapshot]:
    """Return the most-recent OutcomeSnapshot on this conversation
    that carries a non-empty source_payload. Older snapshots are
    still preserved for audit; the newest usually has the fullest
    payload after every resolver run."""
    return (
        OutcomeSnapshot.objects
        .filter(conversation=conv)
        .exclude(source_payload={})
        .order_by('-captured_at').first()
    )


def _first_int(d: dict, keys: tuple) -> Optional[int]:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            n = int(v)
            if n > 0:
                return n
        except (TypeError, ValueError):
            # Handle "3 bedrooms" prose values.
            if isinstance(v, str):
                m = re.search(r'\d+', v)
                if m:
                    try:
                        n = int(m.group(0))
                        if n > 0:
                            return n
                    except ValueError:
                        pass
    return None


def _first_str(d: dict, keys: tuple) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def _first_sqft(d: dict) -> Optional[int]:
    """Sqft can arrive as an int, a string like '1800', a range like
    '1500-2000', or a bucket label like 'Under 1000'. Prefer the
    midpoint of a range; ignore bucket labels (too coarse for
    per-cell attribution)."""
    for k in _SQFT_KEYS:
        v = d.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
        if isinstance(v, str):
            s = v.strip()
            # Numeric range like '1500-2000' or '1500 - 2000'.
            m = re.match(r'^\s*(\d{3,5})\s*[-–]\s*(\d{3,5})\s*$', s)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
                return (lo + hi) // 2
            # Bare number.
            m = re.match(r'^\s*(\d{3,5})\s*(?:sq\s*ft|sqft|square\s*feet)?\s*$', s, re.I)
            if m:
                return int(m.group(1))
            # 'Under 1000' / 'Over 3000' — too coarse, skip.
    return None
