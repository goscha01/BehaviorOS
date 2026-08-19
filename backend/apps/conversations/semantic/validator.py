"""Strict schema validator for extractor LLM output (v2).

v1 → v2 change (2026-08-19): turn_start/turn_end come from the LLM as
STRINGS matching the stable turn_ids we exposed. Validator looks each
up in TurnIdMap and resolves to INT parent DB indices before returning.
Unknown turn_ids are rejected — no coercion, no fallback.

Rejects (never silently repairs):
- unknown event_type or actor
- unknown turn_id (string not in id_map)
- turn_start after turn_end (checked against resolved int order)
- confidence outside [0, 1]
- evidence missing / empty / non-string
- non-object attributes

Returns validated list[dict]. Malformed items are dropped with a per-item
reason; a fully-broken response returns an empty events list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from apps.conversations.semantic.ontology import (
    ACTORS, EVENT_TYPES, is_valid_actor, is_valid_event_type,
)
from apps.conversations.semantic.preprocessing import TurnIdMap

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    events: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    @property
    def any_valid(self) -> bool:
        return len(self.events) > 0

    def summary(self) -> str:
        return f'valid={len(self.events)} rejected={len(self.rejected)}'


def validate_events(
    parsed: dict, *,
    turn_id_map: Optional[TurnIdMap] = None,
    max_turn_index: Optional[int] = None,  # v1 back-compat for tests
) -> ValidationResult:
    """Validate the parsed LLM JSON.

    v2 path: `turn_id_map` supplied → turn refs are STRING turn_ids
    resolved via the map. Returned events carry INT turn_start/end
    (parent DB indices).

    v1 back-compat path: `max_turn_index` supplied instead → turn refs
    are INT indices bounded by max_turn_index. Existing tests use this.
    """
    result = ValidationResult()

    events = parsed.get('events') if isinstance(parsed, dict) else None
    if not isinstance(events, list):
        result.rejected.append({'event': parsed, 'reason': 'response missing "events" list'})
        return result

    use_id_map = turn_id_map is not None

    for raw in events:
        if not isinstance(raw, dict):
            result.rejected.append({'event': raw, 'reason': 'event not an object'})
            continue

        etype = raw.get('event_type')
        if not etype or not is_valid_event_type(etype):
            result.rejected.append({
                'event': raw,
                'reason': f'unknown event_type: {etype!r}',
            })
            continue

        actor = raw.get('actor')
        if not actor or not is_valid_actor(actor):
            result.rejected.append({
                'event': raw,
                'reason': f'unknown actor: {actor!r} (allowed: {sorted(ACTORS)})',
            })
            continue

        ts_raw, te_raw = raw.get('turn_start'), raw.get('turn_end')

        if use_id_map:
            # v2: string turn_ids
            if not isinstance(ts_raw, str) or not isinstance(te_raw, str):
                result.rejected.append({
                    'event': raw,
                    'reason': (f'turn_start/end must be string turn_ids '
                               f'(got {type(ts_raw).__name__}/{type(te_raw).__name__})'),
                })
                continue
            if ts_raw not in turn_id_map.id_to_parent:
                result.rejected.append({
                    'event': raw, 'reason': f'unknown turn_start id: {ts_raw!r}',
                })
                continue
            if te_raw not in turn_id_map.id_to_parent:
                result.rejected.append({
                    'event': raw, 'reason': f'unknown turn_end id: {te_raw!r}',
                })
                continue
            ts = turn_id_map.id_to_parent[ts_raw]
            te = turn_id_map.id_to_parent[te_raw]
        else:
            # v1: int indices
            if not isinstance(ts_raw, int) or not isinstance(te_raw, int):
                result.rejected.append({
                    'event': raw,
                    'reason': f'turn_start/end must be int (got {ts_raw!r}/{te_raw!r})',
                })
                continue
            if max_turn_index is not None and (
                ts_raw < 0 or te_raw < 0
                or ts_raw > max_turn_index or te_raw > max_turn_index
            ):
                result.rejected.append({
                    'event': raw,
                    'reason': f'turn indices out of range [0, {max_turn_index}]: {ts_raw}..{te_raw}',
                })
                continue
            ts, te = ts_raw, te_raw

        if ts > te:
            result.rejected.append({
                'event': raw, 'reason': f'turn_start > turn_end: {ts} > {te}',
            })
            continue

        conf = raw.get('confidence')
        if not isinstance(conf, (int, float)) or not 0.0 <= float(conf) <= 1.0:
            result.rejected.append({
                'event': raw, 'reason': f'confidence out of [0,1]: {conf!r}',
            })
            continue

        evidence = raw.get('evidence', '')
        if not isinstance(evidence, str) or not evidence.strip():
            result.rejected.append({
                'event': raw, 'reason': 'evidence missing or empty',
            })
            continue

        attrs = raw.get('attributes', {})
        if not isinstance(attrs, dict):
            result.rejected.append({
                'event': raw, 'reason': f'attributes must be object, got {type(attrs).__name__}',
            })
            continue

        result.events.append({
            'event_type': etype,
            'actor': actor,
            'turn_start': ts,   # INT parent DB idx
            'turn_end': te,
            'confidence': float(conf),
            'attributes': attrs,
            'evidence': evidence.strip()[:1000],
        })

    return result
