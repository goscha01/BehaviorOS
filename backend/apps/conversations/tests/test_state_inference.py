"""Pipeline 1B-6 tests: state-inference rules + non-monotonic movement
+ quarantine + AT_RISK accumulation."""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.conversations.analysis.conditional import Event
from apps.conversations.analysis.state_inference import (
    AT_RISK_MIN_SIGNALS, INFERENCE_VERSION, QUARANTINED_SIGNALS,
    STATE_AT_RISK, STATE_BOOKING_INTENT, STATE_ENGAGED, STATE_EXPLORING,
    STATE_HIGH_INTENT, STATE_UNKNOWN, infer_state_history,
)


def _ev(et, turn, ordinal=None):
    return Event(
        event_type=et, turn_start=turn,
        ordinal=ordinal if ordinal is not None else turn,
    )


# ---------------------------------------------------------------------------
# Monotonic progression
# ---------------------------------------------------------------------------


class MonotonicProgressionTests(SimpleTestCase):
    def test_single_exploring_signal_moves_to_exploring(self):
        h = infer_state_history(
            [_ev('SERVICE_INQUIRY', 0)], conversation_id='c1',
        )
        self.assertEqual(len(h.transitions), 1)
        self.assertEqual(h.transitions[0].state, STATE_EXPLORING)
        self.assertEqual(h.transitions[0].previous_state, STATE_UNKNOWN)
        self.assertEqual(h.final_state(), STATE_EXPLORING)

    def test_progression_unknown_to_booking_intent(self):
        events = [
            _ev('SERVICE_INQUIRY', 0),
            _ev('PROPERTY_DETAILS_PROVIDED', 2),
            _ev('PRICE_REQUESTED', 4),
            _ev('BOOKING_REQUESTED', 6),
        ]
        h = infer_state_history(events, 'c1')
        states = [t.state for t in h.transitions]
        self.assertEqual(states, [
            STATE_EXPLORING, STATE_ENGAGED, STATE_HIGH_INTENT, STATE_BOOKING_INTENT,
        ])
        self.assertEqual(h.final_state(), STATE_BOOKING_INTENT)

    def test_same_state_signal_repeat_does_not_emit_transition(self):
        events = [
            _ev('PRICE_REQUESTED', 0),
            _ev('AVAILABILITY_REQUESTED', 2),  # same state HIGH_INTENT
        ]
        h = infer_state_history(events, 'c1')
        self.assertEqual(len(h.transitions), 1)
        self.assertEqual(h.transitions[0].state, STATE_HIGH_INTENT)

    def test_lower_state_signal_after_higher_does_not_regress(self):
        events = [
            _ev('BOOKING_REQUESTED', 0),
            _ev('SERVICE_INQUIRY', 5),  # would evidence EXPLORING but we're already BOOKING_INTENT
        ]
        h = infer_state_history(events, 'c1')
        self.assertEqual(len(h.transitions), 1)
        self.assertEqual(h.final_state(), STATE_BOOKING_INTENT)


# ---------------------------------------------------------------------------
# Multi-path aggregation
# ---------------------------------------------------------------------------


class AggregationTests(SimpleTestCase):
    def test_high_intent_reachable_from_price_only(self):
        h = infer_state_history([_ev('PRICE_REQUESTED', 0)], 'c1')
        self.assertEqual(h.final_state(), STATE_HIGH_INTENT)

    def test_high_intent_reachable_from_availability_only(self):
        h = infer_state_history([_ev('AVAILABILITY_REQUESTED', 0)], 'c1')
        self.assertEqual(h.final_state(), STATE_HIGH_INTENT)

    def test_high_intent_reachable_from_discount_only(self):
        h = infer_state_history([_ev('DISCOUNT_REQUESTED', 0)], 'c1')
        self.assertEqual(h.final_state(), STATE_HIGH_INTENT)

    def test_engaged_reachable_from_property_or_qualification(self):
        h1 = infer_state_history([_ev('PROPERTY_DETAILS_PROVIDED', 0)], 'c1')
        self.assertEqual(h1.final_state(), STATE_ENGAGED)
        h2 = infer_state_history([_ev('QUALIFICATION_ANSWER', 0)], 'c2')
        self.assertEqual(h2.final_state(), STATE_ENGAGED)


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


