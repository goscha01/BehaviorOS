"""Tests for the stdlib statistical primitives used by the ROM v1
evaluator. Pure-Python (no DB)."""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.conversations.measurement.stats import (
    fishers_exact_two_sided_p, newcombe_diff_ci,
    wilson_score_interval,
)


class FishersExactTests(SimpleTestCase):
    def test_symmetric_table_is_high_p(self):
        # 50/50 in both arms → nothing to reject
        p = fishers_exact_two_sided_p(10, 10, 10, 10)
        self.assertGreater(p, 0.9)

    def test_strong_effect_is_low_p(self):
        # 100% success vs 0% success at n=10 per arm — very rare under null
        p = fishers_exact_two_sided_p(10, 0, 0, 10)
        self.assertLess(p, 0.001)

    def test_moderate_effect_at_v1_sample_floor(self):
        # 24/30 (80%) post vs 18/30 (60%) pre — 20pp effect
        p = fishers_exact_two_sided_p(24, 6, 18, 12)
        # Fisher's exact roughly ~0.05 zone; not tight but well below 0.1
        self.assertLess(p, 0.15)

    def test_degenerate_zero_marginal(self):
        self.assertEqual(fishers_exact_two_sided_p(0, 0, 5, 5), 1.0)
        self.assertEqual(fishers_exact_two_sided_p(5, 5, 0, 0), 1.0)

    def test_result_is_deterministic(self):
        a = fishers_exact_two_sided_p(7, 3, 2, 8)
        b = fishers_exact_two_sided_p(7, 3, 2, 8)
        self.assertEqual(a, b)


class WilsonIntervalTests(SimpleTestCase):
    def test_zero_denominator_returns_widest(self):
        lo, hi = wilson_score_interval(0, 0)
        self.assertEqual((lo, hi), (0.0, 1.0))

    def test_bounds_always_in_unit_interval(self):
        for k, n in [(0, 5), (5, 5), (1, 3), (100, 100)]:
            lo, hi = wilson_score_interval(k, n)
            self.assertGreaterEqual(lo, 0.0)
            self.assertLessEqual(hi, 1.0)
            self.assertLessEqual(lo, hi)

    def test_center_near_observed_at_large_n(self):
        lo, hi = wilson_score_interval(500, 1000, alpha=0.05)
        self.assertAlmostEqual((lo + hi) / 2, 0.5, places=2)


class NewcombeDiffCiTests(SimpleTestCase):
    def test_identical_arms_ci_straddles_zero(self):
        lo, hi = newcombe_diff_ci(10, 20, 10, 20)
        self.assertLess(lo, 0.0)
        self.assertGreater(hi, 0.0)

    def test_zero_denominator_returns_widest(self):
        lo, hi = newcombe_diff_ci(0, 0, 5, 10)
        self.assertEqual((lo, hi), (-1.0, 1.0))

    def test_positive_effect_ci_is_above_zero(self):
        # 80% vs 20% at n=50 per arm — very positive effect
        lo, hi = newcombe_diff_ci(40, 50, 10, 50)
        self.assertGreater(lo, 0.30)
        self.assertLess(hi, 0.85)
        self.assertLess(lo, hi)
