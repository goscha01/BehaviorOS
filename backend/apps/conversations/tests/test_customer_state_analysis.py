"""Pipeline 1B-5 tests: customer-state analysis primitives + classifier."""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.conversations.analysis.conditional import ConversationRecord, Event
from apps.conversations.analysis.customer_state_analysis import (
    _classify, analyze, compute_stats, customer_signal_first_occurrence_order,
    enumerate_ngrams, record_contains_ngram, record_contains_signal,
)


def _ev(et, turn, ordinal=None):
    return Event(
        event_type=et, turn_start=turn,
        ordinal=ordinal if ordinal is not None else turn,
    )


def _rec(cid, klass, events, turn_count=10):
    return ConversationRecord(
        conversation_id=cid, outcome_class=klass,
        lb_status='completed' if klass == 'positive' else 'lost',
        turn_count=turn_count,
        events=events,
    )


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


class FirstOccurrencePrimitiveTests(SimpleTestCase):
    def test_first_occurrence_preserves_temporal_order(self):
        rec = _rec('c1', 'positive', [
            _ev('SERVICE_INQUIRY', 0),
            _ev('QUESTION_FAQ', 1),
            _ev('PRICE_REQUESTED', 2),
            _ev('PRICE_REQUESTED', 5),   # repeat, ignored
            _ev('AVAILABILITY_REQUESTED', 8),
        ])
        order = customer_signal_first_occurrence_order(rec)
        self.assertEqual(
            order,
            ['SERVICE_INQUIRY', 'QUESTION_FAQ',
             'PRICE_REQUESTED', 'AVAILABILITY_REQUESTED'],
        )

    def test_non_customer_signal_events_ignored(self):
        # PRICE_GIVEN is AGENT_ACTION, BOOKING_CONFIRMED is OUTCOME_PROXY —
        # both must be filtered from first-occurrence order.
        rec = _rec('c1', 'positive', [
            _ev('SERVICE_INQUIRY', 0),
            _ev('PRICE_GIVEN', 1),
            _ev('BOOKING_CONFIRMED', 2),
            _ev('AVAILABILITY_REQUESTED', 3),
        ])
        order = customer_signal_first_occurrence_order(rec)
        self.assertEqual(order, ['SERVICE_INQUIRY', 'AVAILABILITY_REQUESTED'])


class NgramEnumerationTests(SimpleTestCase):
    def test_2grams_preserve_order(self):
        ngs = enumerate_ngrams(['A', 'B', 'C'], 2)
        # combinations preserves input order
        self.assertEqual(ngs, [('A', 'B'), ('A', 'C'), ('B', 'C')])

    def test_3grams(self):
        ngs = enumerate_ngrams(['A', 'B', 'C', 'D'], 3)
        self.assertEqual(len(ngs), 4)
        self.assertIn(('A', 'B', 'C'), ngs)
        self.assertIn(('B', 'C', 'D'), ngs)

    def test_ngram_larger_than_input(self):
        self.assertEqual(enumerate_ngrams(['A'], 2), [])


class PresenceTestsTests(SimpleTestCase):
    def test_signal_presence(self):
        rec = _rec('c1', 'positive', [
            _ev('SERVICE_INQUIRY', 0),
            _ev('PRICE_REQUESTED', 3),
        ])
        self.assertTrue(record_contains_signal(rec, 'PRICE_REQUESTED'))
        self.assertFalse(record_contains_signal(rec, 'CUSTOMER_DEFERRED'))

    def test_ngram_presence_non_adjacent(self):
        rec = _rec('c1', 'positive', [
            _ev('SERVICE_INQUIRY', 0),
            _ev('QUESTION_FAQ', 1),
            _ev('PRICE_REQUESTED', 3),
            _ev('AVAILABILITY_REQUESTED', 5),
        ])
        # (SERVICE_INQUIRY, AVAILABILITY_REQUESTED) is present (non-adjacent OK)
        self.assertTrue(record_contains_ngram(
            rec, ('SERVICE_INQUIRY', 'AVAILABILITY_REQUESTED'),
        ))
        # Reversed order is NOT present
        self.assertFalse(record_contains_ngram(
            rec, ('AVAILABILITY_REQUESTED', 'SERVICE_INQUIRY'),
        ))
        # 3-gram in the right order
        self.assertTrue(record_contains_ngram(
            rec, ('SERVICE_INQUIRY', 'PRICE_REQUESTED', 'AVAILABILITY_REQUESTED'),
        ))


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class ClassifierTests(SimpleTestCase):
    def _shell(self, *, diff, ci_low, ci_high, holdout='reproduced'):
        from apps.conversations.analysis.customer_state_analysis import SignalStats
        return SignalStats(
            pattern=('X',), kind='single',
            d_present=20, d_present_pos=15, d_present_neg=5,
            d_absent_pos=50, d_absent_neg=50,
            baseline_pos_rate=0.5,
            d_pos_rate_given_signal=0.75, d_pos_rate_given_absence=0.5,
            d_lift=0.25, d_diff_vs_absence=diff,
            d_diff_ci_low=ci_low, d_diff_ci_high=ci_high,
            len_short_dir='positive', len_long_dir='positive',
            h_present=10, h_present_pos=8, h_present_neg=2,
            h_pos_rate_given_signal=0.8, h_diff_vs_absence=0.3,
            holdout_status=holdout, classification='UNSET',
        )

    def test_high_intent_when_material_positive_ci_excludes_zero_holdout_reproduced(self):
        s = self._shell(diff=+0.25, ci_low=+0.10, ci_high=+0.40)
        self.assertEqual(_classify(s, material_lift=0.10), 'HIGH_INTENT')

    def test_risk_signal_when_material_negative_ci_excludes_zero_holdout_reproduced(self):
        s = self._shell(diff=-0.25, ci_low=-0.40, ci_high=-0.10)
        self.assertEqual(_classify(s, material_lift=0.10), 'RISK_SIGNAL')

    def test_insufficient_when_ci_spans_zero(self):
        s = self._shell(diff=+0.25, ci_low=-0.05, ci_high=+0.55)
        self.assertEqual(_classify(s, material_lift=0.10), 'INSUFFICIENT_EVIDENCE')

    def test_insufficient_when_holdout_not_reproduced(self):
        s = self._shell(diff=+0.25, ci_low=+0.10, ci_high=+0.40,
                         holdout='not_reproduced')
        self.assertEqual(_classify(s, material_lift=0.10), 'INSUFFICIENT_EVIDENCE')

    def test_insufficient_when_holdout_underpowered(self):
        s = self._shell(diff=+0.25, ci_low=+0.10, ci_high=+0.40,
                         holdout='underpowered')
        self.assertEqual(_classify(s, material_lift=0.10), 'INSUFFICIENT_EVIDENCE')

    def test_insufficient_when_lift_below_material_threshold(self):
        s = self._shell(diff=+0.05, ci_low=+0.01, ci_high=+0.09)
        self.assertEqual(_classify(s, material_lift=0.10), 'INSUFFICIENT_EVIDENCE')


