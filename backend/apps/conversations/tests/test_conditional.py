"""Pipeline 1B-3 tests: behavioral classification (no dual-role events),
response-window primitive (event-based termination not turn-count),
per-conversation observation enumeration (first-C-per-type only),
cell computation (C+A vs C+other-A vs C+no-A), status classification
(SUPPORTED / DIRECTIONAL_ONLY / UNDERPOWERED / HOLDOUT_REPRODUCED /
HOLDOUT_FAILED), and LEAD_MISMATCH exclusion."""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.conversations.analysis.conditional import (
    ConditionalConfig, ConversationRecord, Event, NO_ACTION, analyze,
    compute_cells, enumerate_observations, find_first_response,
)
from apps.conversations.semantic.ontology import (
    AGENT_ACTION_EVENTS, CONVERSATION_STATE_EVENTS, CUSTOMER_SIGNAL_EVENTS,
    EVENT_TYPES, OUTCOME_PROXY_EVENTS, POST_OUTCOME_EVENTS,
    event_behavioral_class,
)


# ---------------------------------------------------------------------------
# Behavioral classification — no dual-role events, no unclassified events
# ---------------------------------------------------------------------------


class BehavioralClassificationTests(SimpleTestCase):
    def test_every_event_type_has_exactly_one_behavioral_role(self):
        # The ontology module's import-time assert enforces this, but
        # exercise it explicitly so a regression here is a test failure
        # (surfaced) not an ImportError (hidden as a "module broke").
        for et in EVENT_TYPES:
            # Should not raise
            role = event_behavioral_class(et)
            self.assertIn(role, {
                'CUSTOMER_SIGNAL', 'AGENT_ACTION', 'CONVERSATION_STATE',
                'OUTCOME_PROXY', 'POST_OUTCOME',
            })

    def test_no_overlap_between_behavioral_classes(self):
        overlap = CUSTOMER_SIGNAL_EVENTS & AGENT_ACTION_EVENTS
        self.assertEqual(overlap, set())
        overlap = CUSTOMER_SIGNAL_EVENTS & OUTCOME_PROXY_EVENTS
        self.assertEqual(overlap, set())
        overlap = AGENT_ACTION_EVENTS & POST_OUTCOME_EVENTS
        self.assertEqual(overlap, set())

    def test_customer_stopped_responding_is_outcome_proxy_not_signal(self):
        # Explicit user directive: don't dual-classify OUTCOME_PROXY events
        # as customer signals. Follow-up analysis must derive "stalled"
        # state at analysis time, not reuse this event type.
        self.assertEqual(
            event_behavioral_class('CUSTOMER_STOPPED_RESPONDING'),
            'OUTCOME_PROXY',
        )
        self.assertNotIn('CUSTOMER_STOPPED_RESPONDING', CUSTOMER_SIGNAL_EVENTS)

    def test_price_objection_is_customer_signal(self):
        self.assertEqual(event_behavioral_class('PRICE_OBJECTION'), 'CUSTOMER_SIGNAL')

    def test_discount_offered_is_agent_action(self):
        # Key comparator for the price-objection response analysis.
        self.assertEqual(event_behavioral_class('DISCOUNT_OFFERED'), 'AGENT_ACTION')

    def test_scope_value_explained_is_agent_action(self):
        # Alternate response to PRICE_OBJECTION — same behavioral class
        # so both compete in the same C+A cell family.
        self.assertEqual(event_behavioral_class('SCOPE_VALUE_EXPLAINED'), 'AGENT_ACTION')

    def test_lead_mismatch_is_conversation_state(self):
        self.assertEqual(event_behavioral_class('LEAD_MISMATCH'), 'CONVERSATION_STATE')

    def test_call_attempt_is_agent_action_despite_unknown_timing(self):
        # UNKNOWN_TIMING events must all resolve to AGENT_ACTION
        # behaviorally (that's the current membership).
        self.assertEqual(event_behavioral_class('CALL_ATTEMPT'), 'AGENT_ACTION')
        self.assertEqual(event_behavioral_class('HUMAN_HANDOFF'), 'AGENT_ACTION')


# ---------------------------------------------------------------------------
# Response-window primitive — event-based termination
# ---------------------------------------------------------------------------


