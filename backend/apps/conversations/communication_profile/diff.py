"""CommunicationProfileV1 diff engine.

Compares an extracted CommunicationProfileV1 (observed) against the
DefaultCommunicationProfileV1 (LB defaults) and emits one
CommunicationProfileDiff per registered dimension. The owner review UI
consumes these rows.

Category vocabulary (fixed):
  SAME_AS_DEFAULT              observed matches default
  DIFFERENT_FROM_DEFAULT       observed differs from default
                                 (owner reviews proposed override)
  BUSINESS_SPECIFIC            no default coverage; observed present
  CONFLICTING_OR_UNCLEAR       mixed / ambiguous observed signal
  INSUFFICIENT_EVIDENCE        support_n below threshold

The mapping to override payloads (proposed_override) is kept tight —
one narrative per dimension, one canonical proposal shape per section,
so the LB playbook renderer or Callio comm block can consume the
approved overrides without further transformation.
"""

from __future__ import annotations

import logging
from typing import Optional

from apps.conversations.communication_profile.default_profile import (
    DEFAULT_COMMUNICATION_PROFILE, DIMENSIONS,
    get_default_dimension_value,
)
from apps.conversations.communication_profile.extractor import (
    MIN_SUPPORT_TO_REPORT,
)
from apps.conversations.models import (
    CommunicationProfileDiff, CommunicationProfileRun,
)

logger = logging.getLogger(__name__)


def build_diffs(*, run: CommunicationProfileRun) -> list[CommunicationProfileDiff]:
    """Compute diffs for every registered dimension against the default
    profile. Idempotent per (run, dimension) via update_or_create."""
    profile = run.profile_json or {}
    out: list[CommunicationProfileDiff] = []
    for spec in DIMENSIONS:
        path = spec['path']
        label = spec['label']
        section = spec['section']
        observed = _observed_at(profile, path)
        default = get_default_dimension_value(path)
        diff = _diff_one(observed=observed, default=default,
                         path=path, label=label, section=section)
        obj, _ = CommunicationProfileDiff.objects.update_or_create(
            run=run,
            dimension=path,
            defaults={
                'category': diff['category'],
                'default_value': diff['default_value'],
                'observed_value': diff['observed_value'],
                'support_n': diff['support_n'],
                'confidence': diff['confidence'],
                'narrative': diff['narrative'],
                'proposed_override': diff['proposed_override'],
                'evidence_conversation_ids': diff['evidence'],
            },
        )
        out.append(obj)
    return out


def _observed_at(profile: dict, path: str) -> dict:
    parts = path.split('.')
    node = profile
    for p in parts:
        if not isinstance(node, dict) or p not in node:
            return {}
        node = node[p]
    return node if isinstance(node, dict) else {'value': node}


def _diff_one(
    *, observed: dict, default: dict,
    path: str, label: str, section: str,
) -> dict:
    default_value = default.get('value') if default else None
    obs_value = observed.get('value') if observed else None
    support_n = int(observed.get('support_n') or 0) if observed else 0
    confidence = (observed.get('confidence') or '') if observed else ''
    evidence = (observed.get('evidence_conversation_ids') or []) if observed else []

    # Insufficient evidence gate.
    if support_n < MIN_SUPPORT_TO_REPORT or obs_value in (None, [], ''):
        return {
            'category': CommunicationProfileDiff.Category.INSUFFICIENT_EVIDENCE,
            'default_value': default,
            'observed_value': observed or {},
            'support_n': support_n,
            'confidence': confidence or 'INSUFFICIENT',
            'narrative': (
                f'Not enough evidence to describe "{label}" — need at least '
                f'{MIN_SUPPORT_TO_REPORT} supporting agent turns/conversations.'
            ),
            'proposed_override': {},
            'evidence': evidence,
        }

    # Business-specific: no default at all.
    if not default or default_value is None or default_value == []:
        return {
            'category': CommunicationProfileDiff.Category.BUSINESS_SPECIFIC,
            'default_value': default or {},
            'observed_value': observed,
            'support_n': support_n,
            'confidence': confidence,
            'narrative': (
                f'No default guidance covers "{label}". Observed value: '
                f'{_short(obs_value)} (n={support_n}). Owner can opt in as '
                'a tenant-specific rule.'
            ),
            'proposed_override': _proposed_override(
                path=path, label=label, section=section,
                value=obs_value, default_value=default_value,
                support_n=support_n,
            ),
            'evidence': evidence,
        }

    # Same as default → suppress from active review (still surfaces as
    # SAME_AS_DEFAULT for auditability).
    if _values_equal(obs_value, default_value):
        return {
            'category': CommunicationProfileDiff.Category.SAME_AS_DEFAULT,
            'default_value': default,
            'observed_value': observed,
            'support_n': support_n,
            'confidence': confidence,
            'narrative': (
                f'"{label}" matches the default ({_short(default_value)}). '
                'No owner action needed.'
            ),
            'proposed_override': {},
            'evidence': evidence,
        }

    # Different from default.
    return {
        'category': CommunicationProfileDiff.Category.DIFFERENT_FROM_DEFAULT,
        'default_value': default,
        'observed_value': observed,
        'support_n': support_n,
        'confidence': confidence,
        'narrative': _narrative_for_diff(
            label=label,
            default_value=default_value,
            observed_value=obs_value,
            support_n=support_n,
        ),
        'proposed_override': _proposed_override(
            path=path, label=label, section=section,
            value=obs_value, default_value=default_value,
            support_n=support_n,
        ),
        'evidence': evidence,
    }


