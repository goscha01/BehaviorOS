"""DefaultCommunicationProfileV1 — the LB text-AI default behavior we
diff observed profiles against.

This is a structured mirror of LB's `SECTION_DEFAULT_PROMPTS` +
`BASE_HARD_RULES` (see Leadbridge/src/ai/section-default-prompts.ts +
base-hard-rules.ts). Copied VERBATIM so a change on LB side is a
visible change here (drift check in tests, later).

The DEFAULT PROFILE is what LB text AI + Callio ship with when no
tenant customization exists. Any observation that materially deviates
from these values is a candidate override for owner review.
"""

from __future__ import annotations

DEFAULT_PROFILE_VERSION = 'leadbridge-playbook-v1'


DEFAULT_COMMUNICATION_PROFILE: dict = {
    'profile_version': DEFAULT_PROFILE_VERSION,
    # Response length — from BASE_HARD_RULES ("Keep replies under 3
    # sentences when possible" in personality_brand_voice) + Callio
    # `maxResponseWords` default (40, min 20).
    'response_style': {
        'typical_agent_sentences': {
            'value': 3,
            'phrasing': 'Keep replies under 3 sentences when possible.',
            'source_section': 'personality_brand_voice',
        },
        'typical_agent_words': {
            'value': 40,
            'phrasing': 'Callio maxResponseWords default; clamped min 20.',
            'source_section': 'callio.voiceV2.maxResponseWords',
        },
        'asks_one_question_at_a_time': {
            'value': True,
            'phrasing': (
                'Ask 1-2 questions at a time; never more; prefer one open-'
                'ended question over a checklist.'
            ),
            'source_section': 'qualification_guidance',
        },
    },
    # Pricing communication — SECTION_DEFAULT_PROMPTS.pricing_guidance.
    'pricing_communication': {
        'directness': {
            'value': 'explain_then_price',
            'phrasing': (
                "Present ranges before exact figures when the customer is "
                "still exploring. Don't volunteer pricing unless asked or "
                "unless qualification is complete."
            ),
            'source_section': 'pricing_guidance',
        },
        'explains_price_range': {
            'value': True,
            'phrasing': 'Present ranges before exact figures.',
            'source_section': 'pricing_guidance',
        },
    },
    # Qualification style — SECTION_DEFAULT_PROMPTS.qualification_guidance.
    'qualification_style': {
        'questions_per_turn_mode': {
            'value': 'one_at_a_time',
            'phrasing': (
                'Ask 1-2 questions at a time; never more; prefer one '
                'open-ended question over a checklist.'
            ),
            'source_section': 'qualification_guidance',
        },
        'typical_sequence': {
            'value': [
                'square_footage', 'timing',
                'condition', 'scope_pets_extras_frequency',
            ],
            'phrasing': (
                'Priority: square footage > timing > condition '
                '(move-in/move-out, heavy soil) > scope '
                '(pets, extras, frequency).'
            ),
            'source_section': 'qualification_guidance',
        },
    },
    # Booking style — SECTION_DEFAULT_PROMPTS.booking_guidance.
    'booking_style': {
        'proposes_specific_times': {
            'value': False,
            'phrasing': (
                "Don't propose specific times — you have no calendar "
                "visibility. Ask the customer when THEY want service."
            ),
            'source_section': 'booking_guidance',
        },
        'confirmation_language': {
            'value': (
                "let me check our timing for [their time] and we'll confirm "
                "shortly during our business hours"
            ),
            'phrasing': (
                'Once they name a time, acknowledge with a holding message. '
                'A team member will reach out DURING BUSINESS HOURS to '
                'confirm.'
            ),
            'source_section': 'booking_guidance',
        },
    },
    # Objection style — SECTION_DEFAULT_PROMPTS.objection_handling.
    'objection_style': {
        'acknowledge_before_responding': {
            'value': True,
            'phrasing': (
                'When the customer pushes back, acknowledge their concern '
                'before responding.'
            ),
            'source_section': 'objection_handling',
        },
        'pricing_objection_approach': {
            'value': 'ask_budget_then_reduce_scope',
            'phrasing': (
                'For pricing objections, ask what budget they had in mind '
                'before offering anything; consider reduced scope before '
                'any discount.'
            ),
            'source_section': 'objection_handling',
        },
    },
    # Tone — SECTION_DEFAULT_PROMPTS.followup_tone +
    # personality_brand_voice.
    'tone': {
        'formality': {
            'value': 'neutral',
            'phrasing': (
                'Friendly, professional, and local. Match the customer\'s '
                'energy — formal if formal, casual if casual.'
            ),
            'source_section': 'personality_brand_voice',
        },
        'warmth': {
            'value': 'medium',
            'phrasing': (
                'Follow-up messages should feel like a continuation, not '
                'a new pitch. Close warmly, no pressure.'
            ),
            'source_section': 'followup_tone',
        },
        'characteristic_phrases': {
            'value': [],
            'phrasing': (
                'No characteristic phrases by default — those are tenant-'
                'specific and only appear as overrides.'
            ),
            'source_section': '(none)',
        },
    },
}


