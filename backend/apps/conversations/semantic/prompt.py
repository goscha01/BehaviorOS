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

v2 → v3 changes (2026-08-19, driven by 1B-4B action-semantics audits):
- Sharpened PRICE_GIVEN vs PRICE_EXPLAINED with an explicit criterion
  (has-context test) and worked counterexamples from the audit.
- QUALIFICATION_QUESTION protection — an agent question that asks for
  bedrooms/bathrooms/square footage/frequency/property type/scope MUST
  be QUALIFICATION_QUESTION and MUST NOT be PRICE_GIVEN even when the
  customer's preceding turn was about pricing.
- FOLLOW_UP_SENT replaced by FOLLOW_UP_GENERIC / FOLLOW_UP_SUBSTANTIVE.
  A follow-up template that also offers slots / restates a price /
  offers a discount is SUBSTANTIVE. A pure nudge ("just checking in",
  "any updates") is GENERIC. Substantive follow-ups should ALSO emit
  the substantive types (PRICE_GIVEN, TIME_SLOT_OFFERED, etc) — don't
  collapse everything into FOLLOW_UP_SUBSTANTIVE.
- ACKNOWLEDGMENT new — polite ack with no forward motion ("got it",
  "thanks for the details", "perfect, see you Tuesday"). Previously
  such replies got mis-fit into FOLLOW_UP_SENT or dropped.
- Explicit "never emit an event for an empty turn" rule (validator
  ALSO enforces this deterministically).
"""

from __future__ import annotations

from apps.conversations.semantic.ontology import (
    ACTORS, EVENT_TYPES, ONTOLOGY_VERSION, event_types_by_category,
)


PROMPT_VERSION = 'prompt-v3'


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

**PRICE_GIVEN vs PRICE_EXPLAINED** (v3 sharpened):
- PRICE_GIVEN = agent states a price / range with essentially NO
  context. Examples:
    "Your regular cleaning is $159"
    "Deep clean is $200-$250"
    "$150."
- PRICE_EXPLAINED = agent states a price AND supplies material context:
  duration, scope items covered, breakdown, per-hour rate context,
  comparison, or a reason. Examples:
    "$169 for 3.5 hours of cleaning. Any additional time is $50 per hour"
    → PRICE_EXPLAINED (duration context)
    "The price for today will be $349 for 3.5 hours of cleaning; that
     should be enough time to..."
    → PRICE_EXPLAINED (duration + reason)
    "$150 covers 2 hours and includes supplies"
    → PRICE_EXPLAINED (breakdown)
  Counter-example (audit failure): "$249. Please send payment by
  March 7" is PRICE_GIVEN not PRICE_EXPLAINED — payment terms are
  not price rationale.
- DISCOUNT_OFFERED is separate; emit it in ADDITION to
  PRICE_GIVEN/EXPLAINED when the agent offers a promo:
    "$150, and we have 10% off new customers" → PRICE_GIVEN +
    DISCOUNT_OFFERED. Do not fold discount into SERVICE_SCOPE_CLARIFIED
    (that was an audit failure mode).

**QUALIFICATION_QUESTION vs pricing types** (v3 audit protection):
- ANY agent question that asks the customer for bedrooms, bathrooms,
  square footage, cleaning frequency, property type, or scope details
  is QUALIFICATION_QUESTION. It is NOT PRICE_GIVEN, PRICE_EXPLAINED,
  or PRICE_REQUESTED, EVEN when it is the agent's response to a
  customer price question. Examples:
    "What is the square footage of your house?" → QUALIFICATION_QUESTION
    "How many bedrooms and bathrooms?" → QUALIFICATION_QUESTION
    "So just to confirm, we are going to clean 3 bedroom and 2
     bathroom, regular cleaning?" → QUALIFICATION_QUESTION (also
     SERVICE_SCOPE_CLARIFIED if it confirms scope)
- The agent needs qualifying info BEFORE they can quote. That is
  qualification, not price.

**FOLLOW_UP_GENERIC vs FOLLOW_UP_SUBSTANTIVE** (v3 replaces FOLLOW_UP_SENT):
- FOLLOW_UP_GENERIC = the reply is primarily a nudge / check-in with
  NO substantive sales content. Examples:
    "Just checking in — let me know if you need anything."
    "Any updates?"
    "Hi, wanted to see if you had a chance to think it over."
- FOLLOW_UP_SUBSTANTIVE = the reply is a follow-up template that ALSO
  offers a material next step (a price, availability, discount, or
  booking prompt). Examples:
    "Hi , this is Kate from Spotless Homes! I wanted to follow up
     on the cleaning service details I sent. We'd love to help;
     Tuesday at 2pm is open" → FOLLOW_UP_SUBSTANTIVE
    (Also emit TIME_SLOT_OFFERED for the "Tuesday at 2pm" part.)
    "Circling back — I can honor $130 through Friday if you're
     ready" → FOLLOW_UP_SUBSTANTIVE + DISCOUNT_OFFERED.
- Do NOT collapse everything into one FOLLOW_UP_SUBSTANTIVE event.
  Preserve other applicable AGENT_ACTION events (TIME_SLOT_OFFERED,
  DISCOUNT_OFFERED, PRICE_GIVEN, etc.) that appear inside the
  substantive follow-up.

**ACKNOWLEDGMENT** (v3 new):
- Polite acknowledgment with NO forward motion. Examples:
    "Got it!"
    "Thanks for the details."
    "Perfect, see you Tuesday!"
    "Ok"
- Distinct from FOLLOW_UP_GENERIC — an acknowledgment is a REPLY to
  something the customer just said; a follow-up is UNPROMPTED by a
  recent customer turn.
- If the acknowledgment ALSO contains a next step ("Got it — Tuesday
  at 2pm works"), emit both ACKNOWLEDGMENT and the appropriate
  substantive type.

**Empty turns** (v3):
- Never emit ANY event whose source turn (the turn_id you're
  referencing) has empty or whitespace-only text. If a turn is
  empty, no event happened at that turn from the extractor's
  perspective. (A validator will drop any agent-actor event with
  empty source text as a deterministic hard gate.)

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