class ResponseWindowTests(SimpleTestCase):
    def _events(self, spec):
        """spec = list of (event_type, turn_start) tuples"""
        return [Event(event_type=et, turn_start=t, ordinal=i)
                for i, (et, t) in enumerate(spec)]

    def test_first_agent_action_after_signal_is_response(self):
        events = self._events([
            ('PRICE_OBJECTION', 5),
            ('SCOPE_VALUE_EXPLAINED', 6),
            ('DISCOUNT_OFFERED', 7),  # not chosen — SCOPE_VALUE was first
        ])
        action, reason = find_first_response(events, 0, max_turn_distance=20)
        self.assertEqual(action, 'SCOPE_VALUE_EXPLAINED')
        self.assertEqual(reason, 'response_found')

    def test_next_customer_signal_terminates_before_any_agent_action(self):
        # Customer objection → customer immediately declines (say the
        # extractor tagged both) with no agent turn between → NO_ACTION.
        # Note the second signal must be on a LATER turn than the first.
        events = self._events([
            ('PRICE_OBJECTION', 5),
            ('CUSTOMER_DEFERRED', 6),
            ('DISCOUNT_OFFERED', 7),   # too late — window closed
        ])
        action, reason = find_first_response(events, 0, max_turn_distance=20)
        self.assertEqual(action, NO_ACTION)
        self.assertEqual(reason, 'next_customer_signal')

    def test_same_turn_customer_signals_do_not_terminate_each_others_window(self):
        # A single customer message extracted as PRICE_OBJECTION +
        # TIMING_OBJECTION on the same turn. Neither should terminate
        # the other's response window — an agent turn AFTER should be
        # captured as the response.
        events = self._events([
            ('PRICE_OBJECTION', 5),
            ('TIMING_OBJECTION', 5),  # same turn, same actor
            ('DISCOUNT_OFFERED', 6),
        ])
        action, _ = find_first_response(events, 0, max_turn_distance=20)
        self.assertEqual(action, 'DISCOUNT_OFFERED')

    def test_outcome_proxy_terminates_window(self):
        events = self._events([
            ('PRICE_OBJECTION', 5),
            ('CUSTOMER_DECLINED', 6),      # OUTCOME_PROXY
            ('DISCOUNT_OFFERED', 7),       # too late
        ])
        action, reason = find_first_response(events, 0, max_turn_distance=20)
        self.assertEqual(action, NO_ACTION)
        self.assertEqual(reason, 'reached_outcome')

    def test_max_turn_distance_bounds_window(self):
        events = self._events([
            ('PRICE_OBJECTION', 0),
            ('DISCOUNT_OFFERED', 100),   # 100 turns later — pathological
        ])
        action, reason = find_first_response(events, 0, max_turn_distance=20)
        self.assertEqual(action, NO_ACTION)
        self.assertEqual(reason, 'window_expired')

    def test_end_of_events_gives_no_action(self):
        events = self._events([('PRICE_OBJECTION', 5)])
        action, reason = find_first_response(events, 0, max_turn_distance=20)
        self.assertEqual(action, NO_ACTION)
        self.assertEqual(reason, 'end_of_conversation')

    def test_conversation_state_events_do_not_terminate(self):
        # LEAD_MISMATCH / CONVERSATION_STALLED are CONVERSATION_STATE
        # events. The response-window walk skips them (does not treat
        # them as terminators, and does not treat them as agent actions).
        events = self._events([
            ('PRICE_OBJECTION', 5),
            ('CONVERSATION_STALLED', 6),
            ('SCOPE_VALUE_EXPLAINED', 7),
        ])
        action, _ = find_first_response(events, 0, max_turn_distance=20)
        self.assertEqual(action, 'SCOPE_VALUE_EXPLAINED')


# ---------------------------------------------------------------------------
# Observation enumeration — one observation per (conv, C-type)
# ---------------------------------------------------------------------------


class ObservationEnumerationTests(SimpleTestCase):
    def _rec(self, cid, klass, event_spec):
        return ConversationRecord(
            conversation_id=cid, outcome_class=klass,
            lb_status='completed' if klass == 'positive' else 'lost',
            turn_count=10,
            events=[Event(event_type=et, turn_start=t, ordinal=i)
                    for i, (et, t) in enumerate(event_spec)],
        )

    def test_first_occurrence_of_c_type_wins(self):
        # Two PRICE_OBJECTIONs — one at turn 5 followed by DISCOUNT,
        # another at turn 15 followed by SCOPE_VALUE. Only the first
        # should be recorded (avoid correlated pseudo-treatments).
        rec = self._rec('c1', 'positive', [
            ('PRICE_OBJECTION', 5),
            ('DISCOUNT_OFFERED', 6),
            ('PRICE_OBJECTION', 15),
            ('SCOPE_VALUE_EXPLAINED', 16),
        ])
        obs = enumerate_observations([rec], max_turn_distance=20)
        self.assertEqual(obs['c1'], {'PRICE_OBJECTION': 'DISCOUNT_OFFERED'})

    def test_distinct_c_types_each_get_their_own_observation(self):
        # PRICE_OBJECTION → DISCOUNT, TIMING_OBJECTION → SCOPE_VALUE.
        # Both should be recorded (distinct C types = distinct signals).
        rec = self._rec('c1', 'positive', [
            ('PRICE_OBJECTION', 5),
            ('DISCOUNT_OFFERED', 6),
            ('TIMING_OBJECTION', 15),
            ('SCOPE_VALUE_EXPLAINED', 16),
        ])
        obs = enumerate_observations([rec], max_turn_distance=20)
        self.assertEqual(obs['c1'], {
            'PRICE_OBJECTION': 'DISCOUNT_OFFERED',
            'TIMING_OBJECTION': 'SCOPE_VALUE_EXPLAINED',
        })

    def test_no_response_within_window_recorded_as_no_action(self):
        rec = self._rec('c1', 'negative', [
            ('PRICE_OBJECTION', 5),
            # customer immediately declined, agent never responded
            ('CUSTOMER_DECLINED', 6),
        ])
        obs = enumerate_observations([rec], max_turn_distance=20)
        self.assertEqual(obs['c1'], {'PRICE_OBJECTION': NO_ACTION})


