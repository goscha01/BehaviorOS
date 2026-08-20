"""Pipeline 1B-4B tests: action-semantics audit — text extraction from
turns + audit orchestration with a stub classifier."""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.conversations.analysis.action_semantics_audit import (
    MAX_AGENT_TURN_COUNT, MAX_REPLY_TEXT_CHARS, SEMANTIC_CATEGORIES,
    TurnText, audit, extract_first_agent_reply,
)
from apps.conversations.analysis.conditional import Event


def _t(ordinal, speaker, text=''):
    return TurnText(ordinal=ordinal, speaker=speaker, text=text)


def _ev(et, turn, ordinal=None):
    return Event(event_type=et, turn_start=turn,
                 ordinal=ordinal if ordinal is not None else turn)


class ExtractFirstAgentReplyTests(SimpleTestCase):
    def test_returns_empty_when_no_agent_in_window(self):
        turns = [_t(0, 'customer', 'hi'),
                 _t(1, 'customer', 'my details'),
                 _t(2, 'customer', 'anyone there?')]
        text, contrib = extract_first_agent_reply(
            turns, c_turn_start=0, window_end_turn=10,
        )
        self.assertEqual(text, '')
        self.assertEqual(contrib, [])

    def test_captures_first_agent_turn_inside_window(self):
        turns = [_t(0, 'customer', 'details'),
                 _t(1, 'agent', 'Thanks! Price is $150.'),
                 _t(2, 'customer', 'ok')]
        text, contrib = extract_first_agent_reply(
            turns, c_turn_start=0, window_end_turn=5,
        )
        self.assertEqual(text, 'Thanks! Price is $150.')
        self.assertEqual(len(contrib), 1)

    def test_captures_consecutive_agent_run_up_to_cap(self):
        turns = [_t(0, 'customer', 'details')]
        # 5 consecutive agent turns; cap should truncate at MAX_AGENT_TURN_COUNT
        for i in range(1, 6):
            turns.append(_t(i, 'agent', f'msg{i}'))
        turns.append(_t(6, 'customer', 'ok'))
        text, contrib = extract_first_agent_reply(
            turns, c_turn_start=0, window_end_turn=20,
        )
        self.assertEqual(len(contrib), MAX_AGENT_TURN_COUNT)
        self.assertIn('msg1', text)
        self.assertIn(f'msg{MAX_AGENT_TURN_COUNT}', text)
        # msg past the cap should NOT be in text
        self.assertNotIn(f'msg{MAX_AGENT_TURN_COUNT+1}', text)

    def test_stops_on_non_agent_after_agent_run_started(self):
        turns = [_t(0, 'customer', 'details'),
                 _t(1, 'agent', 'reply1'),
                 _t(2, 'customer', 'follow-up'),
                 _t(3, 'agent', 'reply2 — should NOT be included')]
        text, contrib = extract_first_agent_reply(
            turns, c_turn_start=0, window_end_turn=20,
        )
        self.assertEqual(text, 'reply1')
        self.assertEqual(len(contrib), 1)

    def test_respects_window_end(self):
        turns = [_t(0, 'customer', 'details'),
                 _t(1, 'customer', 'more'),
                 _t(2, 'agent', 'outside window — should be excluded')]
        text, contrib = extract_first_agent_reply(
            turns, c_turn_start=0, window_end_turn=1,
        )
        self.assertEqual(text, '')
        self.assertEqual(contrib, [])

    def test_truncates_long_reply_text(self):
        big = 'x' * (MAX_REPLY_TEXT_CHARS + 500)
        turns = [_t(0, 'customer', 'details'),
                 _t(1, 'agent', big)]
        text, _ = extract_first_agent_reply(
            turns, c_turn_start=0, window_end_turn=20,
        )
        # Truncated + ellipsis
        self.assertTrue(text.endswith('…'))
        self.assertEqual(len(text), MAX_REPLY_TEXT_CHARS + 1)

    def test_ignores_turns_at_or_before_c(self):
        turns = [_t(0, 'agent', 'prior — must be ignored'),
                 _t(1, 'customer', 'C is here'),
                 _t(2, 'agent', 'the real reply')]
        text, contrib = extract_first_agent_reply(
            turns, c_turn_start=1, window_end_turn=10,
        )
        self.assertEqual(text, 'the real reply')
        self.assertEqual(len(contrib), 1)


# ---------------------------------------------------------------------------
# Orchestration with a deterministic stub classifier
# ---------------------------------------------------------------------------


def _stub_classifier(mapping):
    """Return a classify_fn that consults `mapping[reply_text_prefix]`
    for the category. Falls back to mixed_or_unclear."""
    def _fn(condition, extracted_action, reply_text):
        for prefix, cat in mapping.items():
            if reply_text.startswith(prefix):
                return cat, 0.9, f'stub matched prefix {prefix!r}'
        return 'mixed_or_unclear', 0.5, 'stub fallback'
    return _fn


