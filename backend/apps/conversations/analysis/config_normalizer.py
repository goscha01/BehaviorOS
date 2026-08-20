"""Pipeline 1B-4: extract BehavioralPolicy rows from a TenantConfigSnapshot.

The raw config is mostly natural language and tenant-editable JSON —
`global_ai_prompt`, `ai_instructions_json`, `follow_up_settings_json`,
etc. LLM reads these and emits normalized policies of the form:

    condition_event  →  ordered prescribed_action_events

keyed by ontology-v2 CUSTOMER_SIGNAL types (condition) and AGENT_ACTION
types (prescribed actions). Rules that don't map to a customer-signal
→ agent-action shape (pricing tables, FAQ text, qualification schema)
are intentionally NOT forced into policies — they stay in the snapshot
as contextual inputs.

Validation:
- Every emitted policy's `condition_event` must be a CUSTOMER_SIGNAL.
- Every action in `prescribed_action_events` must be an AGENT_ACTION.
- Unknown event types are dropped with a per-item warning; the whole
  extraction never fails on one bad row.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from apps.conversations.semantic.ontology import (
    AGENT_ACTION_EVENTS, CUSTOMER_SIGNAL_EVENTS,
)
from apps.learning.services.llm_client import LearningLLMClient, LLMResult

logger = logging.getLogger(__name__)


NORMALIZER_VERSION = 'config-normalizer-v1'


@dataclass
class ExtractedPolicy:
    condition_event: str
    prescribed_action_events: list[str]
    channel: str                        # 'text' | 'voice' | 'policy'
    source_rule_text: str
    source_pointer: dict = field(default_factory=dict)
    extraction_confidence: float = 0.7


@dataclass
class NormalizationResult:
    policies: list[ExtractedPolicy]
    rejected: list[dict]
    llm_input_tokens: int
    llm_output_tokens: int
    llm_cost_usd: Decimal
    model_used: str


def _system_prompt() -> str:
    return f'''You are a behavior-policy extractor for a sales-conversation
platform. Given a JSON blob describing one tenant's current
configuration (AI prompts, per-service instructions, follow-up
settings, and metadata), identify explicit prescribed behaviors of
the form:

    when <customer signal> happens → agent should do <ordered action list>

You return a JSON object with a `policies` array. Each policy MUST
use event types from the fixed ontology below — do NOT invent new
types, do NOT return prose types.

## Allowed CUSTOMER_SIGNAL condition_event values

{sorted(CUSTOMER_SIGNAL_EVENTS)}

## Allowed AGENT_ACTION prescribed_action_events values

{sorted(AGENT_ACTION_EVENTS)}

## Rules

- One policy = one condition mapped to one ordered action sequence.
- If a rule prescribes multiple actions in a specific order, encode
  them as an ordered list. If the rule is ambiguous about order, keep
  it as a single-element list with the most primary action.
- If a rule mentions a customer signal that is NOT in the CUSTOMER_SIGNAL
  vocabulary above, do NOT emit the policy — the ontology is fixed.
- Same for actions. Prefer omission over invention.
- Pricing tables, FAQ answers, qualification questions, and general
  positioning/tone instructions are NOT policies of this form. Skip them.
- Extract from all sources in the config: `user.global_ai_prompt`,
  each `service_profiles[i].ai_instructions_json`, each
  `saved_accounts[i].follow_up_settings_json` — record which one in
  `source_pointer.config_path`.
- Set `confidence` between 0.5 and 1.0 based on how explicit the rule
  is. A direct "when X then do Y" gets 0.9+; an inferred rule from
  positioning language gets 0.6-0.7.
- Verbatim quote the source text in `source_rule_text` (max 500 chars).

## Output schema

{{
  "policies": [
    {{
      "condition_event": "PROPERTY_DETAILS_PROVIDED",
      "prescribed_action_events": ["SERVICE_SCOPE_CLARIFIED", "PRICE_GIVEN", "TIME_SLOT_OFFERED"],
      "channel": "text",
      "source_rule_text": "After you have the bedrooms and bathrooms, confirm the scope, then give a price and offer a time.",
      "source_pointer": {{"config_path": "user.global_ai_prompt"}},
      "confidence": 0.92
    }}
  ]
}}

Return ONLY the JSON object — no prose before or after.'''


def _user_prompt(raw_config: dict) -> str:
    return f'''Tenant configuration JSON:

{json.dumps(raw_config, indent=2)}

Extract behavioral policies per the rules above. Return the JSON object.'''


def _validate_and_dedupe(
    raw_policies: list[dict],
) -> tuple[list[ExtractedPolicy], list[dict]]:
    """Drop policies with unknown ontology types. Dedupe on
    (condition, tuple(actions), channel) so the LLM emitting the same
    rule twice doesn't create duplicate BehavioralPolicy rows."""
    seen: set[tuple] = set()
    accepted: list[ExtractedPolicy] = []
    rejected: list[dict] = []
    for raw in raw_policies:
        if not isinstance(raw, dict):
            rejected.append({'raw': raw, 'reason': 'not an object'})
            continue
        cond = raw.get('condition_event')
        if cond not in CUSTOMER_SIGNAL_EVENTS:
            rejected.append({'raw': raw, 'reason': f'unknown condition_event: {cond!r}'})
            continue
        actions_raw = raw.get('prescribed_action_events') or []
        if not isinstance(actions_raw, list):
            rejected.append({'raw': raw, 'reason': 'prescribed_action_events must be a list'})
            continue
        actions: list[str] = []
        bad_action = False
        for a in actions_raw:
            if a not in AGENT_ACTION_EVENTS:
                rejected.append({
                    'raw': raw,
                    'reason': f'unknown action event: {a!r} (dropping whole policy)',
                })
                bad_action = True
                break
            actions.append(a)
        if bad_action:
            continue
        channel = raw.get('channel') or 'text'
        if channel not in ('text', 'voice', 'policy'):
            rejected.append({'raw': raw, 'reason': f'bad channel: {channel!r}'})
            continue
        pointer = raw.get('source_pointer') or {}
        if not isinstance(pointer, dict):
            pointer = {}
        source_text = str(raw.get('source_rule_text') or '')[:500]
        try:
            conf = float(raw.get('confidence', 0.7))
        except (TypeError, ValueError):
            conf = 0.7
        conf = max(0.0, min(1.0, conf))
        # Dedup key
        key = (cond, tuple(actions), channel)
        if key in seen:
            rejected.append({'raw': raw, 'reason': 'duplicate policy after normalization'})
            continue
        seen.add(key)
        accepted.append(ExtractedPolicy(
            condition_event=cond,
            prescribed_action_events=actions,
            channel=channel,
            source_rule_text=source_text,
            source_pointer=pointer,
            extraction_confidence=conf,
        ))
    return accepted, rejected


def normalize(
    raw_config: dict, *, model: str = 'gpt-4o-mini',
    max_tokens: int = 4000,
    client: Optional[LearningLLMClient] = None,
) -> NormalizationResult:
    """Run the LLM extraction against a raw config JSON.

    Returns a NormalizationResult with validated policies + a log of
    rejected items (per-item reason).
    """
    client = client or LearningLLMClient()
    result: LLMResult = client.analyze(
        system_prompt=_system_prompt(),
        user_prompt=_user_prompt(raw_config),
        model=model,
        max_tokens=max_tokens,
    )
    parsed = result.parsed_json or {}
    if not isinstance(parsed, dict):
        parsed = {}
    raw_policies = parsed.get('policies') or []
    if not isinstance(raw_policies, list):
        raw_policies = []
    accepted, rejected = _validate_and_dedupe(raw_policies)
    return NormalizationResult(
        policies=accepted,
        rejected=rejected,
        llm_input_tokens=result.input_tokens,
        llm_output_tokens=result.output_tokens,
        llm_cost_usd=result.cost_usd,
        model_used=result.model_used,
    )
