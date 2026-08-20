"""Pipeline 1B-4A tests: NO_ACTION classifier — every scenario in the
categorization contract must map deterministically to the right label."""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.conversations.analysis.conditional import Event
from apps.conversations.analysis.no_action_audit import (
    RawTurn, audit, classify_no_action,
)


def _ev(et, turn, ordinal=None):
    """Compact event constructor. ordinal defaults to turn when unset."""
    return Event(
        event_type=et, turn_start=turn,
        ordinal=ordinal if ordinal is not None else turn,
    )


def _turns(specs):
    """Compact ConversationTurn constructor: [(ordinal, speaker), ...]"""
    return [RawTurn(ordinal=o, speaker=s) for o, s in specs]


class ClassifyNoActionTests(SimpleTestCase):
    def test_true_no_response_when_no_agent_turn_anywhere(self):
        c = _ev('PROPERTY_DETAILS_PROVIDED', turn=3)
        # Only customer turns in whole conversation
        turns = _turns([(0, 'customer'), (1, 'customer'), (2, 'customer'),
                        (3, 'customer'), (4, 'customer')])
        reason, _ = classify_no_action(
            reason_raw='end_of_conversation', c_event=c,
            all_events=[c], all_turns=turns, max_turn_distance=20,
        )
        self.assertEqual(reason, 'TRUE_NO_RESPONSE')

    def test_extraction_miss_when_agent_turn_inside_window_but_no_event(self):
        c = _ev('PROPERTY_DETAILS_PROVIDED', turn=3)
        # Agent turn at ordinal=4 (inside the 20-turn window), and the
        # conversation "ends" from the analyzer's perspective (no more
        # semantic events extracted). Classifier should flag EXTRACTION_MISS.
        turns = _turns([
            (0, 'customer'), (1, 'customer'), (2, 'agent'), (3, 'customer'),
            (4, 'agent'), (5, 'customer'),
        ])
        reason, _ = classify_no_action(
            reason_raw='end_of_conversation', c_event=c,
            all_events=[c], all_turns=turns, max_turn_distance=20,
        )
        self.assertEqual(reason, 'EXTRACTION_MISS')

    def test_system_automation_response_when_only_system_turn_inside(self):
        c = _ev('AVAILABILITY_REQUESTED', turn=2)
        # No agent turn at all — but a system turn inside window.
        # Two options: TRUE_NO_RESPONSE (no agent EVER) vs SYSTEM_AUTOMATION.
        # We only classify SYSTEM_AUTOMATION when there's SOMEHOW an agent
        # turn elsewhere in the conversation (i.e., the tenant IS staffed
        # but the specific reply-window was handled by automation).
        # If the conversation is purely customer+system, TRUE_NO_RESPONSE
        # is more honest.
        turns_purely_system = _turns([
            (0, 'customer'), (1, 'customer'), (2, 'customer'),
            (3, 'system'), (4, 'customer'),
        ])
        reason, _ = classify_no_action(
            reason_raw='end_of_conversation', c_event=c,
            all_events=[c], all_turns=turns_purely_system,
            max_turn_distance=20,
        )
        self.assertEqual(reason, 'TRUE_NO_RESPONSE')

        # Now: agent turn exists elsewhere in convo, but the window has
        # only a system turn.
        turns_mixed = _turns([
            (0, 'customer'), (1, 'agent'), (2, 'customer'),
            (3, 'system'),   # inside window
            (4, 'customer'),
        ])
        reason, _ = classify_no_action(
            reason_raw='end_of_conversation', c_event=c,
            all_events=[c], all_turns=turns_mixed, max_turn_distance=20,
        )
        self.assertEqual(reason, 'SYSTEM_AUTOMATION_RESPONSE')

    def test_outcome_proxy_truncated_when_reason_is_reached_outcome(self):
        c = _ev('AVAILABILITY_REQUESTED', turn=2)
        booking = _ev('BOOKING_CONFIRMED', turn=3)  # OUTCOME_PROXY
        # No agent turn between C and outcome (rare but possible if
        # the outcome-proxy event was extracted from customer speech)
        turns = _turns([(0, 'customer'), (1, 'agent'), (2, 'customer'),
                        (3, 'customer')])
        reason, window_end = classify_no_action(
            reason_raw='reached_outcome', c_event=c,
            all_events=[c, booking], all_turns=turns, max_turn_distance=20,
        )
        self.assertEqual(reason, 'OUTCOME_PROXY_TRUNCATED_WINDOW')
        self.assertEqual(window_end, 3)

    def test_customer_immediately_sent_next_signal(self):
        c = _ev('AVAILABILITY_REQUESTED', turn=2)
        next_sig = _ev('BOOKING_REQUESTED', turn=3)  # customer's next signal
        turns = _turns([(0, 'customer'), (1, 'agent'), (2, 'customer'),
                        (3, 'customer'), (4, 'agent')])
        reason, _ = classify_no_action(
            reason_raw='next_customer_signal', c_event=c,
            all_events=[c, next_sig], all_turns=turns,
            max_turn_distance=20,
        )
        self.assertEqual(reason, 'CUSTOMER_IMMEDIATELY_SENT_NEXT_SIGNAL')

    def test_agent_replied_outside_window(self):
        c = _ev('PROPERTY_DETAILS_PROVIDED', turn=0)
        # Agent replied at turn=50 (past max_turn_distance=20)
        turns = _turns(
            [(0, 'customer')]
            + [(i, 'customer') for i in range(1, 50)]
            + [(50, 'agent')]
        )
        reason, window_end = classify_no_action(
            reason_raw='window_expired', c_event=c,
            all_events=[c], all_turns=turns, max_turn_distance=20,
        )
        self.assertEqual(reason, 'AGENT_REPLIED_OUTSIDE_RESPONSE_WINDOW')
        self.assertEqual(window_end, 20)

    def test_conversation_ended_before_reply_when_window_expired_no_agent(self):
        c = _ev('PROPERTY_DETAILS_PROVIDED', turn=0)
        # Agent exists earlier in conv, no agent after C, and total conv
        # is short so window_expired path isn't reached — falls through
        # end_of_conversation with no outside_agent.
        turns = _turns([
            (0, 'customer'),
            (1, 'customer'), (2, 'customer'),
        ])
        # Was there an agent turn ever? Add one before C:
        # Rewrite: agent turn at 0 (before C), C moved to 1
        c2 = _ev('PROPERTY_DETAILS_PROVIDED', turn=1)
        turns2 = _turns([
            (0, 'agent'),        # agent turn EXISTS in conversation
            (1, 'customer'),     # <-- C
            (2, 'customer'),
        ])
        reason, _ = classify_no_action(
            reason_raw='end_of_conversation', c_event=c2,
            all_events=[c2], all_turns=turns2, max_turn_distance=20,
        )
        self.assertEqual(reason, 'CONVERSATION_ENDED_BEFORE_REPLY')


