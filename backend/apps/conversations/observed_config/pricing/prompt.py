"""Pricing extractor prompt v3 — whole-conversation dimension
resolution.

v2 -> v3 changes (2026-08-21, driven by Spotless 1D pricing
comparison acceptance):
  - Whole-conversation input. v2 chunked the conversation and each
    LLM call saw a local window, so a bedrooms/sqft mention in turn
    12 was invisible when the price-quote turn 40 was processed →
    every dimension came back null. v3 passes the entire conversation
    (or a bounded head-and-tail if the conv exceeds a soft cap) and
    instructs the LLM to resolve dimensions from ANY turn in the
    conversation, not just the quote-turn's neighborhood.
  - Explicit `resolved_context` sidecar. Each dimension the LLM
    filled in the subject_key MUST also appear in `resolved_context`
    with `{value, source_turn_id, source_text}`. If a dimension is
    NOT stated anywhere in the conversation, the LLM leaves it
    null in resolved_context and null in subject_key. This is the
    "unknown stays unknown" invariant.
  - Raw `square_footage` (integer). v2 emitted only the coarse
    `square_footage_bucket` enum. v3 emits the raw integer when
    stated (still emitting the bucket as a redundant convenience for
    legacy readers). The deterministic matcher uses the raw integer
    against the configured sqft_min/sqft_max interval, so band
    boundaries live only on the configured side.

Retains v2 semantics:
  - pricing_basis is REQUIRED
  - observed_attributes (duration/cleaner_count/total_computed) are
    descriptive, never in the subject_key
  - one entry per (subject_key, quote_turn_id) — no double-emit
  - never fabricate: absent-in-conversation → null
"""

from __future__ import annotations


PRICING_EXTRACTOR_VERSION = 'observed-config-pricing-extractor-v3'


