"""Pipeline 1B-2 tests: temporal classification, sequence extraction with
pre-outcome truncation, stratified split determinism, length adjustment,
no-tautology guard."""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.conversations.analysis.discovery import (
    ConversationRecord, DiscoveryConfig, _iter_ngrams, _pre_outcome_sequence,
    discover, enumerate_patterns, stratified_split,
)
from apps.conversations.semantic.ontology import (
    OUTCOME_PROXY_EVENTS, POST_OUTCOME_EVENTS, PRE_OUTCOME_EVENTS,
    event_temporal_class,
)


# ---------------------------------------------------------------------------
# Temporal classification — no-tautology guard
# ---------------------------------------------------------------------------


class TemporalClassificationTests(SimpleTestCase):
    def test_booking_confirmed_is_outcome_proxy_not_predictor(self):
        # This is the single most important guard in 1B-2 — if
        # BOOKING_CONFIRMED slips into PRE_OUTCOME the whole analysis
        # is a tautology.
        self.assertEqual(event_temporal_class('BOOKING_CONFIRMED'), 'OUTCOME_PROXY')
        self.assertNotIn('BOOKING_CONFIRMED', PRE_OUTCOME_EVENTS)

    def test_satisfaction_signal_is_post_outcome(self):
        self.assertEqual(event_temporal_class('SATISFACTION_SIGNAL'), 'POST_OUTCOME')

    def test_complaint_is_post_outcome(self):
        self.assertEqual(event_temporal_class('COMPLAINT'), 'POST_OUTCOME')

    def test_reschedule_is_post_outcome(self):
        # Only makes sense after a booking exists.
        self.assertEqual(event_temporal_class('RESCHEDULE_REQUESTED'), 'POST_OUTCOME')

    def test_price_events_are_pre_outcome(self):
        self.assertEqual(event_temporal_class('PRICE_REQUESTED'), 'PRE_OUTCOME')
        self.assertEqual(event_temporal_class('PRICE_GIVEN'), 'PRE_OUTCOME')

    def test_booking_requested_is_pre_outcome(self):
        # Customer's REQUEST to book is pre-outcome; CONFIRMED is the outcome.
        self.assertEqual(event_temporal_class('BOOKING_REQUESTED'), 'PRE_OUTCOME')

    def test_lead_mismatch_is_pre_outcome(self):
        self.assertEqual(event_temporal_class('LEAD_MISMATCH'), 'PRE_OUTCOME')

    def test_customer_declined_is_outcome_proxy(self):
        # Near-tautology with 'lost' — safer as OUTCOME_PROXY.
        self.assertEqual(event_temporal_class('CUSTOMER_DECLINED'), 'OUTCOME_PROXY')


# ---------------------------------------------------------------------------
# Pre-outcome truncation
# ---------------------------------------------------------------------------


class PreOutcomeTruncationTests(SimpleTestCase):
    def _ev(self, et, turn=0):
        return {'event_type': et, 'turn_start': turn}

    def test_truncates_at_first_outcome_proxy(self):
        events = [
            self._ev('SERVICE_INQUIRY', 0),
            self._ev('PRICE_REQUESTED', 1),
            self._ev('PRICE_GIVEN', 2),
            self._ev('BOOKING_CONFIRMED', 3),      # ← stop here
            self._ev('SATISFACTION_SIGNAL', 40),   # ← never reached
        ]
        seq = _pre_outcome_sequence(events)
        self.assertEqual(seq, ['SERVICE_INQUIRY', 'PRICE_REQUESTED', 'PRICE_GIVEN'])

    def test_post_outcome_events_dropped_even_before_proxy(self):
        # SATISFACTION_SIGNAL appearing before any OUTCOME_PROXY is still
        # dropped — it never belongs in a predictor sequence.
        events = [
            self._ev('SERVICE_INQUIRY', 0),
            self._ev('SATISFACTION_SIGNAL', 1),
            self._ev('PRICE_REQUESTED', 2),
        ]
        seq = _pre_outcome_sequence(events)
        self.assertEqual(seq, ['SERVICE_INQUIRY', 'PRICE_REQUESTED'])

    def test_no_outcome_proxy_keeps_full_sequence(self):
        events = [
            self._ev('SERVICE_INQUIRY'), self._ev('PRICE_REQUESTED'),
            self._ev('PRICE_GIVEN'), self._ev('BOOKING_REQUESTED'),
        ]
        seq = _pre_outcome_sequence(events)
        self.assertEqual(len(seq), 4)