# ---- Dimension registry -----------------------------------------------------
# One row per (dot-path, human label). Drives the diff loop and the owner-
# review UI ordering.

DIMENSIONS: list[dict] = [
    {
        'path': 'response_style.typical_agent_sentences',
        'label': 'Response length (sentences)',
        'section': 'personality_brand_voice',
    },
    {
        'path': 'response_style.typical_agent_words',
        'label': 'Response length (words)',
        'section': 'callio.voiceV2.maxResponseWords',
    },
    {
        'path': 'response_style.asks_one_question_at_a_time',
        'label': 'One question at a time',
        'section': 'qualification_guidance',
    },
    {
        'path': 'pricing_communication.directness',
        'label': 'Pricing directness',
        'section': 'pricing_guidance',
    },
    {
        'path': 'pricing_communication.explains_price_range',
        'label': 'Explains price range',
        'section': 'pricing_guidance',
    },
    {
        'path': 'qualification_style.questions_per_turn_mode',
        'label': 'Qualification style',
        'section': 'qualification_guidance',
    },
    {
        'path': 'qualification_style.typical_sequence',
        'label': 'Qualification sequence',
        'section': 'qualification_guidance',
    },
    {
        'path': 'booking_style.proposes_specific_times',
        'label': 'Proposes specific booking times',
        'section': 'booking_guidance',
    },
    {
        'path': 'booking_style.confirmation_language',
        'label': 'Booking-confirmation language',
        'section': 'booking_guidance',
    },
    {
        'path': 'objection_style.acknowledge_before_responding',
        'label': 'Acknowledges objection before responding',
        'section': 'objection_handling',
    },
    {
        'path': 'objection_style.pricing_objection_approach',
        'label': 'Approach to pricing objections',
        'section': 'objection_handling',
    },
    {
        'path': 'tone.formality',
        'label': 'Formality',
        'section': 'personality_brand_voice',
    },
    {
        'path': 'tone.warmth',
        'label': 'Warmth',
        'section': 'followup_tone',
    },
    {
        'path': 'tone.characteristic_phrases',
        'label': 'Characteristic phrases',
        'section': 'personality_brand_voice',
    },
]


def get_default_dimension_value(dimension_path: str) -> dict:
    """Look up the default value at `response_style.typical_agent_sentences`
    style dot-path. Returns the wrapper dict (has `value` + `phrasing`) or
    {} if the path doesn't exist in the default profile."""
    parts = dimension_path.split('.')
    node: dict = DEFAULT_COMMUNICATION_PROFILE
    for p in parts:
        if not isinstance(node, dict) or p not in node:
            return {}
        node = node[p]
    if isinstance(node, dict):
        return node
    return {}
