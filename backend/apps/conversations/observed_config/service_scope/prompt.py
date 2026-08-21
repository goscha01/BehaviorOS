"""Service-scope extractor prompt v1 — config-agnostic.

Ship D objective (per user spec): reconstruct what Spotless actually
promises / includes / excludes / adds in each service, then compare
against ServiceProfile.serviceOptionsJson.

Actor-discipline is the critical guardrail. This extractor emits
THREE distinct fact_types, only ONE of which participates in the
diff:

  agent_scope_statement (POLICY evidence — reaches the diff)
    Agent explicitly states scope: "oven cleaning is extra $50",
    "baseboards are included in deep cleans", "we don't clean
    exterior windows". The agent's statement about scope IS the
    business's scope policy per this conversation.

  customer_scope_question (context only — persisted, not in diff)
    Customer asks about a scope item: "do you clean inside the
    oven?", "is baseboards included?". The customer's ASK does not
    establish scope; it's a signal about what customers care to
    verify.

  performed_observed (context only — persisted, not in diff)
    Evidence that a scope item was performed (agent report or
    customer feedback): "she cleaned the refrigerator", "they wiped
    all the baseboards". Performance does NOT establish scope
    (could be add-on, exception, or one-off). Retain for
    transparency; NEVER feeds the config diff.

Only agent_scope_statement carries a `relationship` label. The other
two fact_types describe the same scope_item without asserting
what the business's policy is.
"""

from __future__ import annotations


SERVICE_SCOPE_EXTRACTOR_VERSION = (
    'observed-config-service-scope-extractor-v1'
)


# Controlled scope_item taxonomy for residential cleaning. Extractor
# may use `other` with `other_topic` for anything outside this set.
SCOPE_ITEMS = (
    'oven_interior',
    'oven_exterior',
    'refrigerator_interior',
    'refrigerator_exterior',
    'microwave',
    'dishwasher_interior',
    'cabinets_interior',
    'cabinets_exterior',
    'windows_interior',
    'windows_exterior',
    'baseboards',
    'walls_wipe',
    'ceiling_fans',
    'blinds',
    'linens_change',
    'bed_making',
    'laundry',
    'dishes',
    'trash_removal',
    'pet_hair',
    'carpet_vacuum',
    'carpet_shampoo',
    'hardwood_floors',
    'garage',
    'basement',
    'attic',
    'balcony_patio',
    'outdoor_areas',
    'inside_appliances_generic',
    'supplies_provided',
    'other',
)


# Values `relationship` may take (agent_scope_statement only).
SCOPE_RELATIONSHIPS = (
    'INCLUDED',
    'EXCLUDED',
    'OPTIONAL_ADDON',
    'EXTRA_CHARGE',
    'CONDITION_DEPENDENT',
)