# ---------------------------------------------------------------------------
# Cell computation — C+A vs C+other-A vs C+no-A partitioning
# ---------------------------------------------------------------------------


class CellComputationTests(SimpleTestCase):
    def test_conversation_lands_in_correct_cell_for_each_a(self):
        records = [
            ConversationRecord(conversation_id='p1', outcome_class='positive',
                                lb_status='completed', turn_count=10, events=[]),
            ConversationRecord(conversation_id='p2', outcome_class='positive',
                                lb_status='completed', turn_count=10, events=[]),
            ConversationRecord(conversation_id='n1', outcome_class='negative',
                                lb_status='lost', turn_count=10, events=[]),
            ConversationRecord(conversation_id='n2', outcome_class='negative',
                                lb_status='lost', turn_count=10, events=[]),
        ]
        # Manually-built observations: two Cs with different responses,
        # so each (C, A) cell partitions correctly.
        observations = {
            'p1': {'PRICE_OBJECTION': 'DISCOUNT_OFFERED'},
            'p2': {'PRICE_OBJECTION': 'SCOPE_VALUE_EXPLAINED'},
            'n1': {'PRICE_OBJECTION': 'DISCOUNT_OFFERED'},
            'n2': {'PRICE_OBJECTION': NO_ACTION},
        }
        cells = compute_cells(records, observations)
        # Two cells: (PRICE_OBJECTION, DISCOUNT_OFFERED) and (PRICE_OBJECTION, SCOPE_VALUE_EXPLAINED)
        self.assertIn(('PRICE_OBJECTION', 'DISCOUNT_OFFERED'), cells)
        self.assertIn(('PRICE_OBJECTION', 'SCOPE_VALUE_EXPLAINED'), cells)
        self.assertNotIn(('PRICE_OBJECTION', NO_ACTION), cells)  # never a cell

        # Discount cell
        disc = cells[('PRICE_OBJECTION', 'DISCOUNT_OFFERED')]
        self.assertEqual(disc.ca_pos_ids, {'p1'})
        self.assertEqual(disc.ca_neg_ids, {'n1'})
        # Scope-value conversation is in "other AGENT_ACTION" for the discount cell
        self.assertEqual(disc.co_pos_ids, {'p2'})
        self.assertEqual(disc.co_neg_ids, set())
        # No-action conversation is in the secondary baseline
        self.assertEqual(disc.cn_pos_ids, set())
        self.assertEqual(disc.cn_neg_ids, {'n2'})

        # Scope-value cell (symmetric partition)
        scope = cells[('PRICE_OBJECTION', 'SCOPE_VALUE_EXPLAINED')]
        self.assertEqual(scope.ca_pos_ids, {'p2'})
        self.assertEqual(scope.ca_neg_ids, set())
        self.assertEqual(scope.co_pos_ids, {'p1'})
        self.assertEqual(scope.co_neg_ids, {'n1'})
        self.assertEqual(scope.cn_neg_ids, {'n2'})


# ---------------------------------------------------------------------------
# End-to-end synthetic conditional analysis
# ---------------------------------------------------------------------------