class QuarantineTests(SimpleTestCase):
    def test_customer_hesitation_is_quarantined(self):
        self.assertIn('CUSTOMER_HESITATION', QUARANTINED_SIGNALS)

    def test_customer_hesitation_alone_does_not_drive_state(self):
        h = infer_state_history([_ev('CUSTOMER_HESITATION', 0)], 'c1')
        self.assertEqual(len(h.transitions), 0)
        self.assertEqual(h.final_state(), STATE_UNKNOWN)

    def test_customer_hesitation_between_signals_is_no_op(self):
        events = [
            _ev('PRICE_REQUESTED', 0),
            _ev('CUSTOMER_HESITATION', 2),
            _ev('BOOKING_REQUESTED', 4),
        ]
        h = infer_state_history(events, 'c1')
        # HIGH_INTENT then BOOKING_INTENT — hesitation ignored
        states = [t.state for t in h.transitions]
        self.assertEqual(states, [STATE_HIGH_INTENT, STATE_BOOKING_INTENT])


# ---------------------------------------------------------------------------
# AT_RISK accumulation + non-monotonic
# ---------------------------------------------------------------------------


class AtRiskTests(SimpleTestCase):
    def test_single_risk_signal_does_not_enter_at_risk(self):
        h = infer_state_history([
            _ev('PRICE_REQUESTED', 0),
            _ev('PRICE_OBJECTION', 2),
        ], 'c1')
        # HIGH_INTENT only — one risk signal insufficient
        self.assertEqual(h.final_state(), STATE_HIGH_INTENT)

    def test_two_risk_signals_trigger_at_risk_from_high_intent(self):
        h = infer_state_history([
            _ev('PRICE_REQUESTED', 0),
            _ev('PRICE_OBJECTION', 2),
            _ev('CUSTOMER_DEFERRED', 4),
        ], 'c1')
        # HIGH_INTENT then AT_RISK
        states = [t.state for t in h.transitions]
        self.assertEqual(states, [STATE_HIGH_INTENT, STATE_AT_RISK])
        # Transition preserves the previous state so recovery-tracking works
        self.assertEqual(h.transitions[1].previous_state, STATE_HIGH_INTENT)

    def test_recovery_from_at_risk_on_higher_signal(self):
        h = infer_state_history([
            _ev('PRICE_REQUESTED', 0),
            _ev('PRICE_OBJECTION', 2),
            _ev('CUSTOMER_DEFERRED', 4),
            _ev('BOOKING_REQUESTED', 6),  # recovers to BOOKING_INTENT
        ], 'c1')
        states = [t.state for t in h.transitions]
        self.assertEqual(states, [
            STATE_HIGH_INTENT, STATE_AT_RISK, STATE_BOOKING_INTENT,
        ])
        # Recovery reason is documented
        self.assertIn('recovery', h.transitions[2].reason.lower())

    def test_at_risk_provenance_lists_all_triggering_events(self):
        h = infer_state_history([
            _ev('PRICE_REQUESTED', 0),
            _ev('PRICE_OBJECTION', 2, ordinal=10),
            _ev('CUSTOMER_DEFERRED', 4, ordinal=20),
        ], 'c1')
        at_risk_t = h.transitions[1]
        self.assertEqual(at_risk_t.state, STATE_AT_RISK)
        # Both risk signals present in provenance
        self.assertEqual(
            sorted(at_risk_t.trigger_event_types),
            sorted(['PRICE_OBJECTION', 'CUSTOMER_DEFERRED']),
        )
        self.assertEqual(sorted(at_risk_t.trigger_event_ordinals), [10, 20])


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class ProvenanceTests(SimpleTestCase):
    def test_transition_records_inference_version(self):
        h = infer_state_history([_ev('BOOKING_REQUESTED', 0)], 'c1')
        self.assertEqual(h.transitions[0].inference_version, INFERENCE_VERSION)

    def test_transition_records_trigger_ordinals(self):
        h = infer_state_history(
            [_ev('PRICE_REQUESTED', 3, ordinal=7)], 'c1',
        )
        self.assertEqual(h.transitions[0].trigger_event_ordinals, [7])
        self.assertEqual(h.transitions[0].effective_turn, 3)

    def test_events_visited_and_entered_helpers(self):
        h = infer_state_history([
            _ev('PRICE_REQUESTED', 0),
            _ev('BOOKING_REQUESTED', 2),
        ], 'c1')
        self.assertTrue(h.entered(STATE_HIGH_INTENT))
        self.assertTrue(h.entered(STATE_BOOKING_INTENT))
        self.assertFalse(h.entered(STATE_AT_RISK))
        self.assertEqual(
            h.states_visited(),
            {STATE_HIGH_INTENT, STATE_BOOKING_INTENT},
        )