class AuditOrchestrationTests(SimpleTestCase):
    def test_end_to_end_with_stub_classifier(self):
        # Two conversations. Both have SERVICE_DETAILS_PROVIDED.
        # convA: agent replies with a substantive next-step
        # convB: agent replies with a generic follow-up
        conv_events = {
            'convA': [_ev('SERVICE_DETAILS_PROVIDED', 1, ordinal=0)],
            'convB': [_ev('SERVICE_DETAILS_PROVIDED', 1, ordinal=0)],
        }
        conv_turns = {
            'convA': [_t(0, 'customer', 'my details'),
                       _t(1, 'customer', 'more details'),
                       _t(2, 'agent', 'PRICE Total is $150 for a deep clean, want to book?')],
            'convB': [_t(0, 'customer', 'my details'),
                       _t(1, 'customer', 'more details'),
                       _t(2, 'agent', 'FOLLOWUP Just checking in — let me know if you need anything.')],
        }
        outcomes = {'convA': 'positive', 'convB': 'negative'}
        classifier = _stub_classifier({
            'PRICE': 'substantive_next_step',
            'FOLLOWUP': 'generic_follow_up',
        })
        result = audit(
            conversation_events=conv_events,
            conversation_turns=conv_turns,
            conversation_outcomes=outcomes,
            condition_event='SERVICE_DETAILS_PROVIDED',
            classify_fn=classifier,
            max_turn_distance=20,
        )
        self.assertEqual(len(result.entries), 2)
        rates = result.outcome_rates_by_category()
        # substantive: 1 pos, 1 total → 1.0
        self.assertEqual(rates['substantive_next_step'], (1, 1, 1.0))
        # generic: 0 pos, 1 total → 0.0
        self.assertEqual(rates['generic_follow_up'], (0, 1, 0.0))

    def test_only_first_occurrence_of_condition_audited(self):
        # Two SERVICE_DETAILS_PROVIDED events in same conv — only the
        # first should be audited (mirrors 1B-3 enumeration semantics)
        conv_events = {
            'conv1': [
                _ev('SERVICE_DETAILS_PROVIDED', 1, ordinal=0),
                _ev('SERVICE_DETAILS_PROVIDED', 8, ordinal=1),
            ],
        }
        conv_turns = {
            'conv1': [_t(i, 'customer' if i != 2 else 'agent',
                          'FIRST' if i == 2 else '') for i in range(15)],
        }
        outcomes = {'conv1': 'positive'}
        classifier = _stub_classifier({'FIRST': 'substantive_next_step'})
        result = audit(
            conversation_events=conv_events,
            conversation_turns=conv_turns,
            conversation_outcomes=outcomes,
            condition_event='SERVICE_DETAILS_PROVIDED',
            classify_fn=classifier,
            max_turn_distance=20,
        )
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0].c_turn_start, 1)

    def test_non_customer_signal_condition_rejected(self):
        # PRICE_GIVEN is an AGENT_ACTION, not a customer signal.
        # The auditor should skip it silently.
        conv_events = {'c1': [_ev('PRICE_GIVEN', 0, ordinal=0)]}
        conv_turns = {'c1': [_t(0, 'agent', 'anything')]}
        result = audit(
            conversation_events=conv_events,
            conversation_turns=conv_turns,
            conversation_outcomes={'c1': 'positive'},
            condition_event='PRICE_GIVEN',
            classify_fn=_stub_classifier({}),
            max_turn_distance=20,
        )
        self.assertEqual(result.entries, [])

    def test_no_agent_turn_yields_true_no_response(self):
        # Customer signals but agent NEVER replies.
        conv_events = {'c1': [_ev('SERVICE_DETAILS_PROVIDED', 0, ordinal=0)]}
        conv_turns = {'c1': [_t(0, 'customer', 'signal'),
                              _t(1, 'customer', 'still there?'),
                              _t(2, 'customer', 'hello?')]}
        result = audit(
            conversation_events=conv_events,
            conversation_turns=conv_turns,
            conversation_outcomes={'c1': 'negative'},
            condition_event='SERVICE_DETAILS_PROVIDED',
            classify_fn=_stub_classifier({}),  # never asked, empty text bypasses
            max_turn_distance=20,
        )
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0].llm_category, 'true_no_response')

    def test_all_semantic_categories_are_valid(self):
        for cat in ('substantive_next_step', 'generic_follow_up',
                    'acknowledgment_only', 'customer_continues_details',
                    'true_no_response', 'mixed_or_unclear'):
            self.assertIn(cat, SEMANTIC_CATEGORIES)
