"""Extractor prompt template — versioned.

v1 → v2 changes (2026-08-19):
- Turn refs are now opaque STRING turn_ids ("t0015" / "t0015.0"), not
  positional integers. Prompt explicitly documents these as stable
  handles that must be returned verbatim.
- Sharpened BOOKING_REQUESTED vs AVAILABILITY_REQUESTED distinction with
  additional examples + counter-examples.
- Sharpened CUSTOMER_DECLINED vs CUSTOMER_DEFERRED distinction (v2 ontology
  addition).
- LEAD_MISMATCH clause for wrong-intent leads (v2 ontology addition).
- Confidence calibration anchors — the LLM is instructed to reserve 1.00
  for genuinely unambiguous cases and use 0.6–0.85 for typical events.
"""

from __future__ import annotations

from apps.conversations.semantic.ontology import (
    ACTORS, EVENT_TYPES, ONTOLOGY_VERSION, event_types_by_category,
)


PROMPT_VERSION = 'prompt-v2'


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
conversation — NOT what should have happened, and NOT what the final
disposition was. Final dispositions are unknown to you and MUST NOT be
inferred.

You will receive numbered conversation turns and must return a JSON
object with an "events" array. Each event references specific turns
from the input using their turn_id and includes the exact quoted
evidence.

## Turn IDs

Every input turn is labeled with an opaque STABLE turn_id like
`[t0015]` or `[t0015.0]`. These IDs are NOT positional — they are
handles you must copy verbatim into `turn_start` and `turn_end`. Do
NOT invent IDs, do NOT infer numeric offsets, do NOT return integers
for these fields.

## Allowed event types (ontology {ONTOLOGY_VERSION})

{_render_ontology_block()}

## Allowed actors

{sorted(ACTORS)}

## Rules

- Use ONLY event_type values from the ontology above. Do NOT invent new types.
- One turn may contain multiple behaviors → emit multiple events for it.
- One event may span multiple turns (e.g. a multi-turn qualification
  exchange) — use turn_start / turn_end.
- Prefer NO event over a speculative event. If the signal is weak or
  ambiguous, don't emit.
- Preserve actor accurately. Voice turns prefixed with [voice] came
  from a bulk transcript we've already split by speaker — the speaker
  label on each such turn is authoritative.
- Every event MUST include exact quoted evidence copied verbatim from
  the conversation. No fabrication.

## Key distinctions

**AVAILABILITY_REQUESTED vs BOOKING_REQUESTED** (frequently confused):
- AVAILABILITY_REQUESTED = customer asks about SLOTS ("are you free
  Tuesday?", "what times do you have this week?", "what days work?"). No
  commitment implied. Use this when customer is fishing for options.
- BOOKING_REQUESTED = customer asks to COMMIT to a specific slot ("let's
  book Tuesday at 2pm", "yes please schedule me for Friday", "I'll take
  the 10am slot", "let's do it"). Use this when they're finalizing.
- If a single customer turn contains both ("are you free Tuesday? Book
  me if so") emit BOTH events on that turn.

**CUSTOMER_DECLINED vs CUSTOMER_DEFERRED vs CUSTOMER_HESITATION**:
- CUSTOMER_DECLINED = customer clearly ends the sales interaction with
  a negative decision ("no thanks", "we found someone else", "not
  interested"). Terminal.
- CUSTOMER_DEFERRED = customer wants to delay, not decline. ("I'll get
  back to you next week", "let me think about it", "maybe later",
  "circle back after the holidays"). Sales process still alive.
- CUSTOMER_HESITATION = customer expresses uncertainty MID-conversation
  without deferring or declining ("I'm not sure", "hmm", "let me check
  with my husband first" WITHOUT a deferral marker). Weaker signal.

**PRICE_REQUESTED vs PRICE_OBJECTION**:
- PRICE_REQUESTED = customer asks about pricing ("how much?", "what's
  your rate for 3 bedrooms?").
- PRICE_OBJECTION = customer BALKS at a stated price ("that's too
  expensive", "can you do better?"). A discount request is
  DISCOUNT_REQUESTED, not a price objection.

**LEAD_MISMATCH** (v2 new):
- Emit when the customer's intent doesn't match the service being
  offered. Most common cases: customer wants a cleaning JOB
  (employment) not to hire; customer texted the wrong number;
  customer thought they were reaching a different business.
- Distinct from CUSTOMER_DECLINED — a declined customer was a real
  lead who chose not to buy; a mismatched contact was never a lead
  in the first place.

**CUSTOMER_STOPPED_RESPONDING**:
- Only emit when the input clearly shows a timeline gap (large
  timestamp jump between two turns, or the last customer turn is
  followed only by unanswered agent follow-ups). Do NOT infer
  silence from the conversation simply ending.

## Confidence calibration

- 0.95–1.00 : truly unambiguous — direct quote unmistakably matches
  the event type ("How much would that cost?" → PRICE_REQUESTED).
  Reserve for the clearest cases only.
- 0.70–0.94 : typical extraction — clear intent from context but
  possible alternative interpretation. Use this band as the default.
- 0.50–0.69 : plausible but the evidence is thin or context is
  needed. Only emit if the alternative reading is materially less
  likely.
- < 0.50 : do NOT emit. Prefer no event over a speculative one.

Avoid clustering all confidence values at 0.95+; the caller uses
confidence to filter downstream, so calibration matters.

## Output schema

{{
  "events": [
    {{
      "event_type": "PRICE_REQUESTED",
      "actor": "customer",
      "turn_start": "t0003",
      "turn_end": "t0003",
      "confidence": 0.92,
      "attributes": {{}},
      "evidence": "How much would that cost?"
    }},
    {{
      "event_type": "PRICE_RANGE_GIVEN",
      "actor": "agent",
      "turn_start": "t0004",
      "turn_end": "t0004",
      "confidence": 0.88,
      "attributes": {{"currency": "USD", "min": 140, "max": 180}},
      "evidence": "Usually between $140 and $180."
    }}
  ]
}}

Return ONLY the JSON object — no prose before or after.'''


USER_PROMPT_TEMPLATE = '''Conversation turns (each labeled with its stable turn_id):

{turns_block}

Extract structured semantic events per the ontology and rules above.
Copy the turn_id values (e.g. "t0015" or "t0015.0") EXACTLY into
turn_start / turn_end. Return the JSON object.'''


def render_user_prompt(turns_block: str) -> str:
    return USER_PROMPT_TEMPLATE.format(turns_block=turns_block)
