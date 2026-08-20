"""Regression eval fixtures for extractor-v3.

Each case is a hand-crafted mini-conversation reproducing a specific
failure mode from the 1B-4B action-semantics audits. The evaluator
renders the turns through the extractor prompt, sends to the LLM, and
asserts that the resulting event set contains what SHOULD be there
and excludes what should NOT.

Cases derived directly from documented v2 failures — see
`memory/project_ca0001_verification.md` +
`memory/project_price_requested_verification.md` for provenance.

Fixtures do NOT touch the database. The evaluator constructs the
prompt using `preprocessing.render_turns_for_prompt` semantics
in-memory, then calls the LLM directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvalTurn:
    speaker: str          # customer | agent | system
    text: str


@dataclass
class ExtractorEvalCase:
    name: str
    description: str
    turns: list[EvalTurn]
    # Event types that MUST appear in the extracted set for the case to pass.
    should_emit: list[str] = field(default_factory=list)
    # AT LEAST ONE of these must appear. Used when multiple defensible
    # ontology types cover the same reply (e.g. AVAILABILITY_GIVEN vs
    # TIME_SLOT_OFFERED for "tomorrow at 10 or 9 am") and the case only
    # needs to verify we didn't pick one of the WRONG types.
    should_emit_any_of: list[str] = field(default_factory=list)
    # Event types that MUST NOT appear.
    must_not_emit: list[str] = field(default_factory=list)
    # Optional actor-level checks: no AGENT event may reference an
    # empty-text turn (positional index into `turns`).
    empty_turn_indices: list[int] = field(default_factory=list)


AUDIT_EVAL_CASES: list[ExtractorEvalCase] = [
    # -----------------------------------------------------------------
    # 1. Qualification question misclassified as PRICE_GIVEN
    #    (audit failure: conv 3a7c168d / 4b99aeb3 / 83dabdc5)
    # -----------------------------------------------------------------
    ExtractorEvalCase(
        name='qualification_question_not_price_given',
        description=(
            'Customer asks about pricing; agent responds with a '
            'qualifying question. Must be QUALIFICATION_QUESTION, '
            'NEVER PRICE_GIVEN or PRICE_EXPLAINED.'
        ),
        turns=[
            EvalTurn('customer', 'Hi, how much for a house clean?'),
            EvalTurn('agent', 'What is the square footage of your house?'),
        ],
        should_emit=['PRICE_REQUESTED', 'QUALIFICATION_QUESTION'],
        must_not_emit=['PRICE_GIVEN', 'PRICE_EXPLAINED'],
    ),
    # -----------------------------------------------------------------
    # 2. Price + explanation misclassified as bare PRICE_GIVEN
    #    (audit failure: conv 16900cb9 / 3dddb72c / 3fe16045)
    # -----------------------------------------------------------------
    ExtractorEvalCase(
        name='price_with_duration_context_is_price_explained',
        description=(
            'Agent states price WITH duration/scope context. '
            'v2 flattened this to PRICE_GIVEN; v3 must emit '
            'PRICE_EXPLAINED.'
        ),
        turns=[
            EvalTurn('customer', 'How much for a deep clean?'),
            EvalTurn(
                'agent',
                'The price is $169 for 3.5 hours of cleaning. Any additional '
                'time is $50 per hour.',
            ),
        ],
        should_emit=['PRICE_REQUESTED', 'PRICE_EXPLAINED'],
        must_not_emit=['PRICE_GIVEN'],
    ),
    # -----------------------------------------------------------------
    # 3. Price + discount misclassified as SERVICE_SCOPE_CLARIFIED
    #    (audit failure: conv 81fe8b6a)
    # -----------------------------------------------------------------
    ExtractorEvalCase(
        name='price_plus_discount_emits_discount_offered',
        description=(
            'Agent restates a price AND offers a discount. '
            'v2 misfiled as SERVICE_SCOPE_CLARIFIED; v3 must emit '
            'DISCOUNT_OFFERED (with either PRICE_GIVEN or PRICE_EXPLAINED).'
        ),
        turns=[
            EvalTurn('customer', 'How much for a recurring clean?'),
            EvalTurn(
                'agent',
                'Your regular is $159, and I can offer 10% off for new '
                'customers this month.',
            ),
        ],
        should_emit=['DISCOUNT_OFFERED'],
        must_not_emit=[],   # PRICE_GIVEN or PRICE_EXPLAINED both acceptable
    ),
    # -----------------------------------------------------------------
    # 4. Time-slot / availability offer misclassified as PRICE_EXPLAINED
    #    (audit failure: conv 347cf8dc)
    # -----------------------------------------------------------------
    ExtractorEvalCase(
        name='availability_offer_is_not_price_explained',
        description=(
            'Agent offers availability instead of engaging on price. '
            'v2 tagged the reply as PRICE_EXPLAINED; v3 must NOT do '
            'that. Either TIME_SLOT_OFFERED or AVAILABILITY_GIVEN is '
            'a defensible label for the reply (windows vs specific '
            'slots is fuzzy) — the case gates the ANTI-regression, '
            'not the choice between the two.'
        ),
        turns=[
            EvalTurn('customer', 'Can you tell me the price?'),
            EvalTurn(
                'agent',
                'I have tomorrow at 10 or 9 am available for you — '
                'want to book one of those?',
            ),
        ],
        should_emit_any_of=['TIME_SLOT_OFFERED', 'AVAILABILITY_GIVEN'],
        must_not_emit=['PRICE_EXPLAINED', 'PRICE_GIVEN'],
    ),
    # -----------------------------------------------------------------
    # 5. Substantive follow-up misclassified as generic FOLLOW_UP_SENT
    #    (audit failure: conv 13b4052a / f2df4a6c)
    # -----------------------------------------------------------------
    ExtractorEvalCase(
        name='substantive_followup_is_follow_up_substantive',
        description=(
            'Agent follow-up template that ALSO offers a slot / '
            'discount / next step. v2 tagged as FOLLOW_UP_SENT '
            '(indiscriminate); v3 must emit FOLLOW_UP_SUBSTANTIVE '
            '(and NEVER FOLLOW_UP_SENT).'
        ),
        turns=[
            EvalTurn('customer', 'I need to think about it.'),
            EvalTurn(
                'agent',
                'Hi, this is Kate from Spotless Homes! I wanted to follow '
                'up on the cleaning service details I sent. Tuesday at '
                '2pm is still open if you want to lock it in.',
            ),
        ],
        should_emit=['FOLLOW_UP_SUBSTANTIVE'],
        must_not_emit=['FOLLOW_UP_SENT', 'FOLLOW_UP_GENERIC'],
    ),
    # -----------------------------------------------------------------
    # 6. Generic follow-up correctly labeled
    # -----------------------------------------------------------------
    ExtractorEvalCase(
        name='generic_followup_is_follow_up_generic',
        description=(
            'Pure nudge with no substantive content. v3 must emit '
            'FOLLOW_UP_GENERIC (and NEVER FOLLOW_UP_SENT).'
        ),
        turns=[
            EvalTurn('customer', 'Let me get back to you.'),
            EvalTurn(
                'agent',
                'Hi — just checking in! Let me know if you have any '
                'questions.',
            ),
        ],
        should_emit=['FOLLOW_UP_GENERIC'],
        must_not_emit=['FOLLOW_UP_SENT', 'FOLLOW_UP_SUBSTANTIVE'],
    ),
    # -----------------------------------------------------------------
    # 7. Acknowledgment coverage (v3 new type)
    #    (audit surfaced 5/58 SERVICE_DETAILS_PROVIDED + 3/30
    #     PRICE_REQUESTED cases; v2 dropped these or misfiled them)
    # -----------------------------------------------------------------
    ExtractorEvalCase(
        name='acknowledgment_is_acknowledgment',
        description=(
            'Polite ack ("got it", "thanks") with no forward motion. '
            'v3 must emit ACKNOWLEDGMENT.'
        ),
        turns=[
            EvalTurn(
                'customer',
                '3 bedrooms, 2 bathrooms, about 1800 sqft, weekly cleaning.',
            ),
            EvalTurn('agent', 'Got it! Thanks for the details.'),
        ],
        should_emit=['ACKNOWLEDGMENT'],
        must_not_emit=['FOLLOW_UP_SENT', 'FOLLOW_UP_GENERIC'],
    ),
    # -----------------------------------------------------------------
    # 8. Empty agent turn must not produce ANY agent event
    #    (audit failure: 10/18 FOLLOW_UP_SENT cells had empty text)
    # -----------------------------------------------------------------
    ExtractorEvalCase(
        name='empty_agent_turn_produces_no_agent_event',
        description=(
            'Turn text is empty/whitespace. v3 must NOT emit any '
            'agent-actor event referencing this turn (validator + '
            'extractor deterministic gate).'
        ),
        turns=[
            EvalTurn('customer', 'Hi, I need a deep clean.'),
            EvalTurn('agent', '   '),   # empty/whitespace
        ],
        should_emit=[],
        must_not_emit=[
            'PRICE_GIVEN', 'PRICE_EXPLAINED', 'FOLLOW_UP_SENT',
            'FOLLOW_UP_GENERIC', 'FOLLOW_UP_SUBSTANTIVE', 'ACKNOWLEDGMENT',
            'AVAILABILITY_GIVEN', 'TIME_SLOT_OFFERED', 'DISCOUNT_OFFERED',
        ],
        empty_turn_indices=[1],
    ),
]
