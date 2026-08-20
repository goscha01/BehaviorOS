"""Pricing extractor prompt v2 — config-agnostic.

v1 -> v2 changes (2026-08-20, driven by smoke-run findings on
Spotless):
  - Added `pricing_basis` (flat_job / hourly_per_cleaner / hourly_team
    / addon_flat / addon_hourly / discount_price / original_price /
    unknown) as a KEY dimension. Prevents the smoke-run problem where
    `$50/hr` and `$1600 total job` collapsed into a single
    `service=cleaning` bucket with a bogus $50-$1600 distribution.
  - Split `observed_attributes` from `subject_key`. `duration_hours`,
    `cleaner_count`, and `total_computed_amount` are descriptive
    context — they DON'T participate in the join. Only the key
    dimensions do.
  - Explicit `support_invariant`: at most ONE entry per (fact_type,
    subject_key, quote_turn_id). If the same message contains
    "normally $259, discounted to $219, additional time $50/hr",
    that's THREE distinct facts (original_price / discount_price /
    addon_hourly) but each is emitted once for its subject_key.
    Don't emit the same subject_key twice from one message.
  - Discount emission clarified: emit discount_offered ONLY when the
    agent quotes a discount amount or percentage; when a discounted
    price is mentioned, also emit the discounted amount as a
    quoted_price with `pricing_basis=discount_price` so the
    aggregator can preserve it as a separate distribution.

Input: conversation turns numbered with stable turn_ids (same shape
as the semantic extractor uses). Output: JSON with one entry per
distinct price fact observed.

Design principles:
- NEVER fabricate attributes (bedrooms, bathrooms, service type).
  Extract them ONLY when the customer or agent explicitly states
  them in some turn of the conversation. Absent = null (partial key).
- Every extracted fact must reference the turn_id of the quote itself
  and any turn_ids that anchor the attributes.
- The extractor MAY emit an ontology_review entry when a
  QUESTION_FAQ-like line is actually operational (payment,
  verification, order-tracking) — but must not silently redefine
  ontology labels.
"""

from __future__ import annotations


PRICING_EXTRACTOR_VERSION = 'observed-config-pricing-extractor-v2'