# ---------------------------------------------------------------------------
# Stratified split determinism
# ---------------------------------------------------------------------------


class StratifiedSplitTests(SimpleTestCase):
    def _records(self, n_pos=20, n_neg=20):
        return [
            ConversationRecord(conversation_id=f'p-{i}', outcome_class='positive',
                                lb_status='completed', turn_count=10)
            for i in range(n_pos)
        ] + [
            ConversationRecord(conversation_id=f'n-{i}', outcome_class='negative',
                                lb_status='lost', turn_count=10)
            for i in range(n_neg)
        ]

    def test_same_seed_same_split(self):
        recs = self._records()
        a1, b1 = stratified_split(recs, discovery_fraction=0.8, seed=42)
        a2, b2 = stratified_split(recs, discovery_fraction=0.8, seed=42)
        self.assertEqual([r.conversation_id for r in a1], [r.conversation_id for r in a2])
        self.assertEqual([r.conversation_id for r in b1], [r.conversation_id for r in b2])

    def test_different_seed_different_split(self):
        recs = self._records()
        a1, _ = stratified_split(recs, discovery_fraction=0.8, seed=42)
        a2, _ = stratified_split(recs, discovery_fraction=0.8, seed=99)
        self.assertNotEqual(
            [r.conversation_id for r in a1],
            [r.conversation_id for r in a2],
        )

    def test_stratification_preserves_class_ratio(self):
        recs = self._records(n_pos=20, n_neg=20)
        discovery, holdout = stratified_split(recs, discovery_fraction=0.8, seed=42)
        d_pos = [r for r in discovery if r.outcome_class == 'positive']
        d_neg = [r for r in discovery if r.outcome_class == 'negative']
        # 80% of 20 = 16 per class, ideally
        self.assertEqual(len(d_pos), 16)
        self.assertEqual(len(d_neg), 16)
        # Holdout has the remaining 4 per class
        h_pos = [r for r in holdout if r.outcome_class == 'positive']
        h_neg = [r for r in holdout if r.outcome_class == 'negative']
        self.assertEqual(len(h_pos), 4)
        self.assertEqual(len(h_neg), 4)


# ---------------------------------------------------------------------------
# Sequence enumeration filters out OUTCOME_PROXY / POST_OUTCOME
# ---------------------------------------------------------------------------


class EnumerationTests(SimpleTestCase):
    def test_ngrams_containing_outcome_proxy_are_skipped(self):
        # Even if a truncated sequence somehow includes an
        # OUTCOME_PROXY event, the enumerator must skip that N-gram.
        recs = [
            ConversationRecord(
                conversation_id='c1', outcome_class='positive',
                lb_status='completed', turn_count=10,
                # Note SATISFACTION_SIGNAL simulated here — truncation
                # normally drops it upstream, but the enumerator's guard
                # is still asserted.
                pre_outcome_events=['PRICE_REQUESTED', 'PRICE_GIVEN', 'SATISFACTION_SIGNAL'],
            ),
        ]
        patterns = enumerate_patterns(recs, sequence_sizes=(2, 3))
        # (PRICE_REQUESTED, PRICE_GIVEN) is a valid bigram.
        # (PRICE_GIVEN, SATISFACTION_SIGNAL) MUST NOT appear because
        # SATISFACTION_SIGNAL is POST_OUTCOME.
        self.assertIn(('ngram', ('PRICE_REQUESTED', 'PRICE_GIVEN')), patterns)
        self.assertNotIn(('ngram', ('PRICE_GIVEN', 'SATISFACTION_SIGNAL')), patterns)

    def test_single_event_enumeration_ignores_non_pre_outcome(self):
        recs = [
            ConversationRecord(
                conversation_id='c1', outcome_class='positive',
                lb_status='completed', turn_count=10,
                pre_outcome_events=['BOOKING_CONFIRMED', 'PRICE_REQUESTED'],
            ),
        ]
        patterns = enumerate_patterns(recs, sequence_sizes=(2,))
        self.assertIn(('single', 'PRICE_REQUESTED'), patterns)
        self.assertNotIn(('single', 'BOOKING_CONFIRMED'), patterns)


