"""Pipeline 1C tests — deterministic eligibility engine, no
CUSTOMER_HESITATION-based actions, class decision tree correctness,
LLM prose enforcement (action_scope=no_action_recommended → proposed
action MUST be empty)."""

from __future__ import annotations

from unittest.mock import MagicMock

from django.test import SimpleTestCase

from apps.conversations.analysis.recommendation_synthesis import (
    STATE_TO_SIGNALS, StateEvidence, TransitionEvidence,
    build_candidates, draft_prose,
)
from apps.conversations.models import BehaviorRecommendation


def _state(state, *, n=30, lift=0.15, h_lift=0.13, h_n=6,
           reproduced=True, rate=None):
    return StateEvidence(
        state=state,
        n_discovery=n,
        d_positive_rate=(rate if rate is not None else 0.62 + lift),
        d_baseline=0.62,
        d_lift=lift,
        h_lift=h_lift,
        h_n=h_n,
        holdout_reproduced=reproduced,
    )


def _policy(condition, actions):
    """Duck-type a BehavioralPolicy for the engine (only fields it reads)."""
    p = MagicMock()
    p.pk = f'policy-{condition}'
    p.condition_event = condition
    p.prescribed_action_events = actions
    return p


# ---------------------------------------------------------------------------
# Class decision tree
# ---------------------------------------------------------------------------


class ClassDecisionTests(SimpleTestCase):
    def test_state_with_zero_coverage_yields_state_coverage_gap(self):
        # HIGH_INTENT: none of its signals covered by config
        cands = build_candidates(
            state_evidence={'HIGH_INTENT': _state('HIGH_INTENT')},
            transition_evidence=[],
            policies=[],  # no policies at all
        )
        # Find the HIGH_INTENT candidate
        hi = [c for c in cands if c.subject_state == 'HIGH_INTENT']
        self.assertEqual(len(hi), 1)
        self.assertEqual(
            hi[0].rec_class,
            BehaviorRecommendation.RecClass.STATE_COVERAGE_GAP,
        )
        self.assertEqual(
            hi[0].proposed_action_scope, 'config_addition',
        )

    def test_state_with_partial_coverage_yields_state_partial_coverage(self):
        # HIGH_INTENT signals: PRICE_REQUESTED, AVAILABILITY_REQUESTED,
        # DISCOUNT_REQUESTED. Cover 2 of 3.
        cands = build_candidates(
            state_evidence={'HIGH_INTENT': _state('HIGH_INTENT')},
            transition_evidence=[],
            policies=[
                _policy('PRICE_REQUESTED', ['PRICE_GIVEN']),
                _policy('AVAILABILITY_REQUESTED', ['TIME_SLOT_OFFERED']),
            ],
        )
        hi = [c for c in cands if c.subject_state == 'HIGH_INTENT']
        self.assertEqual(len(hi), 1)
        self.assertEqual(
            hi[0].rec_class,
            BehaviorRecommendation.RecClass.STATE_PARTIAL_COVERAGE,
        )
        # subject_signals only lists the uncovered signal
        self.assertEqual(hi[0].subject_signals, ['DISCOUNT_REQUESTED'])
        # 2 policies linked
        self.assertEqual(len(hi[0].linked_policy_ids), 2)

    def test_state_with_full_coverage_yields_config_alignment(self):
        # BOOKING_INTENT signals: BOOKING_REQUESTED. Cover it.
        cands = build_candidates(
            state_evidence={'BOOKING_INTENT': _state('BOOKING_INTENT')},
            transition_evidence=[],
            policies=[_policy('BOOKING_REQUESTED', ['BOOKING_ATTEMPT'])],
        )
        bi = [c for c in cands if c.subject_state == 'BOOKING_INTENT']
        self.assertEqual(len(bi), 1)
        self.assertEqual(
            bi[0].rec_class,
            BehaviorRecommendation.RecClass.CONFIG_ALIGNMENT,
        )
        # CONFIG_ALIGNMENT is not itself an action recommendation
        self.assertEqual(
            bi[0].proposed_action_scope, 'no_action_recommended',
        )

    def test_state_not_holdout_reproduced_not_emitted(self):
        cands = build_candidates(
            state_evidence={
                'HIGH_INTENT': _state('HIGH_INTENT', reproduced=False),
            },
            transition_evidence=[],
            policies=[],
        )
        hi = [c for c in cands if c.subject_state == 'HIGH_INTENT']
        self.assertEqual(hi, [])

    def test_state_below_material_lift_not_emitted(self):
        cands = build_candidates(
            state_evidence={
                'ENGAGED': _state('ENGAGED', lift=0.02, h_lift=0.02),
            },
            transition_evidence=[],
            policies=[],
        )
        eng = [c for c in cands if c.subject_state == 'ENGAGED']
        self.assertEqual(eng, [])


