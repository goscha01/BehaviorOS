"""Pipeline 1C — evidence-grounded recommendation synthesis (v1).

Deterministic eligibility → LLM drafts prose. The LLM is NEVER allowed
to decide whether a piece of evidence is enough for a recommendation
or which class it belongs to.

Design constraints (from 1C spec):
- Never recommend based on CUSTOMER_HESITATION (quarantined in 1B-6)
- Never recommend based on AT_RISK (not validated at v3 corpus size)
- Never resurrect v2 findings that v3 rejected
- Every recommendation has: observation, evidence, interpretation,
  optional proposed_action, confidence, limitations
- Proposed action allowed only when evidence supports the problem/
  coverage gap; NOT allowed to claim "this will improve conversion"
  unless causal evidence supports that claim

Eligibility rules (deterministic):

1. For each validated CustomerState with holdout-reproduced material lift:
   - If ALL contributing signals uncovered by config → STATE_COVERAGE_GAP
   - If SOME contributing signals covered → STATE_PARTIAL_COVERAGE
   - If ALL contributing signals covered → CONFIG_ALIGNMENT

2. For each supported state transition (n >= 5) with material lift:
   - Negative-lift transitions → OBSERVED_STATE_INSIGHT (funnel warning)
   - High-positive-lift transitions worth flagging (n >= 10) →
     OBSERVED_STATE_INSIGHT (segmentation signal)

3. For AT_RISK (structural only) + CUSTOMER_HESITATION (quarantined):
   - Emit INSUFFICIENT_EVIDENCE with explicit "do not act on this" note
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from apps.conversations.analysis.state_inference import (
    CANDIDATE_AT_RISK_SIGNALS, ENGAGED_SIGNALS, EXPLORING_SIGNALS,
    HIGH_INTENT_SIGNALS, BOOKING_INTENT_SIGNALS, QUARANTINED_SIGNALS,
    STATE_AT_RISK, STATE_BOOKING_INTENT, STATE_ENGAGED, STATE_EXPLORING,
    STATE_HIGH_INTENT,
)
from apps.conversations.models import (
    BehaviorRecommendation, BehavioralPolicy,
)

logger = logging.getLogger(__name__)


SYNTHESIZER_VERSION = 'recommendation-synthesizer-v1'


# ---------------------------------------------------------------------------
# Thresholds (deterministic gates)
# ---------------------------------------------------------------------------

# Minimum discovery-set lift vs baseline for a state to be "materially
# validated" (matches 1B-6 acceptance-gate convention).
MATERIAL_STATE_LIFT = 0.10
# Holdout reproduction is required in addition to material lift.
HOLDOUT_MIN_N = 3
# Transitions with |lift| >= this and n >= min-n get an insight
TRANSITION_MATERIAL_LIFT = 0.10
TRANSITION_MIN_N = 5


# Mapping from state → its contributing signals (mirrors state_inference)
STATE_TO_SIGNALS: dict[str, frozenset[str]] = {
    STATE_EXPLORING: EXPLORING_SIGNALS,
    STATE_ENGAGED: ENGAGED_SIGNALS,
    STATE_HIGH_INTENT: HIGH_INTENT_SIGNALS,
    STATE_BOOKING_INTENT: BOOKING_INTENT_SIGNALS,
    STATE_AT_RISK: CANDIDATE_AT_RISK_SIGNALS,
}


# ---------------------------------------------------------------------------
# Candidate recommendation (pre-LLM)
# ---------------------------------------------------------------------------


@dataclass
class RecommendationCandidate:
    """Fully deterministic pre-LLM candidate. LLM only fills in
    observation / interpretation / proposed_action / limitations
    prose. Everything else is set by the eligibility engine."""
    rec_class: str                          # BehaviorRecommendation.RecClass value
    confidence: str                          # BehaviorRecommendation.Confidence value
    subject_state: str = ''
    subject_signals: list[str] = field(default_factory=list)
    linked_policy_ids: list[str] = field(default_factory=list)
    linked_transition: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    supporting_conversation_ids: list[str] = field(default_factory=list)
    proposed_action_scope: str = 'no_action_recommended'
    # If set, use this as the deterministic observation/interp/limits
    # for INSUFFICIENT_EVIDENCE cases where LLM prose isn't warranted.
    prewritten_observation: str = ''
    prewritten_interpretation: str = ''
    prewritten_limitations: str = ''


# ---------------------------------------------------------------------------
# State inputs the engine consumes
# ---------------------------------------------------------------------------


@dataclass
class StateEvidence:
    """The subset of 1B-6 output the synthesizer needs about one state."""
    state: str
    n_discovery: int
    d_positive_rate: float
    d_baseline: float
    d_lift: float
    h_lift: float
    h_n: int
    holdout_reproduced: bool
    supporting_conversation_ids: list[str] = field(default_factory=list)


@dataclass
class TransitionEvidence:
    previous_state: str
    state: str
    n: int
    positive_rate: float
    lift: float
    supporting_conversation_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Eligibility engine
# ---------------------------------------------------------------------------


def _config_coverage_for_signals(
    signals: frozenset[str],
    policies_by_condition: dict[str, list[BehavioralPolicy]],
) -> tuple[list[str], list[str], list[str]]:
    """Return (covered_signals, uncovered_signals, linked_policy_ids)."""
    covered: list[str] = []
    uncovered: list[str] = []
    policy_ids: set[str] = set()
    for s in sorted(signals):
        matching = policies_by_condition.get(s, [])
        if matching:
            covered.append(s)
            for p in matching:
                policy_ids.add(str(p.pk))
        else:
            uncovered.append(s)
    return covered, uncovered, sorted(policy_ids)


def _confidence_from_state(ev: StateEvidence) -> str:
    """Map state evidence to a confidence tier."""
    if not ev.holdout_reproduced or ev.h_n < HOLDOUT_MIN_N:
        return BehaviorRecommendation.Confidence.INSUFFICIENT
    if abs(ev.d_lift) >= MATERIAL_STATE_LIFT and ev.h_n >= 8:
        return BehaviorRecommendation.Confidence.HIGH
    if abs(ev.d_lift) >= MATERIAL_STATE_LIFT:
        return BehaviorRecommendation.Confidence.MEDIUM
    return BehaviorRecommendation.Confidence.LOW


def build_candidates(
    *,
    state_evidence: dict[str, StateEvidence],
    transition_evidence: list[TransitionEvidence],
    policies: list[BehavioralPolicy],
    at_risk_is_validated: bool = False,
) -> list[RecommendationCandidate]:
    """Return deterministically-classified candidates. LLM is NOT
    involved at this stage.

    at_risk_is_validated: if 1B-6 said AT_RISK reached validation, the
    engine may propose action for it. Otherwise AT_RISK becomes
    INSUFFICIENT_EVIDENCE.
    """
    candidates: list[RecommendationCandidate] = []
    policies_by_condition: dict[str, list[BehavioralPolicy]] = {}
    for p in policies:
        policies_by_condition.setdefault(p.condition_event, []).append(p)

    # --------------- 1. Validated states → coverage recs ---------------
    for state in [STATE_EXPLORING, STATE_ENGAGED, STATE_HIGH_INTENT,
                   STATE_BOOKING_INTENT]:
        ev = state_evidence.get(state)
        if ev is None:
            continue
        # Only emit coverage recs for states with holdout reproduction
        # AND material lift.
        if not ev.holdout_reproduced:
            continue
        if abs(ev.d_lift) < MATERIAL_STATE_LIFT:
            # Near-null state — no coverage recommendation warranted;
            # skip. (The state exists but doesn't discriminate outcome.)
            continue

        signals = STATE_TO_SIGNALS[state]
        covered, uncovered, policy_ids = _config_coverage_for_signals(
            signals, policies_by_condition,
        )
        confidence = _confidence_from_state(ev)
        evidence_payload = {
            'state': state,
            'state_n_discovery': ev.n_discovery,
            'state_discovery_positive_rate': ev.d_positive_rate,
            'baseline_positive_rate': ev.d_baseline,
            'state_discovery_lift': ev.d_lift,
            'state_holdout_lift': ev.h_lift,
            'state_holdout_n': ev.h_n,
            'covered_signals': covered,
            'uncovered_signals': uncovered,
        }
        if not uncovered:
            # ALL contributing signals covered → CONFIG_ALIGNMENT
            candidates.append(RecommendationCandidate(
                rec_class=BehaviorRecommendation.RecClass.CONFIG_ALIGNMENT,
                confidence=confidence,
                subject_state=state,
                subject_signals=covered,
                linked_policy_ids=policy_ids,
                evidence=evidence_payload,
                supporting_conversation_ids=ev.supporting_conversation_ids[:20],
                proposed_action_scope=(
                    BehaviorRecommendation.ProposedActionScope.NO_ACTION_RECOMMENDED
                ),
            ))
        elif covered:
            # SOME covered → STATE_PARTIAL_COVERAGE (action = config addition)
            candidates.append(RecommendationCandidate(
                rec_class=BehaviorRecommendation.RecClass.STATE_PARTIAL_COVERAGE,
                confidence=confidence,
                subject_state=state,
                subject_signals=uncovered,
                linked_policy_ids=policy_ids,
                evidence=evidence_payload,
                supporting_conversation_ids=ev.supporting_conversation_ids[:20],
                proposed_action_scope=(
                    BehaviorRecommendation.ProposedActionScope.CONFIG_ADDITION
                ),
            ))
        else:
            # NONE covered → STATE_COVERAGE_GAP
            candidates.append(RecommendationCandidate(
                rec_class=BehaviorRecommendation.RecClass.STATE_COVERAGE_GAP,
                confidence=confidence,
                subject_state=state,
                subject_signals=uncovered,
                linked_policy_ids=[],
                evidence=evidence_payload,
                supporting_conversation_ids=ev.supporting_conversation_ids[:20],
                proposed_action_scope=(
                    BehaviorRecommendation.ProposedActionScope.CONFIG_ADDITION
                ),
            ))

    # --------------- 2. Transition insights ---------------
    for te in transition_evidence:
        if te.n < TRANSITION_MIN_N:
            continue
        if abs(te.lift) < TRANSITION_MATERIAL_LIFT:
            continue
        candidates.append(RecommendationCandidate(
            rec_class=BehaviorRecommendation.RecClass.OBSERVED_STATE_INSIGHT,
            confidence=(
                BehaviorRecommendation.Confidence.MEDIUM if te.n >= 15
                else BehaviorRecommendation.Confidence.LOW
            ),
            subject_state=te.state,
            linked_transition={
                'previous_state': te.previous_state, 'state': te.state,
            },
            evidence={
                'transition': f'{te.previous_state} → {te.state}',
                'n': te.n,
                'positive_rate': te.positive_rate,
                'lift': te.lift,
            },
            supporting_conversation_ids=te.supporting_conversation_ids[:20],
            proposed_action_scope=(
                BehaviorRecommendation.ProposedActionScope.MONITORING_ONLY
            ),
        ))

    # --------------- 3. AT_RISK (unvalidated) → INSUFFICIENT_EVIDENCE ---------------
    if not at_risk_is_validated:
        ar_ev = state_evidence.get(STATE_AT_RISK)
        ar_n = ar_ev.n_discovery if ar_ev else 0
        candidates.append(RecommendationCandidate(
            rec_class=BehaviorRecommendation.RecClass.INSUFFICIENT_EVIDENCE,
            confidence=BehaviorRecommendation.Confidence.INSUFFICIENT,
            subject_state=STATE_AT_RISK,
            subject_signals=sorted(CANDIDATE_AT_RISK_SIGNALS),
            evidence={
                'state': STATE_AT_RISK,
                'reason': 'not_validated_at_v3_corpus_size',
                'n_discovery': ar_n,
            },
            proposed_action_scope=(
                BehaviorRecommendation.ProposedActionScope.NO_ACTION_RECOMMENDED
            ),
            prewritten_observation=(
                f'AT_RISK state was structurally inferred for {ar_n} '
                f'discovery conversations but did not meet the '
                f'validation threshold at the current corpus size.'
            ),
            prewritten_interpretation=(
                'Without validated loss-correlation, AT_RISK cannot be '
                'treated as a reliable state. Upstream candidate signals '
                '(CUSTOMER_DEFERRED, PRICE_OBJECTION, etc.) also have '
                'known extractor-quality issues at v3.'
            ),
            prewritten_limitations=(
                'Do NOT propose config changes for AT_RISK or its '
                'contributing signals until: (a) extractor-v4 addresses '
                'the CUSTOMER_HESITATION-adjacent classification defects, '
                'AND (b) corpus expansion or targeted sampling raises '
                'AT_RISK observation count enough for statistical '
                'validation.'
            ),
        ))

    # --------------- 4. CUSTOMER_HESITATION (quarantined) → INSUFFICIENT_EVIDENCE ---------------
    candidates.append(RecommendationCandidate(
        rec_class=BehaviorRecommendation.RecClass.INSUFFICIENT_EVIDENCE,
        confidence=BehaviorRecommendation.Confidence.INSUFFICIENT,
        subject_signals=sorted(QUARANTINED_SIGNALS),
        evidence={
            'quarantined_signals': sorted(QUARANTINED_SIGNALS),
            'reason': 'phase_0_audit_semantic_precision_below_20_percent',
        },
        proposed_action_scope=(
            BehaviorRecommendation.ProposedActionScope.NO_ACTION_RECOMMENDED
        ),
        prewritten_observation=(
            'CUSTOMER_HESITATION was quarantined by the 1B-6 Phase 0 '
            'audit — only ~13% of tagged occurrences are genuine '
            'hesitation; the rest are deferments, objections, '
            'reassurance-seeking, or extractor misclassifications.'
        ),
        prewritten_interpretation=(
            'Any pattern involving CUSTOMER_HESITATION cannot be '
            'interpreted at v3 extraction quality. The upstream signal '
            'is not trustworthy.'
        ),
        prewritten_limitations=(
            'Do NOT recommend or ship any behavior change based on '
            'CUSTOMER_HESITATION until extractor-v4 (a separate '
            'follow-up) sharpens the extractor prompt to route '
            'deferments to CUSTOMER_DEFERRED, objections to the '
            'appropriate objection types, and reserve CUSTOMER_HESITATION '
            'for genuine uncertainty only.'
        ),
    ))

    return candidates


# ---------------------------------------------------------------------------
# LLM synthesis wrapper (drafts prose ONLY)
# ---------------------------------------------------------------------------


@dataclass
class DraftedProse:
    observation: str
    interpretation: str
    proposed_action: str
    limitations: str
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: Decimal = Decimal('0')


def _system_prompt() -> str:
    return (
        'You draft short, factual sentences for a BehaviorOS '
        'recommendation. Every drafting decision is bounded: the '
        'recommendation class, evidence, confidence, and eligibility '
        'have ALREADY been decided by a deterministic gate before '
        'you were called. Do NOT invent evidence, do NOT change the '
        "class, do NOT recommend action if action_scope is "
        '"no_action_recommended".\n\n'
        'You return a JSON object with exactly these fields:\n'
        '  observation: one short sentence stating what we observed, '
        'grounded in the evidence dict\n'
        '  interpretation: one short sentence explaining what the '
        'observation means (do NOT overstate causality)\n'
        '  proposed_action: one short sentence describing the '
        'proposed action, ONLY if action_scope is "config_addition", '
        '"config_review", or "monitoring_only". Empty string otherwise.\n'
        '  limitations: one or two sentences listing what this evidence '
        'does NOT establish (e.g. does not prove the action will '
        'increase conversion; small sample; not causal).\n\n'
        'Language: plain, direct, no marketing tone. Never claim the '
        "proposed action will improve conversion unless the evidence "
        'block explicitly says "causal_effect_on_conversion_supported".\n\n'
        'Return ONLY the JSON object.'
    )


def _user_prompt(cand: RecommendationCandidate) -> str:
    import json as _json
    return (
        f'recommendation_class: {cand.rec_class}\n'
        f'confidence: {cand.confidence}\n'
        f'subject_state: {cand.subject_state or "(none)"}\n'
        f'subject_signals: {cand.subject_signals}\n'
        f'action_scope: {cand.proposed_action_scope}\n'
        f'linked_transition: {cand.linked_transition or "(none)"}\n'
        f'linked_policy_count: {len(cand.linked_policy_ids)}\n\n'
        f'evidence:\n{_json.dumps(cand.evidence, indent=2)}\n\n'
        f'Draft the JSON object.'
    )


def draft_prose(
    cand: RecommendationCandidate, *,
    llm_client, model: str = 'gpt-4o-mini',
) -> DraftedProse:
    """Ask LLM to draft prose for one eligible candidate. If the
    candidate already has prewritten_* fields (INSUFFICIENT_EVIDENCE
    with fixed language), skip the LLM entirely."""
    if cand.prewritten_observation:
        # INSUFFICIENT_EVIDENCE prose is fixed to avoid the LLM
        # accidentally soft-selling a "no action" as an action.
        return DraftedProse(
            observation=cand.prewritten_observation,
            interpretation=cand.prewritten_interpretation,
            proposed_action='',
            limitations=cand.prewritten_limitations,
        )
    try:
        r = llm_client.analyze(
            system_prompt=_system_prompt(),
            user_prompt=_user_prompt(cand),
            model=model, max_tokens=500,
        )
    except Exception as exc:
        logger.warning('recommendation LLM failed: %s', exc)
        return DraftedProse(
            observation=f'(LLM drafting failed: {exc!r})',
            interpretation='',
            proposed_action='',
            limitations='LLM synthesis failed; evidence block preserved on the row.',
        )
    parsed = r.parsed_json or {}
    if not isinstance(parsed, dict):
        parsed = {}
    obs = str(parsed.get('observation', '')).strip()
    interp = str(parsed.get('interpretation', '')).strip()
    action = str(parsed.get('proposed_action', '')).strip()
    limits = str(parsed.get('limitations', '')).strip()
    # Enforce: action_scope=no_action_recommended → proposed_action MUST be empty.
    # This is the deterministic guard against the LLM accidentally
    # recommending action when we said none was warranted.
    if cand.proposed_action_scope == 'no_action_recommended' and action:
        action = ''
    return DraftedProse(
        observation=obs, interpretation=interp,
        proposed_action=action, limitations=limits,
        llm_input_tokens=r.input_tokens, llm_output_tokens=r.output_tokens,
        llm_cost_usd=r.cost_usd,
    )