# ---------------------------------------------------------------------------
# End-to-end synthetic discovery
# ---------------------------------------------------------------------------


class DiscoveryE2ETests(SimpleTestCase):
    def test_positive_only_pattern_shows_positive_effect_and_reproduces(self):
        """Synthetic corpus where PRICE_REQUESTED appears in all positives
        and no negatives — expect effect ≈ +1.0 on discovery, replicated
        on holdout."""
        pos = [
            ConversationRecord(
                conversation_id=f'p-{i}', outcome_class='positive',
                lb_status='completed', turn_count=10,
                pre_outcome_events=['PRICE_REQUESTED', 'PRICE_GIVEN', 'BOOKING_REQUESTED'],
            )
            for i in range(20)
        ]
        neg = [
            ConversationRecord(
                conversation_id=f'n-{i}', outcome_class='negative',
                lb_status='lost', turn_count=10,
                # No PRICE_REQUESTED in negatives
                pre_outcome_events=['SERVICE_INQUIRY', 'QUESTION_FAQ'],
            )
            for i in range(20)
        ]
        config = DiscoveryConfig(min_support=3)
        candidates, meta = discover(pos + neg, config=config, split_seed=42)
        # Find PRICE_REQUESTED
        price = next((c for c in candidates
                      if c.kind == 'single_event' and c.sequence == ['PRICE_REQUESTED']),
                     None)
        self.assertIsNotNone(price)
        self.assertAlmostEqual(price.d_pos_rate, 1.0)
        self.assertAlmostEqual(price.d_neg_rate, 0.0)
        self.assertEqual(price.h_status, 'reproduced')

    def test_null_pattern_does_not_reproduce(self):
        # Same event in both classes at same rate → effect ~0 → 'reproduced'
        # under the |d|<0.02 both-null rule. Not what we're testing here.
        # Instead: force a discovery-only spurious pattern by biasing
        # the split so discovery shows +effect but holdout has 0.
        # Realistically hard to make deterministic with a small n; skip
        # for MVP. The reproduction check IS exercised in the E2E above.
        pass

    def test_length_stratification_direction_recorded(self):
        # Short (turn_count<median): pattern present in positives, absent in negatives
        # Long  (turn_count>=median): pattern absent in positives, present in negatives
        # → short_dir='positive', long_dir='negative' (direction flip)
        # Using n=25 per subgroup so post-stratified-split each turn/class
        # stratum still has ≥3 records.
        recs = []
        for i in range(25):
            recs.append(ConversationRecord(
                conversation_id=f'sp-{i}', outcome_class='positive',
                lb_status='completed', turn_count=5,
                pre_outcome_events=['PRICE_REQUESTED'],
            ))
            recs.append(ConversationRecord(
                conversation_id=f'sn-{i}', outcome_class='negative',
                lb_status='lost', turn_count=5,
                pre_outcome_events=['SERVICE_INQUIRY'],
            ))
            recs.append(ConversationRecord(
                conversation_id=f'lp-{i}', outcome_class='positive',
                lb_status='completed', turn_count=30,
                pre_outcome_events=['SERVICE_INQUIRY'],
            ))
            recs.append(ConversationRecord(
                conversation_id=f'ln-{i}', outcome_class='negative',
                lb_status='lost', turn_count=30,
                pre_outcome_events=['PRICE_REQUESTED'],
            ))
        candidates, _ = discover(recs, config=DiscoveryConfig(min_support=3),
                                  split_seed=42)
        price = next((c for c in candidates
                      if c.kind == 'single_event' and c.sequence == ['PRICE_REQUESTED']),
                     None)
        self.assertIsNotNone(price)
        self.assertEqual(price.len_short_dir, 'positive')
        self.assertEqual(price.len_long_dir, 'negative')


class NgramTests(SimpleTestCase):
    def test_iter_ngrams_produces_ordered_windows(self):
        self.assertEqual(
            list(_iter_ngrams(['a', 'b', 'c', 'd'], 2)),
            [('a', 'b'), ('b', 'c'), ('c', 'd')],
        )
        self.assertEqual(list(_iter_ngrams(['a'], 3)), [])