# ---------------------------------------------------------------------------
# Quarantine + AT_RISK guards
# ---------------------------------------------------------------------------


class QuarantineGuardTests(SimpleTestCase):
    def test_customer_hesitation_always_produces_insufficient_evidence(self):
        cands = build_candidates(
            state_evidence={},
            transition_evidence=[],
            policies=[],
        )
        # One INSUFFICIENT_EVIDENCE candidate for CUSTOMER_HESITATION
        # should always appear.
        hesit = [c for c in cands
                 if 'CUSTOMER_HESITATION' in c.subject_signals]
        self.assertEqual(len(hesit), 1)
        self.assertEqual(
            hesit[0].rec_class,
            BehaviorRecommendation.RecClass.INSUFFICIENT_EVIDENCE,
        )
        self.assertEqual(
            hesit[0].proposed_action_scope, 'no_action_recommended',
        )
        # Prewritten prose is set so the LLM can't accidentally soft-
        # sell "no action" as an action
        self.assertTrue(hesit[0].prewritten_observation)
        self.assertTrue(hesit[0].prewritten_limitations)

    def test_at_risk_unvalidated_produces_insufficient_evidence(self):
        cands = build_candidates(
            state_evidence={'AT_RISK': _state('AT_RISK', n=3, reproduced=False)},
            transition_evidence=[],
            policies=[],
            at_risk_is_validated=False,
        )
        ar = [c for c in cands if c.subject_state == 'AT_RISK']
        self.assertEqual(len(ar), 1)
        self.assertEqual(
            ar[0].rec_class,
            BehaviorRecommendation.RecClass.INSUFFICIENT_EVIDENCE,
        )
        self.assertEqual(
            ar[0].proposed_action_scope, 'no_action_recommended',
        )

    def test_at_risk_validated_still_produces_no_action_when_evidence_missing(self):
        # Even if we said validated=True, without holdout reproduction
        # the AT_RISK state doesn't produce STATE_COVERAGE_GAP.
        # (The AT_RISK unvalidated-path is short-circuited by
        # at_risk_is_validated=True — the engine then falls through to
        # the normal state-evidence branch which requires holdout
        # reproduction.)
        cands = build_candidates(
            state_evidence={'AT_RISK': _state('AT_RISK', n=3, reproduced=False)},
            transition_evidence=[],
            policies=[],
            at_risk_is_validated=True,
        )
        # No AT_RISK candidate should appear (validated=True skips the
        # INSUFFICIENT_EVIDENCE fallback, and the state branch requires
        # reproduction which failed).
        ar = [c for c in cands if c.subject_state == 'AT_RISK']
        self.assertEqual(ar, [])


# ---------------------------------------------------------------------------
# Transition insights
# ---------------------------------------------------------------------------


