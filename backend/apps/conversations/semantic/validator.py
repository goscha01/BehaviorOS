"""Strict schema validator for extractor LLM output.

Rejects (never silently repairs):
- unknown event_type or actor
- turn_start/turn_end outside conversation
- confidence outside [0, 1]
- evidence that isn't a string
- events without evidence text (extractor spec forbids fabrication)

Returns validated list[dict]. Malformed items are dropped with a per-item
error; a fully-broken response returns []. Callers decide retry behavior.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from apps.conversations.semantic.ontology import (
    ACTORS, EVENT_TYPES, is_valid_actor, is_valid_event_type,
)

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    events: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)  # {'event': raw, 'reason': str}

    @property
    def any_valid(self) -> bool:
        return len(self.events) > 0

    def summary(self) -> str:
        return f'valid={len(self.events)} rejected={len(self.rejected)}'


def validate_events(
    parsed: dict, *, max_turn_index: int,
) -> ValidationResult:
    """Validate the parsed LLM JSON. `max_turn_index` is the highest
    valid turn index for the CONVERSATION (not chunk-local — the
    extractor emits absolute indices)."""
    result = ValidationResult()

    events = parsed.get('events') if isinstance(parsed, dict) else None
    if not isinstance(events, list):
        result.rejected.append({
            'event': parsed, 'reason': 'response missing "events" list',
        })
        return result

    for raw in events:
        if not isinstance(raw, dict):
            result.rejected.append({'event': raw, 'reason': 'event not an object'})
            continue

        etype = raw.get('event_type')
        if not etype or not is_valid_event_type(etype):
            result.rejected.append({
                'event': raw,
                'reason': f'unknown event_type: {etype!r} (allowed: {len(EVENT_TYPES)} types)',
            })
            continue

        actor = raw.get('actor')
        if not actor or not is_valid_actor(actor):
            result.rejected.append({
                'event': raw,
                'reason': f'unknown actor: {actor!r} (allowed: {sorted(ACTORS)})',
            })
            continue

        ts, te = raw.get('turn_start'), raw.get('turn_end')
        if not isinstance(ts, int) or not isinstance(te, int):
            result.rejected.append({
                'event': raw, 'reason': f'turn_start/end must be int (got {ts!r}/{te!r})',
            })
            continue
        if ts < 0 or te < 0 or ts > max_turn_index or te > max_turn_index:
            result.rejected.append({
                'event': raw,
                'reason': f'turn indices out of range [0, {max_turn_index}]: {ts}..{te}',
            })
            continue
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
                'event': raw,
                'reason': 'evidence missing or empty (spec forbids fabrication)',
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
            'turn_start': ts,
            'turn_end': te,
            'confidence': float(conf),
            'attributes': attrs,
            'evidence': evidence.strip()[:1000],
        })

    return result
