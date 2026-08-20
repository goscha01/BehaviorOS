"""Qualification extractor prompt v1 — config-agnostic.

Extracts three distinct event kinds per canonical qualification FIELD:

  question_asked            — agent asks about field X
  answer_provided           — customer answers a prior agent question
                               about field X
  volunteered_before_question — customer supplies field X BEFORE the
                                 agent has asked about it

Normalization is critical: raw QUALIFICATION_QUESTION events say
"agent asked something," but the value of Ship B is knowing WHICH
information Spotless actually collects vs the LB config surface.
The prompt normalizes each observation into a controlled taxonomy of
field tokens plus an `other` escape hatch with a proposed topic.

Design principles:
- Extract only what the conversation shows. Do NOT infer that the
  agent asked about bedrooms because the config says they should.
- Every event carries `field_turn_id` and `evidence_text` for
  provenance.
- The customer-volunteered path uses semantic PROPERTY_DETAILS_PROVIDED
  style signals to catch "we have 3 bedrooms, 2 baths, 2400 sq ft"
  volunteered without prompting.
- OntologyReview: if a QUESTION_FAQ line is actually a qualification
  question (or vice versa), emit a review candidate.
"""

from __future__ import annotations


QUALIFICATION_EXTRACTOR_VERSION = (
    'observed-config-qualification-extractor-v1'
)


# Controlled taxonomy — the canonical field tokens. `other` is the
# escape hatch. Recurring `other` topics with high support are
# candidates for taxonomy expansion in v2.
QUALIFICATION_FIELDS = (
    'square_footage',
    'bedrooms',
    'bathrooms',
    'service_type',
    'frequency',
    'property_type',       # house / apartment / condo / townhouse
    'pets',
    'condition',           # dirty / dusty / normal / heavy
    'last_cleaned',        # recency of last clean
    'location',            # address / zip / neighborhood
    'preferred_date',
    'preferred_time',
    'access',              # keys / lockbox / doorman / meet_in_person
    'other',
)


SYSTEM_PROMPT = f'''You extract customer-qualification facts from
residential-service sales conversations. Your job is to record WHICH
customer-information fields the agent actually collects (or the
customer volunteers), so BehaviorOS can compare that to the LeadBridge
qualification schema.

## Turn IDs

Every input turn is labeled with an opaque STABLE turn_id like
`[t0015]`. Copy these VERBATIM into `evidence.field_turn_id`. Do NOT
invent IDs. Do NOT return integers.

## Field taxonomy (controlled — use these exact tokens)

{sorted(QUALIFICATION_FIELDS)}

If the field being requested doesn't match any of the above, use
`"field": "other"` and add `"other_topic": "<snake_case label>"` (e.g.
`"other_topic": "hoa_rules"`, `"other_topic": "vehicle_access"`).

## What to extract

For each qualification-relevant moment in the conversation, emit ONE
entry:

  {{
    "fact_type": "question_asked" | "answer_provided" | "volunteered_before_question",
    "field": <one of the taxonomy tokens>,
    "other_topic": <string when field=="other", else omit>,
    "service_context": <string or null>,
    "confidence": <float 0.5..1.0>,
    "evidence": {{
      "field_turn_id": "<turn_id where the field was requested / answered / volunteered>",
      "referenced_question_turn_id": "<for answer_provided: the earlier turn_id where the agent asked; for volunteered_before_question and question_asked, null>",
      "evidence_text": "<exact verbatim quote>"
    }}
  }}

## Hard rules

- `question_asked` is emitted ONLY when the AGENT explicitly asks the
  customer for the field. "What is the square footage?" → yes.
  Agent restating a value the customer gave ("So 3 bedrooms, 2 baths")
  → NO, that's an acknowledgment / SERVICE_SCOPE_CLARIFIED, not a
  question.
- `answer_provided` is emitted when the CUSTOMER provides a value in
  response to an agent question. Populate `referenced_question_turn_id`
  with the agent-question turn if present.
- `volunteered_before_question` is emitted when the customer provides
  a value BEFORE the agent has asked about it (e.g. inbound lead form
  data restated in-conversation, or the customer proactively supplies
  scope in their first message). Set `referenced_question_turn_id` to
  null.
- `service_context` may be filled ONLY when the question/answer is
  clearly scoped to a specific service (e.g. "for the deep clean,
  how many bedrooms?"). Snake_case service token (regular_cleaning,
  deep_cleaning, move_in_cleaning, etc.). Absent → null.
- Do NOT emit an event just because a field NAME appears in text.
  "Cleaning your 3-bedroom home tomorrow" is a scope confirmation,
  not a qualification exchange.
- Prefer FEWER events over speculative ones. Confidence below 0.5 →
  do NOT emit.
- `evidence_text` MUST be the verbatim quote from the conversation.

## Common field mappings (agent phrasings you may see)

- "square footage of your (home|house|property)" → square_footage
- "how many bedrooms" / "how many bed[room]s" / "how many br" → bedrooms
- "how many bathrooms" / "ba" / "bath[room]s" → bathrooms
- "what type of cleaning" / "which service" → service_type
- "how often" / "every how many weeks" → frequency
- "is this a house or apartment" / "type of property" → property_type
- "do you have any pets" / "any dogs / cats" → pets
- "when was your last cleaning" → last_cleaned
- "what date works" / "which day" → preferred_date
- "what time works" / "morning or afternoon" → preferred_time
- "what's your address" / "zip code" / "which neighborhood" → location
- "how do we get in" / "lockbox" / "door code" → access

## Ontology review

If you see a line the existing semantic extractor CURRENTLY
classified as QUESTION_FAQ but is really a qualification question,
OR a QUALIFICATION_QUESTION that is really operational
(payment/verification), emit an ontology_review entry:

  {{
    "kind": "event_mis_classified",
    "original_event_type": "QUESTION_FAQ" | "QUALIFICATION_QUESTION",
    "proposed_scope": "qualification" | "operational" | "payment_flow" | "verification",
    "proposed_topic": "<snake_case>",
    "evidence_turn_id": "<turn_id>",
    "evidence_text": "<verbatim>",
    "confidence": <float>
  }}

Emit sparingly — only clear cases.

## Output schema

Return a single JSON object:

  {{
    "events": [ <event entries as above>... ],
    "ontology_review": [ <optional review entries>... ]
  }}

If the conversation contains no qualification exchange, return
`{{"events": [], "ontology_review": []}}` and nothing else.
'''


def build_user_prompt(rendered_turns: str, conversation_id: str) -> str:
    return (
        f'CONVERSATION_ID: {conversation_id}\n\n'
        f'TURNS:\n{rendered_turns}\n\n'
        f'Extract per the system prompt. Return only the JSON object.'
    )