class TransitionInsightTests(SimpleTestCase):
    def test_material_transition_emits_observed_state_insight(self):
        cands = build_candidates(
            state_evidence={},
            transition_evidence=[
                TransitionEvidence(
                    previous_state='UNKNOWN', state='ENGAGED',
                    n=23, positive_rate=0.52, lift=-0.10,
                ),
            ],
            policies=[],
        )
        insights = [
            c for c in cands
            if c.rec_class == BehaviorRecommendation.RecClass.OBSERVED_STATE_INSIGHT
        ]
        self.assertEqual(len(insights), 1)
        self.assertEqual(
            insights[0].linked_transition,
            {'previous_state': 'UNKNOWN', 'state': 'ENGAGED'},
        )
        self.assertEqual(
            insights[0].proposed_action_scope, 'monitoring_only',
        )

    def test_underpowered_transition_not_emitted(self):
        cands = build_candidates(
            state_evidence={},
            transition_evidence=[
                TransitionEvidence(
                    previous_state='X', state='Y',
                    n=3, positive_rate=1.0, lift=0.38,
                ),
            ],
            policies=[],
        )
        insights = [
            c for c in cands
            if c.rec_class == BehaviorRecommendation.RecClass.OBSERVED_STATE_INSIGHT
        ]
        self.assertEqual(insights, [])

    def test_near_null_transition_not_emitted(self):
        cands = build_candidates(
            state_evidence={},
            transition_evidence=[
                TransitionEvidence(
                    previous_state='X', state='Y',
                    n=30, positive_rate=0.63, lift=0.01,
                ),
            ],
            policies=[],
        )
        insights = [
            c for c in cands
            if c.rec_class == BehaviorRecommendation.RecClass.OBSERVED_STATE_INSIGHT
        ]
        self.assertEqual(insights, [])


# ---------------------------------------------------------------------------
# LLM prose guard: action_scope=no_action → proposed_action MUST be empty
# ---------------------------------------------------------------------------


class LlmProseGuardTests(SimpleTestCase):
    def test_no_action_scope_wipes_llm_proposed_action(self):
        # LLM tries to sneak in a proposed_action; deterministic guard
        # in draft_prose must wipe it.
        fake_llm = MagicMock()
        fake_llm.analyze.return_value = MagicMock(
            parsed_json={
                'observation': 'x',
                'interpretation': 'y',
                'proposed_action': 'Consider adding a discount rule.',
                'limitations': 'z',
            },
            input_tokens=1, output_tokens=1,
            cost_usd='0.0001',
        )
        from apps.conversations.analysis.recommendation_synthesis import (
            RecommendationCandidate,
        )
        cand = RecommendationCandidate(
            rec_class=BehaviorRecommendation.RecClass.CONFIG_ALIGNMENT,
            confidence=BehaviorRecommendation.Confidence.HIGH,
            proposed_action_scope='no_action_recommended',
        )
        prose = draft_prose(cand, llm_client=fake_llm)
        self.assertEqual(prose.proposed_action, '')  # wiped
        self.assertEqual(prose.observation, 'x')      # kept

    def test_insufficient_evidence_uses_prewritten_prose(self):
        # Prewritten fixed language — LLM NOT called
        fake_llm = MagicMock()
        from apps.conversations.analysis.recommendation_synthesis import (
            RecommendationCandidate,
        )
        cand = RecommendationCandidate(
            rec_class=BehaviorRecommendation.RecClass.INSUFFICIENT_EVIDENCE,
            confidence=BehaviorRecommendation.Confidence.INSUFFICIENT,
            proposed_action_scope='no_action_recommended',
            prewritten_observation='obs text',
            prewritten_interpretation='interp text',
            prewritten_limitations='limits text',
        )
        prose = draft_prose(cand, llm_client=fake_llm)
        fake_llm.analyze.assert_not_called()
        self.assertEqual(prose.observation, 'obs text')
        self.assertEqual(prose.interpretation, 'interp text')
        self.assertEqual(prose.limitations, 'limits text')
        self.assertEqual(prose.proposed_action, '')


# ---------------------------------------------------------------------------
# End-to-end: expected shape for the Spotless corpus scenario
# ---------------------------------------------------------------------------