class AuditOrchestrationTests(SimpleTestCase):
    def test_end_to_end_synthetic(self):
        # Two conversations:
        # A) property details → outcome proxy immediately (BOOKING_CONFIRMED)
        # B) property details → true no response (no agent turn anywhere)
        conv_events = {
            'convA': [
                _ev('PROPERTY_DETAILS_PROVIDED', 2, ordinal=0),
                _ev('BOOKING_CONFIRMED', 3, ordinal=1),
            ],
            'convB': [
                _ev('PROPERTY_DETAILS_PROVIDED', 1, ordinal=0),
            ],
        }
        conv_turns = {
            'convA': _turns([
                (0, 'customer'), (1, 'agent'), (2, 'customer'),
                (3, 'customer'),
            ]),
            'convB': _turns([
                (0, 'customer'), (1, 'customer'), (2, 'customer'),
            ]),
        }
        outcomes = {'convA': 'positive', 'convB': 'negative'}
        result = audit(
            conversation_events=conv_events,
            conversation_turns=conv_turns,
            conversation_outcomes=outcomes,
            conditions=('PROPERTY_DETAILS_PROVIDED',),
            max_turn_distance=20,
        )
        self.assertEqual(len(result.entries), 2)
        by = result.by_condition_and_reason()
        self.assertIn(
            ('PROPERTY_DETAILS_PROVIDED', 'OUTCOME_PROXY_TRUNCATED_WINDOW'),
            by,
        )
        self.assertIn(
            ('PROPERTY_DETAILS_PROVIDED', 'TRUE_NO_RESPONSE'),
            by,
        )
        # Positive convA landed in OUTCOME_PROXY bucket
        outcome_proxy_entries = by[
            ('PROPERTY_DETAILS_PROVIDED', 'OUTCOME_PROXY_TRUNCATED_WINDOW')
        ]
        self.assertEqual(outcome_proxy_entries[0].outcome_class, 'positive')
        # Negative convB landed in TRUE_NO_RESPONSE bucket
        no_response_entries = by[
            ('PROPERTY_DETAILS_PROVIDED', 'TRUE_NO_RESPONSE')
        ]
        self.assertEqual(no_response_entries[0].outcome_class, 'negative')

    def test_first_c_per_conversation_only(self):
        # Two PROPERTY_DETAILS_PROVIDED events in same conv — only the
        # first should be audited (mirrors 1B-3 enumeration semantics).
        conv_events = {
            'convA': [
                _ev('PROPERTY_DETAILS_PROVIDED', 1, ordinal=0),
                _ev('PROPERTY_DETAILS_PROVIDED', 8, ordinal=1),
            ],
        }
        conv_turns = {
            'convA': _turns([(i, 'customer') for i in range(15)]),
        }
        outcomes = {'convA': 'negative'}
        result = audit(
            conversation_events=conv_events,
            conversation_turns=conv_turns,
            conversation_outcomes=outcomes,
            conditions=('PROPERTY_DETAILS_PROVIDED',),
            max_turn_distance=20,
        )
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0].c_turn_start, 1)

    def test_multiple_distinct_conditions_each_audited(self):
        # PROPERTY_DETAILS_PROVIDED + AVAILABILITY_REQUESTED both audited
        conv_events = {
            'conv1': [
                _ev('PROPERTY_DETAILS_PROVIDED', 1, ordinal=0),
                _ev('AVAILABILITY_REQUESTED', 5, ordinal=1),
            ],
        }
        conv_turns = {'conv1': _turns([(i, 'customer') for i in range(10)])}
        outcomes = {'conv1': 'negative'}
        result = audit(
            conversation_events=conv_events,
            conversation_turns=conv_turns,
            conversation_outcomes=outcomes,
            conditions=('PROPERTY_DETAILS_PROVIDED', 'AVAILABILITY_REQUESTED'),
            max_turn_distance=20,
        )
        self.assertEqual(len(result.entries), 2)
        conditions = {e.condition_event for e in result.entries}
        self.assertEqual(
            conditions,
            {'PROPERTY_DETAILS_PROVIDED', 'AVAILABILITY_REQUESTED'},
        )