# ---------------------------------------------------------------------------
# End-to-end synthetic analyze()
# ---------------------------------------------------------------------------


class AnalyzeSyntheticTests(SimpleTestCase):
    def _corpus_where_signal_predicts_positive(
        self, n_pos=25, n_neg=25, signal='PRICE_REQUESTED',
    ) -> list:
        # Positives all have the signal; negatives don't. Should
        # yield HIGH_INTENT for that signal.
        recs = []
        for i in range(n_pos):
            recs.append(_rec(
                f'p-{i}', 'positive',
                [_ev(signal, 0), _ev('AVAILABILITY_REQUESTED', 1)],
            ))
        for i in range(n_neg):
            recs.append(_rec(
                f'n-{i}', 'negative',
                [_ev('SERVICE_INQUIRY', 0)],
            ))
        return recs

    def test_signal_that_only_appears_in_positives_classified_high_intent(self):
        recs = self._corpus_where_signal_predicts_positive(
            n_pos=25, n_neg=25, signal='PRICE_REQUESTED',
        )
        res = analyze(recs, split_seed=42, min_support_single=5)
        pr = next((s for s in res.singles if s.pattern == ('PRICE_REQUESTED',)), None)
        self.assertIsNotNone(pr)
        self.assertEqual(pr.classification, 'HIGH_INTENT')
        # 100% positive rate in the signal cell
        self.assertAlmostEqual(pr.d_pos_rate_given_signal, 1.0)

    def test_null_corpus_yields_insufficient(self):
        # Signal appears equally in positives and negatives
        recs = []
        for i in range(30):
            recs.append(_rec(
                f'p-{i}', 'positive', [_ev('QUESTION_FAQ', 0)],
            ))
        for i in range(30):
            recs.append(_rec(
                f'n-{i}', 'negative', [_ev('QUESTION_FAQ', 0)],
            ))
        res = analyze(recs, split_seed=42, min_support_single=5)
        q = next((s for s in res.singles if s.pattern == ('QUESTION_FAQ',)), None)
        self.assertIsNotNone(q)
        self.assertEqual(q.classification, 'INSUFFICIENT_EVIDENCE')

    def test_ngram_progression_classified_when_present_only_in_positives(self):
        # SERVICE_INQUIRY -> PRICE_REQUESTED -> AVAILABILITY_REQUESTED
        # in positives; only SERVICE_INQUIRY in negatives.
        recs = []
        for i in range(25):
            recs.append(_rec(
                f'p-{i}', 'positive', [
                    _ev('SERVICE_INQUIRY', 0),
                    _ev('PRICE_REQUESTED', 2),
                    _ev('AVAILABILITY_REQUESTED', 4),
                ],
            ))
        for i in range(25):
            recs.append(_rec(
                f'n-{i}', 'negative', [_ev('SERVICE_INQUIRY', 0)],
            ))
        res = analyze(recs, split_seed=42, min_support_single=5, min_support_ngram=5)
        triad = next(
            (s for s in res.ngrams if s.pattern == (
                'SERVICE_INQUIRY', 'PRICE_REQUESTED', 'AVAILABILITY_REQUESTED',
            )),
            None,
        )
        self.assertIsNotNone(triad)
        self.assertEqual(triad.classification, 'HIGH_INTENT')

    def test_baseline_recorded(self):
        recs = self._corpus_where_signal_predicts_positive()
        res = analyze(recs, split_seed=42, min_support_single=5)
        # 25 pos / 25 neg overall → 50%. Discovery is 80% of each so
        # baseline is 20 pos / 20 neg → 50%.
        self.assertAlmostEqual(res.baseline_pos_rate, 0.5)
        self.assertEqual(res.n_discovery_positive, 20)
        self.assertEqual(res.n_discovery_negative, 20)
