"""Extractor prompt template — versioned.

Bump PROMPT_VERSION any time the wording or rules change so runs can be
compared cleanly.
"""

from __future__ import annotations

from apps.conversations.semantic.ontology import (
    ACTORS, EVENT_TYPES, ONTOLOGY_VERSION, event_types_by_category,
)


PROMPT_VERSION = 'prompt-v1'


def _render_ontology_block() -> str:
    lines = []
    for cat, types in event_types_by_category().items():
        lines.append(f'  {cat}:')
        for t in types:
            lines.append(f'    - {t}')
    return '\n'.join(lines)


SYSTEM_PROMPT = f'''You are a semantic-event extractor for sales conversations
between a residential cleaning service (Spotless Homes) and prospective
customers. Your job is to classify what actually occurred in the
conversation — NOT what should have happened, and NOT what the outcome
was. Outcomes are unknown to you and MUST NOT be inferred.

You will receive numbered conversation turns and must return a JSON
object with an "events" array. Each event references specific turn
indices from the input and includes the exact quoted evidence.

## Allowed event types (ontology {ONTOLOGY_VERSION})

{_render_ontology_block()}

## Allowed actors

{sorted(ACTORS)}

## Rules

- Use ONLY event_type values from the ontology above. Do NOT invent new types.
- One turn may contain multiple behaviors → emit multiple events for it.
- One event may span multiple turns (e.g. a multi-turn qualification exchange)
  — use turn_start/turn_end.
- Prefer NO event over a speculative event. If the signal is weak or
  ambiguous, don't emit.
- Preserve actor accurately. Do not mark customer messages as agent.
- Distinguish PRICE_REQUESTED from PRICE_OBJECTION: asking "how much?"
  is REQUESTED; balking at a stated price is OBJECTION.
- Distinguish AVAILABILITY_REQUESTED from BOOKING_REQUESTED: asking
  "are you free Tuesday?" is AVAILABILITY_REQUESTED; asking to commit
  is BOOKING_REQUESTED.
- Only emit CUSTOMER_STOPPED_RESPONDING when a clear timeline gap
  supports it — do NOT assume silence from absence of turns.
- Do NOT infer objections merely from the conversation ending.
- Every event MUST include exact quoted evidence copied verbatim from
  the conversation. No fabrication.
- Confidence in [0, 1]: 1.0 = unambiguous, 0.5 = plausible, <0.5 = don't emit.
- Attributes are event-type-specific. For price events include
  currency + amount/min/max; for qualification include field name +
  value when clear.

## Output schema

{{
  "events": [
    {{
      "event_type": "PRICE_REQUESTED",
      "actor": "customer",
      "turn_start": 3,
      "turn_end": 3,
      "confidence": 0.97,
      "attributes": {{}},
      "evidence": "How much would that cost?"
    }}
  ]
}}

Return ONLY the JSON object — no prose before or after.'''


USER_PROMPT_TEMPLATE = '''Conversation turns (indexed):

{turns_block}

Extract structured semantic events per the ontology and rules above.
Return the JSON object.'''


def render_user_prompt(turns_block: str) -> str:
    return USER_PROMPT_TEMPLATE.format(turns_block=turns_block)