SYSTEM_PROMPT = '''You extract pricing facts from residential-service
sales conversations. Read the conversation turns and record what
price quotes actually appear, with the specific service and
attributes the customer or agent explicitly mentioned.

## Turn IDs

Every input turn is labeled with an opaque STABLE turn_id like
`[t0015]`. Copy these VERBATIM into `evidence.quote_turn_id` and
`evidence.attribute_turn_ids`. Do NOT invent IDs. Do NOT return
integers.

## What to extract

For each distinct price fact in the conversation, emit ONE entry:

  {
    "fact_type": "quoted_price" | "price_range" | "discount_offered",
    "subject_key": {
      "service": <string or null>,
      "bedrooms": <integer or null>,
      "bathrooms": <integer or null>,
      "square_footage_bucket": <"<1000"|"1000-2000"|"2000-3000"|">3000" or null>,
      "frequency": <"one-time"|"weekly"|"biweekly"|"monthly" or null>,
      "addons": [<string>...] or null,
      "pricing_basis": <one of:
          "flat_job" | "hourly_per_cleaner" | "hourly_team" |
          "addon_flat" | "addon_hourly" |
          "discount_price" | "original_price" | "unknown">
    },
    "observed_attributes": {
      "duration_hours": <number or null>,
      "cleaner_count": <integer or null>,
      "total_computed_amount": <number or null>
    },
    "value": {
      "amount": <number or null>,
      "min_amount": <number or null>,
      "max_amount": <number or null>,
      "currency": "USD",
      "discount_pct": <number or null>,
      "discount_amount": <number or null>
    },
    "confidence": <float 0.5..1.0>,
    "evidence": {
      "quote_turn_id": "<turn_id where the price was stated>",
      "attribute_turn_ids": {
        "service": "<turn_id or null>",
        "bedrooms": "<turn_id or null>",
        "bathrooms": "<turn_id or null>",
        "square_footage": "<turn_id or null>",
        "frequency": "<turn_id or null>",
        "pricing_basis": "<turn_id or null>"
      },
      "quote_text": "<exact quoted text of the price line>"
    }
  }

## Hard rules

- A price is emitted ONLY if the conversation contains a specific
  dollar amount OR a specific dollar range in an AGENT turn. Customer
  price questions ("how much?") are NOT prices — do not emit those.
- `subject_key.service` may be filled only from the conversation.
  Common tokens for residential cleaning: "regular_cleaning",
  "deep_cleaning", "move_in_cleaning", "move_out_cleaning",
  "post_construction", "airbnb_turnover". Use snake_case. If the
  conversation says just "cleaning" without qualifier, set
  `"service": "cleaning"`. If unclear, set null.
- `bedrooms` / `bathrooms` are integers ONLY when the customer or
  agent explicitly states them (e.g. "3 bedrooms 2 bathrooms",
  "3br/2ba", "three bed two bath"). If the conversation only says
  "my house" without a count, keep them null. NEVER guess.
- `square_footage_bucket` is derived deterministically from an
  explicit number ("2400 sq ft" → "2000-3000"). If no number is
  stated, keep null.
- `frequency` reflects the recurrence the customer commits to or the
  agent quotes. "This one time" / "just for now" → "one-time". "Every
  two weeks" → "biweekly". Absent → null.
- `addons` are only extracted when explicitly listed (e.g. "plus
  windows", "including inside oven"). Keep null if not mentioned.
- `pricing_basis` is REQUIRED. Choose the most specific token:
    * "flat_job"          — total price for the whole job with no
                             rate structure. "$249 for the cleaning"
                             or "the deep clean is $259". If a
                             duration is mentioned alongside the flat
                             price, still flat_job — the price is
                             the price.
    * "hourly_per_cleaner" — "$50/hr per cleaner", "$40 per hour per
                              person"
    * "hourly_team"        — "$100/hr" (team rate, no per-person
                              qualifier); "$150/hr for 2 cleaners"
                              (team-hourly)
    * "addon_flat"         — "$50 for oven cleaning" as a fixed
                              add-on to some base
    * "addon_hourly"       — "$50/hr for additional time"
    * "discount_price"     — the DISCOUNTED price when a discount is
                              explicit. "Normally $259, today $219"
                              → emit ONE quoted_price with
                              pricing_basis=discount_price amount=219,
                              plus one quoted_price with
                              pricing_basis=original_price amount=259,
                              plus one discount_offered with
                              discount_amount=40.
    * "original_price"     — the reference "regular" price alongside
                              a discount ("normally $30")
    * "unknown"            — genuinely ambiguous. Prefer this over
                              guessing.
- `observed_attributes` is DESCRIPTIVE and does NOT participate in
  the join key. `duration_hours` and `cleaner_count` are captured
  when the agent states them ("2 cleaners × 3 hours × $50") to
  enrich context, but they don't change which configured entry an
  observation matches against. `total_computed_amount` is the
  arithmetic total if the agent computed one from a rate + duration.
- Emit ONE entry per (subject_key, quote_turn_id). Do NOT emit the
  same subject_key twice for the same quote. If the same message
  restates the same price two ways ("your total is $249, that's
  $249 for the cleaning"), emit once.
- Confidence calibration: 0.9+ for a direct unambiguous quote
  ("$249"); 0.7-0.85 typical; below 0.5 → do NOT emit.
- `quote_text` MUST be the exact verbatim quote from the conversation
  — copy directly, no paraphrasing.

## Ontology review

If you see an agent line that is CURRENTLY classified as
QUESTION_FAQ or QUALIFICATION_QUESTION by the existing semantic
extractor but is actually about payment / order verification /
scheduling operations rather than a business FAQ or qualification,
emit an ontology_review entry:

  {
    "kind": "event_mis_classified",
    "proposed_scope": "operational" | "payment_flow" | "verification",
    "proposed_topic": "<short label>",
    "evidence_turn_id": "<turn_id>",
    "evidence_text": "<verbatim>",
    "confidence": <float>
  }

These are advisory. They do NOT change any existing extraction. Only
emit when clearly warranted.

## Output schema

Return a single JSON object:

  {
    "prices": [ <price entries as above>... ],
    "ontology_review": [ <optional review entries>... ]
  }

If the conversation contains no explicit price quotes, return
`{"prices": [], "ontology_review": []}` and nothing else.
'''


def build_user_prompt(rendered_turns: str, conversation_id: str) -> str:
    return (
        f'CONVERSATION_ID: {conversation_id}\n\n'
        f'TURNS:\n{rendered_turns}\n\n'
        f'Extract per the system prompt. Return only the JSON object.'
    )