SYSTEM_PROMPT = f'''You extract service-scope facts from residential-
service sales conversations. Your job is to record what the business
INCLUDES, EXCLUDES, offers as ADD-ONS, or charges EXTRA for — and to
strictly separate that from customer questions about scope and from
descriptions of work that was performed.

## Turn IDs

Every input turn is labeled with a stable `[t0015]` handle. Copy
these VERBATIM into evidence. Do NOT invent IDs.

## Three fact_types (actor-discipline)

  agent_scope_statement (POLICY EVIDENCE)
    The AGENT explicitly states the business's scope policy. Emit
    this ONLY when the agent's statement is a factual assertion
    about what the business does/doesn't do for a given service.
    Examples:
      Agent: "For deep cleans we include baseboards."
        -> service=deep_cleaning, scope_item=baseboards, relationship=INCLUDED
      Agent: "Oven cleaning is $50 extra."
        -> service=null (or the context service), scope_item=oven_interior, relationship=EXTRA_CHARGE
      Agent: "We don't clean exterior windows."
        -> scope_item=windows_exterior, relationship=EXCLUDED
      Agent: "Refrigerator inside is an optional add-on."
        -> scope_item=refrigerator_interior, relationship=OPTIONAL_ADDON
      Agent: "We can do inside oven if the door detaches easily."
        -> scope_item=oven_interior, relationship=CONDITION_DEPENDENT
    Do NOT emit agent_scope_statement when the agent is merely
    describing what was done ("she cleaned the fridge yesterday") —
    that's performed_observed, not scope policy.

  customer_scope_question (CONTEXT ONLY)
    The CUSTOMER asks about a scope item. Emit even without an
    agent answer. Examples:
      Customer: "Do you clean inside the oven?"
      Customer: "Are baseboards included?"
      Customer: "Do you do laundry?"
    This does NOT establish scope. It only shows customers want to
    verify. The diff will NOT use these facts.

  performed_observed (CONTEXT ONLY)
    Evidence that a scope item was performed — either an agent
    describing work done or a customer mentioning what was done.
    Examples:
      Agent: "She cleaned the refrigerator today."
      Customer: "The cleaner did baseboards and it looks great!"
    Performance does NOT establish scope. Could be an add-on, an
    exception, or a one-off request. The diff will NOT use these
    facts.

If the same message contains multiple scope references (agent says
"we include mopping but ovens are extra"), emit ONE entry per
distinct (scope_item, relationship) — two entries in that example.

## Output entry schema

  {{
    "fact_type": "agent_scope_statement" | "customer_scope_question" | "performed_observed",
    "service": <string or null>,
    "scope_item": <one of the taxonomy tokens below>,
    "other_topic": <snake_case when scope_item="other", else omit>,
    "relationship": <one of the values below, ONLY when fact_type="agent_scope_statement">,
    "context": {{
      "frequency": <"one-time" | "weekly" | "biweekly" | "monthly" | null>,
      "condition": <"first_cleaning" | "recurring_maintenance" | "heavy_condition" | null>
    }},
    "confidence": <float 0.5..1.0>,
    "evidence": {{
      "turn_id": "<turn_id where the statement/question/description appears>",
      "actor": "customer" | "agent",
      "evidence_text": "<exact verbatim quote>"
    }}
  }}

## Scope-item taxonomy (exact tokens)

{sorted(SCOPE_ITEMS)}

If an observation doesn't match, use `scope_item="other"` and add
`other_topic="<snake_case>"`.

## Relationship values (agent_scope_statement only)

- INCLUDED             — part of the standard service, no extra charge
- EXCLUDED             — the business does not do this
- OPTIONAL_ADDON       — available on request, may or may not have price
- EXTRA_CHARGE         — available for an additional fee (explicitly stated)
- CONDITION_DEPENDENT  — done only under specific conditions
                          (e.g. "only if the oven door detaches")

Distinguish `OPTIONAL_ADDON` from `EXTRA_CHARGE`:
- If a specific charge is stated ("$50 for oven") → EXTRA_CHARGE
- If it's available but no charge is stated ("we can add oven if you want") → OPTIONAL_ADDON

## Context (descriptive, not part of subject_key)

`context` captures conditions the agent mentioned as narrowing the
statement (e.g. "for weekly cleanings we skip windows"). It's
retained on the observed fact so a human can review, but the diff
does not use it for MATCH/CONFLICT — only VARIABLE_CONTEXT_DEPENDENT
overlays consider it.

## Hard rules

- `service` is filled only when the statement explicitly mentions
  the service. If the whole conversation is about "regular cleaning"
  and the agent says "we include mopping" without naming the
  service in that turn, use service=null. Do NOT propagate service
  across turns.
- `evidence.evidence_text` MUST be the verbatim quote.
- `evidence.actor` MUST match the fact_type constraint:
    agent_scope_statement -> actor=agent
    customer_scope_question -> actor=customer
    performed_observed -> actor=agent OR customer
- Confidence < 0.5 → do NOT emit.
- Prefer fewer, higher-quality events.
- If the agent is quoting a price (e.g. "$249 for deep clean"),
  that is PRICING, not scope. Do NOT emit as agent_scope_statement.

## Output schema

Return a single JSON object:

  {{
    "scope_events": [ <entries as above>... ]
  }}

If the conversation has no scope content, return
`{{"scope_events": []}}` and nothing else.
'''


def build_user_prompt(rendered_turns: str, conversation_id: str) -> str:
    return (
        f'CONVERSATION_ID: {conversation_id}\n\n'
        f'TURNS:\n{rendered_turns}\n\n'
        f'Extract per the system prompt. Return only the JSON object.'
    )
