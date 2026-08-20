"""Pipeline 1B-5 — customer-state & intent benchmark.

Instead of asking "which agent action correlates with outcome given
customer signal C?" (Pipeline 1B-3), this analyzer asks: "does the
customer's OWN signal pattern predict outcome?" The extractor-v3
result showed customer intent baselines (PRICE_REQUESTED at 90%
positive in v2) dominate agent-action variation — this pipeline tests
whether customer signals and their ordered progressions are
themselves the productizable primitive.

Two stages:

1. Single-signal analysis. For each CUSTOMER_SIGNAL type C:
     P(positive | C present in conversation)
   compared against corpus baseline P(positive). Wilson CI on the
   two-proportion difference. Discovery/holdout replication. Length-
   stratified consistency check.

2. Ordered n-gram progression analysis. For each ordered pair or
   triple (A, B[, C]) of distinct CUSTOMER_SIGNAL types where A's
   FIRST occurrence precedes B's FIRST occurrence (etc.), same stats
   as single-signal. Does the ordered progression add information
   beyond individual signals?

Deterministic classifier — no LLM in the analysis path:
  HIGH_INTENT           lift >= +threshold AND CI excludes 0 AND
                        holdout direction reproduces
  RISK_SIGNAL           lift <= -threshold AND CI excludes 0 AND
                        holdout direction reproduces
  INSUFFICIENT_EVIDENCE everything else

Guardrails carried from earlier pipelines:
- LEAD_MISMATCH conversations excluded
- Events truncated at first OUTCOME_PROXY (PRE_OUTCOME view)
- First occurrence per (conversation, C-type) so a customer stating
  the same signal three times counts once
- OUTCOME_PROXY and POST_OUTCOME event types never used as predictors
  (enforced by CUSTOMER_SIGNAL membership check)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from statistics import median
from typing import Iterable, Optional

from apps.conversations.analysis.conditional import (
    ConversationRecord, DiscoveryConfig, stratified_split,
)
from apps.conversations.analysis.discovery import _diff_ci
from apps.conversations.semantic.ontology import CUSTOMER_SIGNAL_EVENTS


ANALYZER_VERSION = 'customer-state-analyzer-v1'


# Defaults — configurable via analyze() kwargs.
DEFAULT_MIN_SUPPORT_SINGLE = 10       # min conversations containing the signal
DEFAULT_MIN_SUPPORT_NGRAM = 8         # min conversations containing the sequence
DEFAULT_MATERIAL_LIFT = 0.10          # |lift vs baseline| >= 0.10 counts as material
DEFAULT_HOLDOUT_MIN_N = 3             # per class on holdout to say anything
DEFAULT_SEQUENCE_SIZES = (2, 3)


# ---------------------------------------------------------------------------
# Sequence + set primitives
# ---------------------------------------------------------------------------


def customer_signal_first_occurrence_order(record: ConversationRecord) -> list[str]:
    """Return distinct CUSTOMER_SIGNAL event_types in the order they
    first appear in the record's pre-outcome event list.

    Preserves temporal order across the conversation. Non-CUSTOMER_SIGNAL
    events are ignored — including OUTCOME_PROXY / POST_OUTCOME which
    are guardrailed out for predictor use.

    (`record.events` is already truncated at first OUTCOME_PROXY by
    `build_records`.)
    """
    seen: set[str] = set()
    order: list[str] = []
    for ev in record.events:
        et = ev.event_type
        if et in CUSTOMER_SIGNAL_EVENTS and et not in seen:
            seen.add(et)
            order.append(et)
    return order


def enumerate_ngrams(order: list[str], n: int) -> list[tuple[str, ...]]:
    """All ordered n-tuples of DISTINCT types drawn from `order` in the
    order they appear. Non-adjacent — captures "A happens then B"
    semantics without requiring adjacency.

    (`combinations` preserves input order which is why we accept
    first-occurrence-ordered list rather than a set.)
    """
    if n <= 0 or len(order) < n:
        return []
    return list(combinations(order, n))


# ---------------------------------------------------------------------------
# Signal-presence tests
# ---------------------------------------------------------------------------


def record_contains_signal(record: ConversationRecord, signal: str) -> bool:
    return any(ev.event_type == signal for ev in record.events)


def record_contains_ngram(record: ConversationRecord, ngram: tuple[str, ...]) -> bool:
    """True iff the record's first-occurrence order contains the ngram
    as a subsequence (in order, not necessarily adjacent)."""
    order = customer_signal_first_occurrence_order(record)
    i, j = 0, 0
    while i < len(order) and j < len(ngram):
        if order[i] == ngram[j]:
            j += 1
        i += 1
    return j == len(ngram)


# ---------------------------------------------------------------------------
# Per-pattern stats
# ---------------------------------------------------------------------------


@dataclass
class SignalStats:
    pattern: tuple[str, ...]
    kind: str                              # 'single' | 'ngram'
    d_present: int                         # discovery: convs w/ pattern
    d_present_pos: int
    d_present_neg: int
    d_absent_pos: int
    d_absent_neg: int
    baseline_pos_rate: float
    d_pos_rate_given_signal: float
    d_pos_rate_given_absence: float
    d_lift: float                          # rate_given_signal - baseline
    d_diff_vs_absence: float               # rate_given_signal - rate_given_absence
    d_diff_ci_low: float
    d_diff_ci_high: float
    len_short_dir: str
    len_long_dir: str
    h_present: int
    h_present_pos: int
    h_present_neg: int
    h_pos_rate_given_signal: float
    h_diff_vs_absence: float
    holdout_status: str                    # 'reproduced' | 'not_reproduced' | 'underpowered'
    classification: str                    # 'HIGH_INTENT' | 'RISK_SIGNAL' | 'INSUFFICIENT_EVIDENCE'
    evidence_positive_ids: list[str] = field(default_factory=list)
    evidence_negative_ids: list[str] = field(default_factory=list)


def _rate(pos: int, neg: int) -> float:
    total = pos + neg
    return pos / total if total > 0 else 0.0


def _length_stratified_direction(
    records: list[ConversationRecord], pattern: tuple[str, ...], kind: str,
    material_lift: float,
) -> tuple[str, str]:
    """Return (short_direction, long_direction) as
    'positive' | 'negative' | 'null' | '' (empty means underpowered)."""
    if not records:
        return '', ''
    turns = sorted(r.turn_count for r in records)
    med = median(turns)
    short = [r for r in records if r.turn_count < med]
    long_ = [r for r in records if r.turn_count >= med]

    def _dir(subset: list[ConversationRecord]) -> str:
        pos = [r for r in subset if r.outcome_class == 'positive']
        neg = [r for r in subset if r.outcome_class == 'negative']
        if len(pos) < 3 or len(neg) < 3:
            return ''
        baseline = _rate(len(pos), len(neg))
        if kind == 'single':
            present_pos = sum(1 for r in pos if record_contains_signal(r, pattern[0]))
            present_neg = sum(1 for r in neg if record_contains_signal(r, pattern[0]))
        else:
            present_pos = sum(1 for r in pos if record_contains_ngram(r, pattern))
            present_neg = sum(1 for r in neg if record_contains_ngram(r, pattern))
        if present_pos + present_neg < 3:
            return ''
        rate_given = _rate(present_pos, present_neg)
        lift = rate_given - baseline
        if abs(lift) < material_lift / 2:
            return 'null'
        return 'positive' if lift > 0 else 'negative'

    return _dir(short), _dir(long_)


def _classify(stats_shell: SignalStats, material_lift: float) -> str:
    lift = stats_shell.d_diff_vs_absence
    ci_low = stats_shell.d_diff_ci_low
    ci_high = stats_shell.d_diff_ci_high
    holdout_ok = stats_shell.holdout_status == 'reproduced'
    material = abs(lift) >= material_lift
    ci_excludes_zero = (ci_low > 0.0) or (ci_high < 0.0)
    if not (material and ci_excludes_zero and holdout_ok):
        return 'INSUFFICIENT_EVIDENCE'
    return 'HIGH_INTENT' if lift > 0 else 'RISK_SIGNAL'


def compute_stats(
    pattern: tuple[str, ...], kind: str,
    discovery: list[ConversationRecord],
    holdout: list[ConversationRecord],
    *, material_lift: float, holdout_min_n: int,
) -> SignalStats:
    """Compute the full stats block for one pattern."""
    d_pos = [r for r in discovery if r.outcome_class == 'positive']
    d_neg = [r for r in discovery if r.outcome_class == 'negative']
    baseline = _rate(len(d_pos), len(d_neg))

    presence_test = (
        (lambda r: record_contains_signal(r, pattern[0])) if kind == 'single'
        else (lambda r: record_contains_ngram(r, pattern))
    )
    d_present_pos = sum(1 for r in d_pos if presence_test(r))
    d_present_neg = sum(1 for r in d_neg if presence_test(r))
    d_absent_pos = len(d_pos) - d_present_pos
    d_absent_neg = len(d_neg) - d_present_neg

    rate_present = _rate(d_present_pos, d_present_neg)
    rate_absent = _rate(d_absent_pos, d_absent_neg)
    lift = rate_present - baseline
    diff_vs_absence = rate_present - rate_absent
    ci_low, ci_high = _diff_ci(
        d_present_pos, d_present_pos + d_present_neg,
        d_absent_pos, d_absent_pos + d_absent_neg,
    )

    short_dir, long_dir = _length_stratified_direction(
        discovery, pattern, kind, material_lift=material_lift,
    )

    # Holdout replication
    h_pos = [r for r in holdout if r.outcome_class == 'positive']
    h_neg = [r for r in holdout if r.outcome_class == 'negative']
    h_present_pos = sum(1 for r in h_pos if presence_test(r))
    h_present_neg = sum(1 for r in h_neg if presence_test(r))
    h_absent_pos = len(h_pos) - h_present_pos
    h_absent_neg = len(h_neg) - h_present_neg
    h_rate_present = _rate(h_present_pos, h_present_neg)
    h_rate_absent = _rate(h_absent_pos, h_absent_neg)
    h_diff = h_rate_present - h_rate_absent
    n_present = h_present_pos + h_present_neg
    n_absent = h_absent_pos + h_absent_neg
    if n_present < holdout_min_n or n_absent < holdout_min_n:
        holdout_status = 'underpowered'
    elif (diff_vs_absence > 0 and h_diff > 0) or (diff_vs_absence < 0 and h_diff < 0):
        holdout_status = 'reproduced'
    elif abs(diff_vs_absence) < material_lift / 4 and abs(h_diff) < material_lift / 4:
        holdout_status = 'reproduced'   # both near-null counts as agree
    else:
        holdout_status = 'not_reproduced'

    stats = SignalStats(
        pattern=pattern, kind=kind,
        d_present=d_present_pos + d_present_neg,
        d_present_pos=d_present_pos, d_present_neg=d_present_neg,
        d_absent_pos=d_absent_pos, d_absent_neg=d_absent_neg,
        baseline_pos_rate=round(baseline, 4),
        d_pos_rate_given_signal=round(rate_present, 4),
        d_pos_rate_given_absence=round(rate_absent, 4),
        d_lift=round(lift, 4),
        d_diff_vs_absence=round(diff_vs_absence, 4),
        d_diff_ci_low=round(ci_low, 4),
        d_diff_ci_high=round(ci_high, 4),
        len_short_dir=short_dir, len_long_dir=long_dir,
        h_present=n_present,
        h_present_pos=h_present_pos, h_present_neg=h_present_neg,
        h_pos_rate_given_signal=round(h_rate_present, 4),
        h_diff_vs_absence=round(h_diff, 4),
        holdout_status=holdout_status,
        classification='INSUFFICIENT_EVIDENCE',
        evidence_positive_ids=[r.conversation_id for r in d_pos if presence_test(r)][:20],
        evidence_negative_ids=[r.conversation_id for r in d_neg if presence_test(r)][:20],
    )
    stats.classification = _classify(stats, material_lift=material_lift)
    return stats


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class CustomerStateResult:
    baseline_pos_rate: float
    n_discovery_positive: int
    n_discovery_negative: int
    n_holdout_positive: int
    n_holdout_negative: int
    singles: list[SignalStats] = field(default_factory=list)
    ngrams: list[SignalStats] = field(default_factory=list)


def analyze(
    records: list[ConversationRecord], *,
    split_seed: int,
    discovery_fraction: float = 0.8,
    sequence_sizes: Iterable[int] = DEFAULT_SEQUENCE_SIZES,
    min_support_single: int = DEFAULT_MIN_SUPPORT_SINGLE,
    min_support_ngram: int = DEFAULT_MIN_SUPPORT_NGRAM,
    material_lift: float = DEFAULT_MATERIAL_LIFT,
    holdout_min_n: int = DEFAULT_HOLDOUT_MIN_N,
) -> CustomerStateResult:
    """End-to-end customer-state analysis. Returns a CustomerStateResult
    containing the corpus baseline, single-signal stats, and n-gram
    stats. Callers rank + report."""
    discovery, holdout = stratified_split(
        records, discovery_fraction=discovery_fraction, seed=split_seed,
    )
    d_pos = [r for r in discovery if r.outcome_class == 'positive']
    d_neg = [r for r in discovery if r.outcome_class == 'negative']
    h_pos = [r for r in holdout if r.outcome_class == 'positive']
    h_neg = [r for r in holdout if r.outcome_class == 'negative']
    baseline = _rate(len(d_pos), len(d_neg))

    result = CustomerStateResult(
        baseline_pos_rate=round(baseline, 4),
        n_discovery_positive=len(d_pos),
        n_discovery_negative=len(d_neg),
        n_holdout_positive=len(h_pos),
        n_holdout_negative=len(h_neg),
    )

    # -------- singles --------
    # Enumerate signals that actually appear (in discovery) with min_support.
    signal_support: dict[str, int] = defaultdict(int)
    for r in discovery:
        for sig in set(customer_signal_first_occurrence_order(r)):
            signal_support[sig] += 1
    for sig in sorted(signal_support):
        if signal_support[sig] < min_support_single:
            continue
        stats = compute_stats(
            (sig,), 'single', discovery, holdout,
            material_lift=material_lift, holdout_min_n=holdout_min_n,
        )
        result.singles.append(stats)

    # -------- n-grams --------
    for n in sequence_sizes:
        ngram_support: dict[tuple[str, ...], int] = defaultdict(int)
        for r in discovery:
            order = customer_signal_first_occurrence_order(r)
            for ng in enumerate_ngrams(order, n):
                ngram_support[ng] += 1
        for ng in sorted(ngram_support.keys()):
            if ngram_support[ng] < min_support_ngram:
                continue
            stats = compute_stats(
                ng, 'ngram', discovery, holdout,
                material_lift=material_lift, holdout_min_n=holdout_min_n,
            )
            result.ngrams.append(stats)

    return result
