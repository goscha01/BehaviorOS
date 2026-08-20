"""Pipeline 1B-4 deterministic policy-alignment classifier.

For each BehavioralPolicy (customer condition → prescribed action
sequence) derived from a TenantConfigSnapshot, compare against the
ConditionalActionPattern evidence produced by Pipeline 1B-3 and emit a
PolicyAlignmentAssessment with one of the four statuses:

    CONFIG_SUPPORTED       — prescribed action has SUPPORTED evidence
                             with meaningfully positive primary effect
    CONFIG_QUESTIONABLE    — prescribed action has SUPPORTED evidence
                             with meaningfully negative primary effect,
                             AND at least one alternative action for the
                             same condition has SUPPORTED positive effect
    EXECUTION_GAP          — prescribed action rarely observed
                             (< 20% of C's observations) AND some
                             alternative dominates (> 40%)
    INSUFFICIENT_EVIDENCE  — nothing above triggers; cells too thin
                             to say anything reliably

Classification is fully deterministic from stored 1B-3 numbers so it's
auditable and re-runnable. The LLM narrative (if provided) explains,
never decides.

Thresholds are constants at module scope so they're easy to inspect,
easy to change, and easy to record in the rationale string.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from apps.conversations.models import (
    BehavioralPolicy, ConditionalActionPattern,
    PolicyAlignmentAssessment,
)

logger = logging.getLogger(__name__)


CLASSIFIER_VERSION = 'policy-alignment-v1'

# Thresholds — deliberately conservative for the first iteration.
# All applied to Pipeline 1B-3 discovery-set numbers.
SUPPORTED_EFFECT_MIN = 0.10       # primary_effect >= +0.10 counts as "positive"
QUESTIONABLE_EFFECT_MAX = -0.10   # primary_effect <= -0.10 counts as "negative"
EXECUTION_GAP_PRESCRIBED_MAX = 0.20   # prescribed action seen < 20% of the time
EXECUTION_GAP_ALT_MIN = 0.40      # some alternative seen > 40% of the time


@dataclass
class AlignmentDecision:
    status: str
    rationale: str
    primary_pattern: Optional[ConditionalActionPattern]
    evidence_conversation_ids: list[str]


def _observed_rates_for_condition(
    condition: str, patterns_for_condition: list[ConditionalActionPattern],
) -> tuple[dict[str, float], int]:
    """Compute the observed rate of each first-response action given C
    on the discovery set. Returns (rates_by_action, total_observations)
    where total = sum of all C+A observations (both classes) + the
    C+no-action baseline.

    The C+no-action count is taken from any of the cells (they all
    share the same CN row for that condition — it's the same
    denominator sliced differently). If no cells exist, returns
    ({}, 0).
    """
    if not patterns_for_condition:
        return {}, 0
    # Count per action from CA cells
    ca_counts: dict[str, int] = {}
    for p in patterns_for_condition:
        ca_counts[p.action_event] = p.d_ca_positive + p.d_ca_negative
    # CN count is the same across all rows for this C (they share
    # the condition denominator). Pull from the first.
    cn_count = (patterns_for_condition[0].d_cn_positive
                + patterns_for_condition[0].d_cn_negative)
    total = sum(ca_counts.values()) + cn_count
    if total == 0:
        return {}, 0
    rates = {a: n / total for a, n in ca_counts.items()}
    # Also include NO_ACTION as an implicit "rate" for completeness
    rates['__NO_ACTION__'] = cn_count / total
    return rates, total


def classify(
    policy: BehavioralPolicy,
    patterns_for_condition: list[ConditionalActionPattern],
) -> AlignmentDecision:
    """Return the deterministic alignment decision for one policy
    against all ConditionalActionPattern rows for the same condition."""
    condition = policy.condition_event
    prescribed = list(policy.prescribed_action_events or [])
    prescribed_set = set(prescribed)

    # Look up SUPPORTED patterns for prescribed actions
    supported_prescribed_pos: list[ConditionalActionPattern] = []
    supported_prescribed_neg: list[ConditionalActionPattern] = []
    supported_alt_pos: list[ConditionalActionPattern] = []

    for p in patterns_for_condition:
        if p.overall_status != ConditionalActionPattern.OverallStatus.SUPPORTED:
            continue
        eff = p.d_primary_effect
        if p.action_event in prescribed_set:
            if eff >= SUPPORTED_EFFECT_MIN:
                supported_prescribed_pos.append(p)
            elif eff <= QUESTIONABLE_EFFECT_MAX:
                supported_prescribed_neg.append(p)
        else:
            if eff >= SUPPORTED_EFFECT_MIN:
                supported_alt_pos.append(p)

    # Decision tree — order matters; more specific → more general.

    # 1) At least one prescribed action has SUPPORTED positive evidence.
    if supported_prescribed_pos:
        # Pick the strongest as the primary pattern for reporting.
        primary = max(
            supported_prescribed_pos,
            key=lambda p: p.d_primary_effect,
        )
        rationale = (
            f'CONFIG_SUPPORTED: '
            f'({condition}, {primary.action_event}) SUPPORTED '
            f'primary_effect={primary.d_primary_effect:+.2f} '
            f'>= threshold +{SUPPORTED_EFFECT_MIN:.2f}. '
            f'CA cell n={primary.d_ca_positive + primary.d_ca_negative}, '
            f'alt-A cell n={primary.d_co_positive + primary.d_co_negative}. '
            f'holdout={primary.holdout_status}.'
        )
        return AlignmentDecision(
            status=PolicyAlignmentAssessment.AlignmentStatus.CONFIG_SUPPORTED,
            rationale=rationale,
            primary_pattern=primary,
            evidence_conversation_ids=list(primary.evidence_positive_ids or [])[:20],
        )

    # 2) Prescribed action has SUPPORTED negative + an alternative has
    #    SUPPORTED positive → policy looks weaker than alternatives.
    if supported_prescribed_neg and supported_alt_pos:
        primary = min(
            supported_prescribed_neg,
            key=lambda p: p.d_primary_effect,
        )
        best_alt = max(
            supported_alt_pos, key=lambda p: p.d_primary_effect,
        )
        rationale = (
            f'CONFIG_QUESTIONABLE: '
            f'prescribed ({condition}, {primary.action_event}) SUPPORTED '
            f'with primary_effect={primary.d_primary_effect:+.2f} '
            f'<= threshold {QUESTIONABLE_EFFECT_MAX:+.2f}; '
            f'alternative ({condition}, {best_alt.action_event}) SUPPORTED '
            f'with primary_effect={best_alt.d_primary_effect:+.2f}. '
            f'holdout(prescribed)={primary.holdout_status}, '
            f'holdout(alt)={best_alt.holdout_status}.'
        )
        return AlignmentDecision(
            status=PolicyAlignmentAssessment.AlignmentStatus.CONFIG_QUESTIONABLE,
            rationale=rationale,
            primary_pattern=primary,
            evidence_conversation_ids=list(primary.evidence_negative_ids or [])[:20],
        )

    # 3) Execution gap: prescribed action rarely observed + a
    #    non-prescribed action dominates.
    rates, total = _observed_rates_for_condition(condition, patterns_for_condition)
    if total > 0:
        prescribed_rate = sum(rates.get(a, 0.0) for a in prescribed)
        alt_rates = {a: r for a, r in rates.items()
                     if a not in prescribed_set and a != '__NO_ACTION__'}
        max_alt_rate = max(alt_rates.values(), default=0.0)
        if (prescribed_rate < EXECUTION_GAP_PRESCRIBED_MAX
                and max_alt_rate > EXECUTION_GAP_ALT_MIN):
            dominant_action = max(alt_rates, key=alt_rates.get)
            primary = next(
                (p for p in patterns_for_condition
                 if p.action_event == dominant_action),
                None,
            )
            rationale = (
                f'EXECUTION_GAP: '
                f'total observations of {condition}={total}. '
                f'Prescribed actions {prescribed} observed at combined '
                f'rate={prescribed_rate:.2f} '
                f'(< threshold {EXECUTION_GAP_PRESCRIBED_MAX:.2f}); '
                f'dominant observed action = {dominant_action} '
                f'at rate={max_alt_rate:.2f} '
                f'(> threshold {EXECUTION_GAP_ALT_MIN:.2f}).'
            )
            return AlignmentDecision(
                status=PolicyAlignmentAssessment.AlignmentStatus.EXECUTION_GAP,
                rationale=rationale,
                primary_pattern=primary,
                evidence_conversation_ids=(
                    list(primary.evidence_negative_ids or [])[:20]
                    if primary else []
                ),
            )

    # 4) Fallback — insufficient evidence for a claim.
    rationale = (
        f'INSUFFICIENT_EVIDENCE: no SUPPORTED cell for any prescribed '
        f'action {prescribed} of {condition}, '
        f'and no execution-gap pattern observed. '
        f'Total observations of {condition}={total}.'
    )
    # If we have at least one pattern for a prescribed action, use it
    # as the primary — even underpowered cells are worth showing.
    primary = next(
        (p for p in patterns_for_condition
         if p.action_event in prescribed_set),
        None,
    )
    return AlignmentDecision(
        status=PolicyAlignmentAssessment.AlignmentStatus.INSUFFICIENT_EVIDENCE,
        rationale=rationale,
        primary_pattern=primary,
        evidence_conversation_ids=[],
    )
