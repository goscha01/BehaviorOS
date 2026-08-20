"""Deterministic statistical primitives for the ROM v1 evaluator.

Implemented from stdlib only (math.lgamma) to avoid adding scipy/numpy
just for a two-proportion test. All functions are pure — same inputs
always yield same outputs — so evaluator re-runs are byte-identical.

- fishers_exact_two_sided_p(a, b, c, d) — 2x2 contingency table
- wilson_score_interval(k, n, alpha) — CI for a single proportion at
  small n (much better than normal approximation)
- newcombe_diff_ci(k1, n1, k2, n2, alpha) — CI for difference of
  two proportions (Newcombe hybrid method, small-n appropriate)
"""

from __future__ import annotations

import math


def fishers_exact_two_sided_p(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher's exact p-value for a 2x2 table.

        |        | positive | negative |
        | arm 1  |    a     |    b     |
        | arm 2  |    c     |    d     |

    Sum of hypergeometric probabilities over ALL tables with the same
    marginals that are at least as extreme as the observed one
    (probability <= observed probability). Standard 2-sided
    convention. Returns 1.0 for degenerate tables (any marginal = 0).
    """
    row1 = a + b
    row2 = c + d
    col1 = a + c
    col2 = b + d
    n = a + b + c + d
    if row1 == 0 or row2 == 0 or col1 == 0 or col2 == 0:
        return 1.0

    def logp(x: int) -> float:
        # log P(X=x | row1, col1, n) under the hypergeometric distribution
        return (
            _lchoose(col1, x)
            + _lchoose(col2, row1 - x)
            - _lchoose(n, row1)
        )

    observed_logp = logp(a)
    # x ranges from max(0, col1 - row2) to min(col1, row1)
    x_min = max(0, col1 - row2)
    x_max = min(col1, row1)
    total = 0.0
    for x in range(x_min, x_max + 1):
        lp = logp(x)
        # Tables at least as extreme: probability <= observed. Use a
        # tiny epsilon on the log scale to avoid comparing equal probs
        # unstably.
        if lp <= observed_logp + 1e-12:
            total += math.exp(lp)
    # Clamp for float drift
    return max(0.0, min(1.0, total))


def wilson_score_interval(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for a single proportion.

    Much better small-n behavior than the normal approximation:
    - Never returns bounds outside [0, 1]
    - Reasonable coverage for n as small as 5
    """
    if n == 0:
        return (0.0, 1.0)
    z = _z_from_two_sided_alpha(alpha)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    halfw = (
        z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    ) / denom
    return (max(0.0, center - halfw), min(1.0, center + halfw))


def newcombe_diff_ci(
    k1: int, n1: int, k2: int, n2: int, alpha: float = 0.05,
) -> tuple[float, float]:
    """Newcombe hybrid-score CI for (p1 - p2).

    p1 = k1/n1  (post arm — "treatment")
    p2 = k2/n2  (pre arm  — "baseline")

    Constructed from the Wilson intervals of the two proportions
    individually — well-calibrated at small n and never gives bounds
    outside [-1, 1].
    """
    if n1 == 0 or n2 == 0:
        return (-1.0, 1.0)
    p1 = k1 / n1
    p2 = k2 / n2
    l1, u1 = wilson_score_interval(k1, n1, alpha)
    l2, u2 = wilson_score_interval(k2, n2, alpha)
    diff = p1 - p2
    lower = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    upper = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return (max(-1.0, lower), min(1.0, upper))


# ---------- internals ----------


def _lchoose(n: int, k: int) -> float:
    """log(n choose k) via math.lgamma — exact enough for our
    conversation counts (n well under 1e6)."""
    if k < 0 or k > n:
        return float('-inf')
    return (
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
    )


def _z_from_two_sided_alpha(alpha: float) -> float:
    """Standard-normal critical value for a two-sided alpha. Uses the
    inverse CDF (probit) approximation good to ~4 decimal places, which
    is more than enough for CI construction — we're already only
    displaying the CI to one decimal place."""
    return _probit(1 - alpha / 2)


def _probit(p: float) -> float:
    """Inverse normal CDF — Acklam's approximation. Good to ~1e-4.
    Domain 0 < p < 1."""
    if p <= 0 or p >= 1:
        raise ValueError(f'probit domain: 0 < p < 1, got {p}')
    a = [
        -3.969683028665376e+01, 2.209460984245205e+02,
        -2.759285104469687e+02, 1.383577518672690e+02,
        -3.066479806614716e+01, 2.506628277459239e+00,
    ]
    b = [
        -5.447609879822406e+01, 1.615858368580409e+02,
        -1.556989798598866e+02, 6.680131188771972e+01,
        -1.328068155288572e+01,
    ]
    c = [
        -7.784894002430293e-03, -3.223964580411365e-01,
        -2.400758277161838e+00, -2.549732539343734e+00,
        4.374664141464968e+00, 2.938163982698783e+00,
    ]
    d = [
        7.784695709041462e-03, 3.224671290700398e-01,
        2.445134137142996e+00, 3.754408661907416e+00,
    ]
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (
            (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5])
            / ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(
            (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5])
            / ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q
        / (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)
    )