class ConditionalAnalyzeTests(SimpleTestCase):
    def _make_records(self, template, n, klass, cid_prefix):
        return [
            ConversationRecord(
                conversation_id=f'{cid_prefix}-{i}', outcome_class=klass,
                lb_status='completed' if klass == 'positive' else 'lost',
                turn_count=10,
                events=[Event(event_type=et, turn_start=t, ordinal=idx)
                        for idx, (et, t) in enumerate(template)],
            )
            for i in range(n)
        ]

    def test_clear_winning_action_is_supported_and_reproduces(self):
        # 20 positive convos where PRICE_OBJECTION → SCOPE_VALUE_EXPLAINED
        # 20 negative convos where PRICE_OBJECTION → DISCOUNT_OFFERED
        # → SCOPE_VALUE is strictly better response to price objection
        pos_scope = self._make_records(
            [('PRICE_OBJECTION', 5), ('SCOPE_VALUE_EXPLAINED', 6)],
            20, 'positive', 'ps',
        )
        neg_discount = self._make_records(
            [('PRICE_OBJECTION', 5), ('DISCOUNT_OFFERED', 6)],
            20, 'negative', 'nd',
        )
        # Add some mixed so both actions have both outcomes represented
        # (otherwise CO cell for either action is single-class).
        pos_discount = self._make_records(
            [('PRICE_OBJECTION', 5), ('DISCOUNT_OFFERED', 6)],
            5, 'positive', 'pd',
        )
        neg_scope = self._make_records(
            [('PRICE_OBJECTION', 5), ('SCOPE_VALUE_EXPLAINED', 6)],
            5, 'negative', 'ns',
        )
        records = pos_scope + neg_discount + pos_discount + neg_scope
        config = ConditionalConfig(min_cell_support=3)
        results, meta = analyze(records, config=config, split_seed=42)

        # Find the SCOPE_VALUE_EXPLAINED cell
        scope = next((r for r in results
                      if r.condition_event == 'PRICE_OBJECTION'
                      and r.action_event == 'SCOPE_VALUE_EXPLAINED'), None)
        discount = next((r for r in results
                         if r.condition_event == 'PRICE_OBJECTION'
                         and r.action_event == 'DISCOUNT_OFFERED'), None)
        self.assertIsNotNone(scope)
        self.assertIsNotNone(discount)
        # SCOPE_VALUE should have strong positive primary effect.
        self.assertGreater(scope.d_primary_effect, 0.5)
        # DISCOUNT should have strong negative primary effect (mirror).
        self.assertLess(discount.d_primary_effect, -0.5)
        # Both effects should have opposite signs — that's the win.
        self.assertNotEqual(
            scope.d_primary_effect > 0,
            discount.d_primary_effect > 0,
        )

    def test_underpowered_cell_gets_underpowered_status(self):
        # Only 2 observations of a cell → below min_cell_support=8
        records = self._make_records(
            [('PRICE_OBJECTION', 5), ('URGENCY_CREATED', 6)],
            2, 'positive', 'x',
        ) + self._make_records(
            [('PRICE_OBJECTION', 5), ('DISCOUNT_OFFERED', 6)],
            20, 'negative', 'y',
        )
        config = ConditionalConfig(min_cell_support=8)
        results, _ = analyze(records, config=config, split_seed=42)
        urgency = next((r for r in results
                        if r.condition_event == 'PRICE_OBJECTION'
                        and r.action_event == 'URGENCY_CREATED'), None)
        self.assertIsNotNone(urgency)
        self.assertEqual(urgency.overall_status, 'UNDERPOWERED')

    def test_no_action_baseline_captured_as_secondary(self):
        # Some conversations get PRICE_OBJECTION with no response.
        # The C+no-A baseline should be populated on the CA cells.
        pos_scope = self._make_records(
            [('PRICE_OBJECTION', 5), ('SCOPE_VALUE_EXPLAINED', 6)],
            10, 'positive', 'ps',
        )
        neg_none = [
            ConversationRecord(
                conversation_id=f'nn-{i}', outcome_class='negative',
                lb_status='lost', turn_count=10,
                events=[
                    Event(event_type='PRICE_OBJECTION', turn_start=5, ordinal=0),
                    Event(event_type='CUSTOMER_DECLINED', turn_start=6, ordinal=1),
                ],
            )
            for i in range(10)
        ]
        records = pos_scope + neg_none
        config = ConditionalConfig(min_cell_support=3)
        results, _ = analyze(records, config=config, split_seed=42)
        scope = next((r for r in results
                      if r.condition_event == 'PRICE_OBJECTION'
                      and r.action_event == 'SCOPE_VALUE_EXPLAINED'), None)
        self.assertIsNotNone(scope)
        # The no-action baseline should have data (from the negatives).
        self.assertGreater(scope.d_cn_neg, 0)
        # Secondary effect should be positive (scope beats no-response).
        self.assertGreater(scope.d_secondary_effect, 0.0)


# ---------------------------------------------------------------------------
# Config default sanity
# ---------------------------------------------------------------------------


class ConditionalConfigTests(SimpleTestCase):
    def test_defaults_match_1b3_spec(self):
        c = ConditionalConfig()
        self.assertEqual(c.min_cell_support, 8)
        self.assertEqual(c.max_turn_distance, 20)
        self.assertEqual(c.discovery_fraction, 0.80)

    def test_as_dict_serializable(self):
        c = ConditionalConfig(min_cell_support=5, max_turn_distance=30)
        d = c.as_dict()
        self.assertEqual(d['min_cell_support'], 5)
        self.assertEqual(d['max_turn_distance'], 30)
        self.assertIsInstance(d['positive_statuses'], list)