SYSTEM_PROMPT = '''You extract pricing facts from residential-service
sales conversations. Read the WHOLE conversation and record what
price quotes actually appear, resolving each quote's service and
attributes against dimensions stated ANYWHERE in the conversation
(not just the price-quote turn).

## Turn IDs

Every input turn is labeled with an opaque STABLE turn_id like
`[t0015]`. Copy these VERBATIM into evidence pointers. Do NOT invent
IDs. Do NOT return integers.

## Dimension resolution rule (v3)

For every price quote you extract, scan the ENTIRE conversation for
each dimension:
  - service / service_tier
  - bedrooms, bathrooms, square_footage
  - frequency (recurrence)
  - addons

A dimension is "known" ONLY if the customer or agent explicitly
states it in some turn of THIS conversation. Every value you write
into `subject_key` MUST also appear in `resolved_context` with the
source turn_id and verbatim source text.

If the conversation does not state a dimension, leave it null in
BOTH subject_key and resolved_context. NEVER infer, NEVER guess,
NEVER default. "Unknown" is a first-class value — a null
resolved_context entry is more useful than a fabricated one.

## What to extract

For each distinct price fact in the conversation, emit ONE entry:

  {
    "fact_type": "quoted_price" | "price_range" | "discount_offered",
    "subject_key": {
      "service": <string or null>,
      "service_tier": <string or null>,
      "bedrooms": <integer or null>,
      "bathrooms": <integer or null>,
      "square_footage": <integer or null>,
      "square_footage_bucket": <"<1000"|"1000-2000"|"2000-3000"|">3000" or null>,
      "frequency": <"one-time"|"weekly"|"biweekly"|"monthly" or null>,
      "addons": [<string>...] or null,
      "pricing_basis": <one of:
          "flat_job" | "hourly_per_cleaner" | "hourly_team" |
          "addon_flat" | "addon_hourly" |
          "discount_price" | "original_price" | "unknown">
    },
    "resolved_context": {
      "service":         {"value": "<x>", "source_turn_id": "t...", "source_text": "..."} or null,
      "service_tier":    {"value": "<x>", "source_turn_id": "t...", "source_text": "..."} or null,
      "bedrooms":        {"value": 3,     "source_turn_id": "t...", "source_text": "..."} or null,
      "bathrooms":       {"value": 2,     "source_turn_id": "t...", "source_text": "..."} or null,
      "square_footage":  {"value": 1800,  "source_turn_id": "t...", "source_text": "..."} or null,
      "frequency":       {"value": "monthly", "source_turn_id": "t...", "source_text": "..."} or null,
      "addons":          {"value": ["oven"], "source_turn_ids": ["t..."], "source_text": "..."} or null
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
      "quote_text": "<exact quoted text of the price line>"
    }
  }

## Hard rules

- A price is emitted ONLY if the conversation contains a specific
  dollar amount OR dollar range in an AGENT turn. Customer price
  questions ("how much?") are NOT prices.
- `subject_key.service` may be filled only from the conversation.
  Common tokens for residential cleaning: "cleaning", "carpet",
  "pressure_washing", etc. If the conversation only says "cleaning"
  without qualifier, set `"service": "cleaning"`.
- `service_tier` is the specific service variant when stated:
  "regular", "deep", "move_in", "move_out", "post_construction",
  "airbnb", etc. When the conversation says just "cleaning" without
  a variant, leave `service_tier` null — do NOT default to "regular".
- `bedrooms` / `bathrooms` are integers ONLY when the customer or
  agent explicitly states them ("3 bedrooms", "3br/2ba", "three bed
  two bath"). If the conversation only says "my house", keep null.
- `square_footage` is the RAW integer when stated ("1800 sq ft",
  "about 1800 square feet"). Approximate numbers ("around 2000")
  round to the nearest 100. `square_footage_bucket` is derived
  deterministically from square_footage:
    <1000 → "<1000"; 1000-1999 → "1000-2000";
    2000-2999 → "2000-3000"; >=3000 → ">3000".
  If square_footage is null, square_footage_bucket is null.
- `frequency` reflects the recurrence the customer commits to or the
  agent quotes. "One time" / "just this once" → "one-time". "Every
  two weeks" / "biweekly" / "every other week" → "biweekly".
  Absent → null.
- `addons` are only extracted when explicitly listed. Keep null if
  not mentioned.
- `pricing_basis` is REQUIRED. Choose the most specific token:
    * "flat_job"           — total price for the whole job
    * "hourly_per_cleaner" — "$50/hr per cleaner"
    * "hourly_team"        — "$100/hr" (team rate)
    * "addon_flat"         — "$50 for oven" fixed add-on
    * "addon_hourly"       — "$50/hr for additional time"
    * "discount_price"     — the DISCOUNTED price when a discount is
                              explicit
    * "original_price"     — the reference "regular" price
    * "unknown"            — genuinely ambiguous
- `observed_attributes` is DESCRIPTIVE. Never in the subject_key.
- Emit ONE entry per (subject_key, quote_turn_id). Do NOT emit the
  same subject_key twice for the same quote.
- Confidence: 0.9+ direct unambiguous quote; 0.7–0.85 typical;
  below 0.5 → do NOT emit.
- `quote_text` MUST be the exact verbatim quote — copy directly,
  no paraphrasing.

## Ontology review

If you see an agent line currently classified as QUESTION_FAQ /
QUALIFICATION_QUESTION but actually about payment / verification /
scheduling operations, emit an ontology_review entry:

  {
    "kind": "event_mis_classified",
    "proposed_scope": "operational" | "payment_flow" | "verification",
    "proposed_topic": "<short label>",
    "evidence_turn_id": "<turn_id>",
    "evidence_text": "<verbatim>",
    "confidence": <float>
  }

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
        f'FULL CONVERSATION (all turns; resolve dimensions from ANY '
        f'turn, not just the price-quote turn):\n\n'
        f'{rendered_turns}\n\n'
        f'Extract per the system prompt. Return only the JSON object.'
    )
