"""Generate a RecommendationProposal from an accepted BehaviorRecommendation.

Deterministic eligibility (accepted lifecycle + eligible rec class)
gates the LLM call. The LLM drafts a concrete rule text the operator
can review; it does NOT decide whether the proposal is generated.

v1 scope: STATE_COVERAGE_GAP / STATE_PARTIAL_COVERAGE recommendations
with change_type=`add_behavior_rule`. Everything else is rejected
with an explicit reason so the consumer can surface "not applicable"
rather than a mystery error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from apps.conversations.models import (
    BehaviorRecommendation, RecommendationLifecycleState,
    RecommendationProposal,
)

logger = logging.getLogger(__name__)


GENERATOR_VERSION = 'proposal-generator-v1'


# Eligible rec classes for v1 (only these can become proposals).
_ELIGIBLE_CLASSES = frozenset({
    BehaviorRecommendation.RecClass.STATE_COVERAGE_GAP,
    BehaviorRecommendation.RecClass.STATE_PARTIAL_COVERAGE,
})


class ProposalIneligible(Exception):
    """Raised when a recommendation cannot become a proposal in v1.
    Deterministic — never depends on LLM decisions."""


@dataclass
class DraftedRule:
    summary: str
    detail: str
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: Decimal = Decimal('0')
    llm_model: str = ''


def check_eligibility(rec: BehaviorRecommendation) -> None:
    """Raise ProposalIneligible with a specific reason if the rec can't
    become a proposal in v1."""
    if rec.rec_class not in _ELIGIBLE_CLASSES:
        raise ProposalIneligible(
            f'v1 proposals only supported for STATE_COVERAGE_GAP or '
            f'STATE_PARTIAL_COVERAGE recs; got {rec.rec_class}'
        )
    lifecycle = getattr(rec, 'lifecycle', None)
    if lifecycle is None or lifecycle.state != RecommendationLifecycleState.State.ACCEPTED:
        raise ProposalIneligible(
            'Recommendation must be accepted before a proposal can be '
            'generated (this is the second authorization boundary: '
            'Accept ≠ Apply).'
        )
    if not rec.subject_signals:
        raise ProposalIneligible(
            'Recommendation has no subject_signals; cannot infer which '
            'customer signal the rule should address.'
        )
    if not rec.run.config_snapshot:
        raise ProposalIneligible(
            'Recommendation run has no config snapshot; cannot compute '
            'drift-detection hash for consumer apply.'
        )


def _draft_prompt(rec: BehaviorRecommendation, target_signal: str) -> tuple[str, str]:
    """Build (system, user) prompts for the rule-drafting LLM call.

    The prompt intentionally does NOT give the LLM the full config —
    only the recommendation + evidence. This keeps the drafted rule
    focused on the customer condition; the consumer decides where to
    place it in its own config.
    """
    system = (
        'You draft one short behavior rule that a small-business owner '
        'will review before their AI assistant applies it. The rule '
        'addresses one specific customer signal. It must be:\n'
        '- Plain-language, first-person from the business owner (e.g. '
        '"When a customer asks about discounts, offer our returning-'
        'customer rate.")\n'
        '- Actionable — describes what the AI assistant should DO, not '
        'what the customer is doing.\n'
        '- Neutral about outcome — never promise the rule will "increase '
        'conversions" or similar (evidence does NOT support that claim).\n'
        '- Directly grounded in the recommendation evidence you receive.\n\n'
        'Return ONLY a JSON object of the form:\n'
        '  {"summary": "<one-sentence summary; will be shown in a preview '
        'header>",\n'
        '   "detail": "<2-4 sentences of the actual rule text the assistant '
        'will use>"}'
    )
    import json as _json
    user = (
        f'Recommendation class: {rec.rec_class}\n'
        f'Confidence: {rec.confidence}\n'
        f'Customer signal to address: {target_signal}\n'
        f'Recommendation observation: {rec.observation}\n'
        f'Recommendation interpretation: {rec.interpretation}\n'
        f'Recommendation proposed_action (existing hint): {rec.proposed_action}\n'
        f'Limitations to respect in your language: {rec.limitations}\n'
        f'Evidence:\n{_json.dumps(rec.evidence, indent=2)}\n\n'
        'Draft the JSON object.'
    )
    return system, user


def draft_rule(
    rec: BehaviorRecommendation,
    *,
    target_signal: str,
    llm_client,
    model: str = 'gpt-4o-mini',
) -> DraftedRule:
    system, user = _draft_prompt(rec, target_signal)
    r = llm_client.analyze(
        system_prompt=system, user_prompt=user, model=model, max_tokens=400,
    )
    parsed = r.parsed_json or {}
    if not isinstance(parsed, dict):
        parsed = {}
    summary = str(parsed.get('summary', '')).strip()
    detail = str(parsed.get('detail', '')).strip()
    if not summary or not detail:
        # LLM returned something unusable — surface a hard error so the
        # consumer sees "generation failed" rather than a bad proposal.
        raise RuntimeError(
            f'LLM returned unusable draft (summary={summary!r} '
            f'detail_len={len(detail)}); refusing to persist a broken '
            f'proposal'
        )
    return DraftedRule(
        summary=summary, detail=detail,
        llm_input_tokens=r.input_tokens,
        llm_output_tokens=r.output_tokens,
        llm_cost_usd=r.cost_usd,
        model_used=model if hasattr(r, 'model_used') else model,
    ) if False else DraftedRule(
        summary=summary, detail=detail,
        llm_input_tokens=r.input_tokens,
        llm_output_tokens=r.output_tokens,
        llm_cost_usd=r.cost_usd,
        llm_model=model,
    )


def generate_proposal(
    rec: BehaviorRecommendation,
    *,
    llm_client,
    model: str = 'gpt-4o-mini',
) -> RecommendationProposal:
    """Create + persist a RecommendationProposal for `rec`.

    Idempotent per recommendation: reruns update the same row (v1
    convention — later versions may bump a version field and preserve
    prior drafts).
    """
    check_eligibility(rec)
    # v1 supports the single-signal case; if the rec references
    # multiple uncovered signals, pick the first one for the rule.
    target_signal = rec.subject_signals[0]
    drafted = draft_rule(
        rec, target_signal=target_signal,
        llm_client=llm_client, model=model,
    )
    snapshot = rec.run.config_snapshot
    proposal, _ = RecommendationProposal.objects.update_or_create(
        recommendation=rec,
        defaults={
            'target_system': RecommendationProposal.TargetSystem.LEADBRIDGE,
            'change_type': RecommendationProposal.ChangeType.ADD_BEHAVIOR_RULE,
            'condition': target_signal,
            'scope': snapshot.service_group or '',
            'proposed_behavior_summary': drafted.summary,
            'proposed_behavior_detail': drafted.detail,
            'config_snapshot': snapshot,
            'config_snapshot_hash': snapshot.raw_config_sha256,
            'status': RecommendationProposal.Status.PROPOSED,
            'generator_version': GENERATOR_VERSION,
            'llm_model': drafted.llm_model,
            'llm_input_tokens': drafted.llm_input_tokens,
            'llm_output_tokens': drafted.llm_output_tokens,
            'llm_cost_usd': drafted.llm_cost_usd,
        },
    )
    return proposal