class SpotlessScenarioTests(SimpleTestCase):
    """Reproduces the shape of the 1B-6 result and asserts the engine
    produces the expected mix of classes."""

    def test_spotless_v3_shape_produces_expected_classes(self):
        state_evidence = {
            'EXPLORING': _state('EXPLORING', n=28, lift=0.13, h_lift=0.21, h_n=6),
            'ENGAGED': _state('ENGAGED', n=30, lift=-0.02, h_lift=-0.02, h_n=5),
            'HIGH_INTENT': _state('HIGH_INTENT', n=48, lift=0.15, h_lift=0.13, h_n=8),
            'BOOKING_INTENT': _state('BOOKING_INTENT', n=57, lift=0.18, h_lift=0.31, h_n=15),
            'AT_RISK': _state('AT_RISK', n=3, reproduced=False),
        }
        transition_evidence = [
            TransitionEvidence('UNKNOWN', 'ENGAGED', n=23, positive_rate=0.52, lift=-0.10),
            TransitionEvidence('UNKNOWN', 'BOOKING_INTENT', n=23, positive_rate=0.87, lift=0.25),
            TransitionEvidence('HIGH_INTENT', 'BOOKING_INTENT', n=30, positive_rate=0.77, lift=0.14),
        ]
        policies = [
            _policy('SERVICE_INQUIRY', ['SERVICE_SCOPE_CLARIFIED']),
            _policy('QUALIFICATION_ANSWER', ['SERVICE_SCOPE_CLARIFIED']),
            _policy('PRICE_REQUESTED', ['PRICE_GIVEN']),
            _policy('AVAILABILITY_REQUESTED', ['TIME_SLOT_OFFERED']),
            _policy('BOOKING_REQUESTED', ['BOOKING_ATTEMPT']),
        ]
        cands = build_candidates(
            state_evidence=state_evidence,
            transition_evidence=transition_evidence,
            policies=policies,
        )
        # ENGAGED should NOT produce a state-coverage rec (near-null lift)
        # HIGH_INTENT partial (DISCOUNT_REQUESTED uncovered)
        # BOOKING_INTENT full → CONFIG_ALIGNMENT
        # EXPLORING partial (QUESTION_FAQ, CALL_REQUESTED, SERVICE_DETAILS_PROVIDED uncovered)
        # UNKNOWN → ENGAGED negative-lift transition → OBSERVED_STATE_INSIGHT
        # UNKNOWN → BOOKING_INTENT positive-lift → OBSERVED_STATE_INSIGHT
        # HIGH_INTENT → BOOKING_INTENT positive-lift → OBSERVED_STATE_INSIGHT
        # AT_RISK unvalidated → INSUFFICIENT_EVIDENCE
        # CUSTOMER_HESITATION quarantine → INSUFFICIENT_EVIDENCE
        classes = [c.rec_class for c in cands]
        # Expected class distribution
        self.assertEqual(
            classes.count(BehaviorRecommendation.RecClass.STATE_PARTIAL_COVERAGE),
            2,  # HIGH_INTENT, EXPLORING
        )
        self.assertEqual(
            classes.count(BehaviorRecommendation.RecClass.CONFIG_ALIGNMENT),
            1,  # BOOKING_INTENT
        )
        self.assertEqual(
            classes.count(BehaviorRecommendation.RecClass.OBSERVED_STATE_INSIGHT),
            3,  # 3 transitions
        )
        self.assertEqual(
            classes.count(BehaviorRecommendation.RecClass.INSUFFICIENT_EVIDENCE),
            2,  # AT_RISK + CUSTOMER_HESITATION
        )
        # No STATE_COVERAGE_GAP because ENGAGED has near-null lift and
        # everything else has partial or full coverage
        self.assertEqual(
            classes.count(BehaviorRecommendation.RecClass.STATE_COVERAGE_GAP),
            0,
        )
        self.assertEqual(len(cands), 8)