def _values_equal(a, b) -> bool:
    """Semantic equality for the small set of scalar/enum/list types the
    profile uses."""
    if a is None and b is None:
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        # Match "3 sentences" default vs "2 sentences" observed as
        # unequal even though ints. Tolerance is 0 — the diff engine
        # doesn't second-guess.
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(str, a)) == sorted(map(str, b))
    return str(a).strip().lower() == str(b).strip().lower()


def _short(value) -> str:
    text = str(value)
    return text if len(text) <= 80 else text[:77] + '…'


def _narrative_for_diff(
    *, label: str, default_value, observed_value, support_n: int,
) -> str:
    return (
        f'{label}: default is {_short(default_value)}; observed is '
        f'{_short(observed_value)} (n={support_n} agent turns / '
        'conversations).'
    )


# ---------------------------------------------------------------------------
# Proposed override shapes — one canonical shape per section so LB / Callio
# can consume approved overrides without further transformation.
# ---------------------------------------------------------------------------

_SECTION_FROM_PATH: dict[str, str] = {
    'response_style.typical_agent_sentences': 'personality_brand_voice',
    'response_style.typical_agent_words': 'callio.voiceV2',
    'response_style.asks_one_question_at_a_time': 'qualification_guidance',
    'pricing_communication.directness': 'pricing_guidance',
    'pricing_communication.explains_price_range': 'pricing_guidance',
    'qualification_style.questions_per_turn_mode': 'qualification_guidance',
    'qualification_style.typical_sequence': 'qualification_guidance',
    'booking_style.proposes_specific_times': 'booking_guidance',
    'booking_style.confirmation_language': 'booking_guidance',
    'objection_style.acknowledge_before_responding': 'objection_handling',
    'objection_style.pricing_objection_approach': 'objection_handling',
    'tone.formality': 'personality_brand_voice',
    'tone.warmth': 'followup_tone',
    'tone.characteristic_phrases': 'personality_brand_voice',
}


def _proposed_override(
    *, path: str, label: str, section: str,
    value, default_value, support_n: int,
) -> dict:
    """One consumable override envelope. LB path: apply to
    aiPlaybookV2[section].customInstructions as a short prose sentence.
    Callio path: the same value is exposed in the communicationProfile
    block additively.

    Payload shape:
      {
        "consumer": "leadbridge" | "callio" | "both",
        "leadbridge": {
            "section": "<PlaybookSectionKey>",
            "custom_instruction_text": "<one-line prose>"
        },
        "callio": {
            "dimension": "<dot-path>",
            "value": <observed>
        },
        "provenance": {"dimension": path, "support_n": n}
      }
    """
    text = _proposal_prose(label=label, value=value)
    lb_section = _SECTION_FROM_PATH.get(path, section)
    consumer = _consumer_for(path)
    envelope: dict = {
        'consumer': consumer,
        'provenance': {
            'dimension': path,
            'support_n': support_n,
        },
    }
    if consumer in ('leadbridge', 'both'):
        # `callio.voiceV2` isn't a real LB playbook section — for the
        # response-length words dimension, skip the LB payload.
        if lb_section and not lb_section.startswith('callio.'):
            envelope['leadbridge'] = {
                'section': lb_section,
                'custom_instruction_text': text,
            }
    if consumer in ('callio', 'both'):
        envelope['callio'] = {
            'dimension': path,
            'value': value,
        }
    return envelope


def _consumer_for(path: str) -> str:
    """Which runtime consumes this override.

    - LB owns text-AI SMS/chat responses → all playbook-section knobs go LB.
    - Callio owns voice execution → response_style + tone knobs go Callio.
    - Both when the knob influences both surfaces (default).
    """
    if path == 'response_style.typical_agent_words':
        return 'callio'
    return 'both'


def _proposal_prose(*, label: str, value) -> str:
    """Produce a short prose sentence usable as
    aiPlaybookV2[section].customInstructions text."""
    if isinstance(value, bool):
        return f'{label}: {"yes" if value else "no"} for this business.'
    if isinstance(value, list):
        return f'{label}: {", ".join(map(str, value))} (business-specific order).'
    return f'{label}: {value} (observed pattern for this business).'
