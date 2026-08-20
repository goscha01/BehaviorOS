"""Pipeline 1B-4 tests: config normalizer validation + deterministic
alignment classifier."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.conversations.analysis.config_normalizer import (
    NORMALIZER_VERSION, _validate_and_dedupe, normalize,
)
from apps.conversations.analysis.policy_alignment import (
    EXECUTION_GAP_ALT_MIN, EXECUTION_GAP_PRESCRIBED_MAX,
    QUESTIONABLE_EFFECT_MAX, SUPPORTED_EFFECT_MIN,
    _observed_rates_for_condition, classify,
)


# ---------------------------------------------------------------------------
# Normalizer validation
# ---------------------------------------------------------------------------


class NormalizerValidationTests(SimpleTestCase):
    def test_valid_policy_passes(self):
        accepted, rejected = _validate_and_dedupe([
            {
                'condition_event': 'PROPERTY_DETAILS_PROVIDED',
                'prescribed_action_events': ['SERVICE_SCOPE_CLARIFIED', 'PRICE_GIVEN'],
                'channel': 'text',
                'source_rule_text': 'After details, clarify scope then price.',
                'source_pointer': {'config_path': 'user.global_ai_prompt'},
                'confidence': 0.9,
            }
        ])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 0)
        self.assertEqual(accepted[0].condition_event, 'PROPERTY_DETAILS_PROVIDED')
        self.assertEqual(
            accepted[0].prescribed_action_events,
            ['SERVICE_SCOPE_CLARIFIED', 'PRICE_GIVEN'],
        )

    def test_condition_must_be_customer_signal(self):
        # PRICE_GIVEN is an AGENT_ACTION, not a customer signal.
        _, rejected = _validate_and_dedupe([
            {
                'condition_event': 'PRICE_GIVEN',
                'prescribed_action_events': ['BOOKING_ATTEMPT'],
                'channel': 'text',
                'source_rule_text': 'x',
            }
        ])
        self.assertEqual(len(rejected), 1)
        self.assertIn('unknown condition_event', rejected[0]['reason'])

    def test_action_must_be_agent_action(self):
        # BOOKING_REQUESTED is a customer signal, not an agent action.
        _, rejected = _validate_and_dedupe([
            {
                'condition_event': 'AVAILABILITY_REQUESTED',
                'prescribed_action_events': ['BOOKING_REQUESTED'],
                'channel': 'text',
                'source_rule_text': 'x',
            }
        ])
        self.assertEqual(len(rejected), 1)
        self.assertIn('unknown action event', rejected[0]['reason'])

    def test_outcome_proxy_actions_rejected(self):
        # BOOKING_CONFIRMED is an OUTCOME_PROXY, not an AGENT_ACTION.
        _, rejected = _validate_and_dedupe([
            {
                'condition_event': 'BOOKING_REQUESTED',
                'prescribed_action_events': ['BOOKING_CONFIRMED'],
                'channel': 'text',
                'source_rule_text': 'x',
            }
        ])
        self.assertEqual(len(rejected), 1)

    def test_duplicate_after_normalization_rejected_once(self):
        accepted, rejected = _validate_and_dedupe([
            {
                'condition_event': 'PROPERTY_DETAILS_PROVIDED',
                'prescribed_action_events': ['PRICE_GIVEN'],
                'channel': 'text',
                'source_rule_text': 'v1',
            },
            {
                'condition_event': 'PROPERTY_DETAILS_PROVIDED',
                'prescribed_action_events': ['PRICE_GIVEN'],
                'channel': 'text',
                'source_rule_text': 'v2 (same rule paraphrased)',
            },
        ])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        self.assertIn('duplicate', rejected[0]['reason'])

    def test_confidence_clamped_to_valid_range(self):
        accepted, _ = _validate_and_dedupe([
            {
                'condition_event': 'PRICE_OBJECTION',
                'prescribed_action_events': ['SCOPE_VALUE_EXPLAINED'],
                'channel': 'text',
                'source_rule_text': 'x',
                'confidence': 5.0,
            }
        ])
        self.assertEqual(accepted[0].extraction_confidence, 1.0)

    def test_non_dict_input_rejected(self):
        _, rejected = _validate_and_dedupe(['not a dict', 42, None])
        self.assertEqual(len(rejected), 3)


# ---------------------------------------------------------------------------
# Normalizer end-to-end with mocked LLM
# ---------------------------------------------------------------------------


class NormalizerLlmTests(SimpleTestCase):
    def test_llm_response_flows_through_validation(self):
        mock_client = MagicMock()
        mock_client.analyze.return_value = MagicMock(
            parsed_json={
                'policies': [
                    {
                        'condition_event': 'PROPERTY_DETAILS_PROVIDED',
                        'prescribed_action_events': ['PRICE_GIVEN', 'TIME_SLOT_OFFERED'],
                        'channel': 'text',
                        'source_rule_text': 'After details, price + times.',
                        'source_pointer': {'config_path': 'user.global_ai_prompt'},
                        'confidence': 0.88,
                    },
                    {
                        'condition_event': 'BOGUS_TYPE',        # rejected
                        'prescribed_action_events': ['PRICE_GIVEN'],
                        'channel': 'text',
                    },
                ],
            },
            input_tokens=100, output_tokens=50,
            cost_usd='0.001', model_used='gpt-4o-mini',
        )
        result = normalize({'user': {}}, client=mock_client)
        self.assertEqual(len(result.policies), 1)
        self.assertEqual(len(result.rejected), 1)
        self.assertEqual(result.policies[0].condition_event, 'PROPERTY_DETAILS_PROVIDED')


# ---------------------------------------------------------------------------
# Classifier: deterministic thresholds
# ---------------------------------------------------------------------------


def _make_pattern(condition, action, *, ca_pos=0, ca_neg=0,
                   co_pos=0, co_neg=0, cn_pos=0, cn_neg=0,
                   primary_effect=0.0, status='UNDERPOWERED',
                   holdout='UNDERPOWERED'):
    """Build a stand-in for a ConditionalActionPattern (avoids DB)."""
    p = MagicMock()
    p.condition_event = condition
    p.action_event = action
    p.d_ca_positive = ca_pos
    p.d_ca_negative = ca_neg
    p.d_co_positive = co_pos
    p.d_co_negative = co_neg
    p.d_cn_positive = cn_pos
    p.d_cn_negative = cn_neg
    p.d_primary_effect = primary_effect
    p.d_ca_rate = ca_pos / (ca_pos + ca_neg) if (ca_pos + ca_neg) else 0.0
    p.d_co_rate = co_pos / (co_pos + co_neg) if (co_pos + co_neg) else 0.0
    p.d_primary_ci_low = -1.0
    p.d_primary_ci_high = 1.0
    p.evidence_positive_ids = []
    p.evidence_negative_ids = []
    p.overall_status = status
    p.holdout_status = holdout
    return p


def _make_policy(condition, actions):
    p = MagicMock()
    p.condition_event = condition
    p.prescribed_action_events = actions
    p.channel = 'text'
    p.source_rule_text = 'x'
    return p


class ClassifierTests(SimpleTestCase):
    def test_supported_when_prescribed_has_supported_positive(self):
        policy = _make_policy('AVAILABILITY_REQUESTED', ['TIME_SLOT_OFFERED'])
        patterns = [
            _make_pattern(
                'AVAILABILITY_REQUESTED', 'TIME_SLOT_OFFERED',
                ca_pos=9, ca_neg=1, co_pos=17, co_neg=4,
                primary_effect=+0.15, status='SUPPORTED',
                holdout='HOLDOUT_REPRODUCED',
            ),
        ]
        decision = classify(policy, patterns)
        self.assertEqual(decision.status, 'CONFIG_SUPPORTED')
        self.assertIn('SUPPORTED', decision.rationale)

    def test_questionable_when_prescribed_negative_and_alt_positive(self):
        policy = _make_policy('PROPERTY_DETAILS_PROVIDED', ['PRICE_GIVEN'])
        patterns = [
            _make_pattern(
                'PROPERTY_DETAILS_PROVIDED', 'PRICE_GIVEN',
                ca_pos=6, ca_neg=7, co_pos=9, co_neg=4,
                primary_effect=-0.23, status='SUPPORTED',
                holdout='HOLDOUT_REPRODUCED',
            ),
            _make_pattern(
                'PROPERTY_DETAILS_PROVIDED', 'SERVICE_SCOPE_CLARIFIED',
                ca_pos=8, ca_neg=2, co_pos=6, co_neg=6,
                primary_effect=+0.30, status='SUPPORTED',
                holdout='HOLDOUT_REPRODUCED',
            ),
        ]
        decision = classify(policy, patterns)
        self.assertEqual(decision.status, 'CONFIG_QUESTIONABLE')

    def test_execution_gap_when_prescribed_rare_and_alt_dominates(self):
        # 5% of C observations went to prescribed action, 60% to a
        # different action. Both cells too underpowered to be SUPPORTED
        # (so we don't collide with the earlier branches).
        policy = _make_policy('SERVICE_DETAILS_PROVIDED', ['SERVICE_SCOPE_CLARIFIED'])
        patterns = [
            _make_pattern(
                'SERVICE_DETAILS_PROVIDED', 'SERVICE_SCOPE_CLARIFIED',
                ca_pos=1, ca_neg=1, co_pos=0, co_neg=0,     # 2 obs (5%)
                cn_pos=10, cn_neg=10,                         # CN = 20 (50%)
                primary_effect=0.0, status='UNDERPOWERED',
            ),
            _make_pattern(
                'SERVICE_DETAILS_PROVIDED', 'FOLLOW_UP_SENT',
                ca_pos=1, ca_neg=17, co_pos=0, co_neg=0,     # 18 obs (45%)
                cn_pos=10, cn_neg=10,
                primary_effect=-0.30, status='UNDERPOWERED',
            ),
        ]
        # Total = 2 + 18 + 20 = 40. prescribed rate = 2/40 = 5% (<20%).
        # Alt rate FOLLOW_UP_SENT = 18/40 = 45% (>40%). → EXECUTION_GAP
        decision = classify(policy, patterns)
        self.assertEqual(decision.status, 'EXECUTION_GAP')
        self.assertIn('FOLLOW_UP_SENT', decision.rationale)

    def test_insufficient_evidence_when_nothing_qualifies(self):
        policy = _make_policy('PRICE_OBJECTION', ['SCOPE_VALUE_EXPLAINED'])
        patterns = [
            _make_pattern(
                'PRICE_OBJECTION', 'DISCOUNT_OFFERED',
                ca_pos=1, ca_neg=1, cn_pos=2, cn_neg=2,
                primary_effect=+0.50, status='UNDERPOWERED',
            ),
        ]
        decision = classify(policy, patterns)
        self.assertEqual(decision.status, 'INSUFFICIENT_EVIDENCE')

    def test_no_patterns_at_all_is_insufficient_evidence(self):
        policy = _make_policy('LEAD_MISMATCH', ['HUMAN_HANDOFF'])
        decision = classify(policy, [])
        self.assertEqual(decision.status, 'INSUFFICIENT_EVIDENCE')

    def test_thresholds_module_constants_are_documented(self):
        # Sanity: thresholds shouldn't drift silently. Locks the four
        # values as the classifier's public knobs.
        self.assertEqual(SUPPORTED_EFFECT_MIN, 0.10)
        self.assertEqual(QUESTIONABLE_EFFECT_MAX, -0.10)
        self.assertEqual(EXECUTION_GAP_PRESCRIBED_MAX, 0.20)
        self.assertEqual(EXECUTION_GAP_ALT_MIN, 0.40)


class ObservedRateHelperTests(SimpleTestCase):
    def test_rate_calculation_includes_no_action_baseline(self):
        patterns = [
            _make_pattern('X', 'A', ca_pos=5, ca_neg=5, cn_pos=10, cn_neg=10),
            _make_pattern('X', 'B', ca_pos=10, ca_neg=10, cn_pos=10, cn_neg=10),
        ]
        rates, total = _observed_rates_for_condition('X', patterns)
        # Total = 10 (A) + 20 (B) + 20 (CN) = 50
        self.assertEqual(total, 50)
        self.assertAlmostEqual(rates['A'], 10 / 50)
        self.assertAlmostEqual(rates['B'], 20 / 50)
        self.assertAlmostEqual(rates['__NO_ACTION__'], 20 / 50)

    def test_empty_patterns_returns_empty(self):
        rates, total = _observed_rates_for_condition('X', [])
        self.assertEqual(total, 0)
        self.assertEqual(rates, {})
