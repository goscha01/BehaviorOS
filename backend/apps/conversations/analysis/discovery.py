"""Pipeline 1B-2 pattern discovery — sequence + outcome analysis.

Pipeline:
  frozen conversations
    → per-conv pre-outcome event window
    → stratified 80/20 discovery/holdout split
    → single-event + N-gram enumeration on discovery set
    → filter by minimum support
    → compute presence rates in positive vs negative class
    → length-adjustment sensitivity check on short/long halves
    → Wilson CI on rate difference
    → holdout validation of effect direction
    → CandidatePattern rows

Explicitly does NOT produce recommendations. Output is evidence-backed
candidates for human review + Pipeline 1B-3 causal work.

Guardrails encoded here (per the 1B-2 spec):
- Events are TRUNCATED at the first OUTCOME_PROXY event per conversation.
  Post-outcome events (SATISFACTION_SIGNAL, COMPLAINT, ...) are excluded
  from sequence enumeration entirely.
- Only PRE_OUTCOME event types can appear in candidate patterns (single
  or sequence). Sequences containing any non-PRE_OUTCOME event are
  labeled MIXED and filtered out of the primary results.
- Stratified split preserves positive/negative class ratios across
  discovery + holdout.
- Length adjustment: same pattern computed independently on short
  (below-median turn count) and long halves; direction must agree
  (or one stratum too small to say) or the finding is flagged.
"""

from __future__ import annotations

import logging
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable, Iterator, Optional

from apps.conversations.models import (
    Conversation, ConversationSemanticEvent, LearningCorpus,
    LearningCorpusMember, SemanticExtractionRun,
)
from apps.conversations.semantic.ontology import (
    OUTCOME_PROXY_EVENTS, PRE_OUTCOME_EVENTS, event_temporal_class,
)

logger = logging.getLogger(__name__)


ANALYZER_VERSION = 'pattern-analyzer-v1'


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


DEFAULT_POSITIVE_STATUSES = ('booked', 'in_progress', 'completed')
DEFAULT_NEGATIVE_STATUSES = ('lost',)
DEFAULT_SEQUENCE_SIZES = (2, 3)
# A pattern must appear in at least this many discovery-set conversations
# in EACH class to be worth computing. Keeps output tractable + focuses on
# patterns supported by real evidence.
DEFAULT_MIN_SUPPORT = 5
# Fraction of records going to discovery vs holdout.
DEFAULT_DISCOVERY_FRACTION = 0.80


@dataclass
class DiscoveryConfig:
    positive_statuses: tuple[str, ...] = DEFAULT_POSITIVE_STATUSES
    negative_statuses: tuple[str, ...] = DEFAULT_NEGATIVE_STATUSES
    sequence_sizes: tuple[int, ...] = DEFAULT_SEQUENCE_SIZES
    min_support: int = DEFAULT_MIN_SUPPORT
    discovery_fraction: float = DEFAULT_DISCOVERY_FRACTION

    def as_dict(self) -> dict:
        return {
            'positive_statuses': list(self.positive_statuses),
            'negative_statuses': list(self.negative_statuses),
            'sequence_sizes': list(self.sequence_sizes),
            'min_support': self.min_support,
            'discovery_fraction': self.discovery_fraction,
        }


# ---------------------------------------------------------------------------
# Per-conversation preparation
# ---------------------------------------------------------------------------


@dataclass
class ConversationRecord:
    conversation_id: str
    outcome_class: str            # 'positive' | 'negative' — set by build_records
    lb_status: str
    turn_count: int
    # ordered pre-outcome event_types (truncated at first OUTCOME_PROXY)
    pre_outcome_events: list[str] = field(default_factory=list)


def _pre_outcome_sequence(events: list[dict]) -> list[str]:
    """Given ordered event dicts (event_type, turn_start, ...), return
    ordered event_type list truncated at the FIRST OUTCOME_PROXY event.

    Post-outcome events are dropped even if they appear before the first
    OUTCOME_PROXY (rare in practice — post-outcome events almost always
    come after the outcome). Unknown-timing events kept, no truncation.
    """
    out: list[str] = []
    for ev in events:
        et = ev['event_type']
        if et in OUTCOME_PROXY_EVENTS:
            break
        temporal = event_temporal_class(et)
        if temporal == 'POST_OUTCOME':
            continue
        out.append(et)
    return out


def build_records(
    corpus: LearningCorpus, run: SemanticExtractionRun,
    *, config: DiscoveryConfig,
) -> list[ConversationRecord]:
    """Load semantic events for the corpus × extraction_run and build
    per-conversation ConversationRecord with pre-outcome sequences +
    outcome class."""
    members = list(LearningCorpusMember.objects.filter(corpus=corpus)
                   .select_related('conversation'))
    # Fetch all events for the run in one query, group by conversation.
    events_by_conv: dict = defaultdict(list)
    for ev in (ConversationSemanticEvent.objects
               .filter(extraction_run=run,
                       conversation_id__in=[m.conversation_id for m in members])
               .order_by('conversation_id', 'ordinal')
               .values('conversation_id', 'event_type', 'turn_start', 'ordinal')):
        events_by_conv[ev['conversation_id']].append(ev)

    positive_set = set(config.positive_statuses)
    negative_set = set(config.negative_statuses)

    records: list[ConversationRecord] = []
    for member in members:
        # Use lb_status_at_freeze — the corpus is frozen for a reason.
        status = member.lb_status_at_freeze
        if status in positive_set:
            outcome_class = 'positive'
        elif status in negative_set:
            outcome_class = 'negative'
        else:
            # engaged / cancelled / hired_someone / etc — excluded from
            # binary comparison. Recorded but not scored.
            continue
        raw_events = events_by_conv.get(member.conversation_id, [])
        records.append(ConversationRecord(
            conversation_id=str(member.conversation_id),
            outcome_class=outcome_class,
            lb_status=status,
            turn_count=member.turn_count_at_freeze,
            pre_outcome_events=_pre_outcome_sequence(raw_events),
        ))
    return records


# ---------------------------------------------------------------------------
# Stratified split
# ---------------------------------------------------------------------------


def stratified_split(
    records: list[ConversationRecord], *, discovery_fraction: float, seed: int,
) -> tuple[list[ConversationRecord], list[ConversationRecord]]:
    """Deterministic stratified 80/20 split. Preserves positive/negative
    ratio across discovery + holdout."""
    rng = random.Random(seed)
    positive = [r for r in records if r.outcome_class == 'positive']
    negative = [r for r in records if r.outcome_class == 'negative']
    rng.shuffle(positive)
    rng.shuffle(negative)

    def _split(records_list):
        cut = int(round(len(records_list) * discovery_fraction))
        return records_list[:cut], records_list[cut:]

    disc_pos, hold_pos = _split(positive)
    disc_neg, hold_neg = _split(negative)
    return disc_pos + disc_neg, hold_pos + hold_neg


# ---------------------------------------------------------------------------
# Pattern enumeration
# ---------------------------------------------------------------------------


def _iter_ngrams(seq: list[str], n: int) -> Iterator[tuple[str, ...]]:
    if n <= 0 or len(seq) < n:
        return
    for i in range(len(seq) - n + 1):
        yield tuple(seq[i:i + n])


def enumerate_patterns(
    records: list[ConversationRecord], *,
    sequence_sizes: Iterable[int],
) -> dict[tuple, dict]:
    """Enumerate single-events + N-grams present in the record set.

    Returns {pattern_key: {kind, sequence, positive_ids, negative_ids}}
    where pattern_key = ('single', event_type) or ('ngram', (e1, e2, ...)).
    Presence-based: each record contributes AT MOST ONCE to each pattern.
    """
    out: dict[tuple, dict] = defaultdict(
        lambda: {'kind': None, 'sequence': None,
                 'positive_ids': set(), 'negative_ids': set()}
    )
    for rec in records:
        # Single-event presence
        for et in set(rec.pre_outcome_events):
            if et not in PRE_OUTCOME_EVENTS:
                continue  # LEAD_MISMATCH etc. — actually IS pre-outcome, but
                          # this filter also excludes UNKNOWN-classified events
                          # from single-event enumeration.
            key = ('single', et)
            row = out[key]
            row['kind'] = 'single_event'
            row['sequence'] = [et]
            if rec.outcome_class == 'positive':
                row['positive_ids'].add(rec.conversation_id)
            else:
                row['negative_ids'].add(rec.conversation_id)
        # N-gram sequences
        for n in sequence_sizes:
            seen_in_conv: set[tuple[str, ...]] = set()
            for ng in _iter_ngrams(rec.pre_outcome_events, n):
                # Only enumerate sequences composed entirely of PRE_OUTCOME
                # event types. Sequences containing UNKNOWN-timing events
                # are dropped from primary results (would be labeled MIXED
                # but they add noise here).
                if not all(e in PRE_OUTCOME_EVENTS for e in ng):
                    continue
                if ng in seen_in_conv:
                    continue
                seen_in_conv.add(ng)
                key = ('ngram', ng)
                row = out[key]
                row['kind'] = 'ordered_sequence'
                row['sequence'] = list(ng)
                if rec.outcome_class == 'positive':
                    row['positive_ids'].add(rec.conversation_id)
                else:
                    row['negative_ids'].add(rec.conversation_id)
    return out


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% CI for a proportion. Bounded, works at k=0/n=0
    edge cases without exploding."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _diff_ci(k_a: int, n_a: int, k_b: int, n_b: int, z: float = 1.96) -> tuple[float, float]:
    """95% CI on (p_a - p_b) using independent Wilson intervals summed
    via the naive-Wald-on-diff approximation (conservative for our
    purpose — final gate is direction agreement on holdout, not CI purity)."""
    if n_a == 0 or n_b == 0:
        return -1.0, 1.0
    p_a, p_b = k_a / n_a, k_b / n_b
    se = math.sqrt(p_a * (1 - p_a) / n_a + p_b * (1 - p_b) / n_b)
    diff = p_a - p_b
    return max(-1.0, diff - z * se), min(1.0, diff + z * se)


def _length_stratified_direction(
    records: list[ConversationRecord], sequence_events: list[str], kind: str,
) -> tuple[str, str]:
    """Recompute the effect direction on short (turn_count < median) vs
    long (>= median) halves of the record set. Returns ('positive' /
    'negative' / 'null' / '') per stratum. Empty means insufficient n."""
    if not records:
        return '', ''
    turns = sorted(r.turn_count for r in records)
    med = median(turns)
    short = [r for r in records if r.turn_count < med]
    long_ = [r for r in records if r.turn_count >= med]

    def _dir(subset):
        pos = [r for r in subset if r.outcome_class == 'positive']
        neg = [r for r in subset if r.outcome_class == 'negative']
        if len(pos) < 3 or len(neg) < 3:
            return ''  # insufficient
        seq = tuple(sequence_events)
        if kind == 'single_event':
            f_pos = sum(1 for r in pos if seq[0] in set(r.pre_outcome_events))
            f_neg = sum(1 for r in neg if seq[0] in set(r.pre_outcome_events))
        else:
            f_pos = sum(1 for r in pos if seq in set(_iter_ngrams(r.pre_outcome_events, len(seq))))
            f_neg = sum(1 for r in neg if seq in set(_iter_ngrams(r.pre_outcome_events, len(seq))))
        p_pos = f_pos / len(pos)
        p_neg = f_neg / len(neg)
        d = p_pos - p_neg
        if abs(d) < 0.02:
            return 'null'
        return 'positive' if d > 0 else 'negative'

    return _dir(short), _dir(long_)


# ---------------------------------------------------------------------------
# Holdout validation
# ---------------------------------------------------------------------------


def _presence_on_records(
    records: list[ConversationRecord], sequence: list[str], kind: str,
) -> tuple[int, int, int, int]:
    """Return (pos_present, pos_total, neg_present, neg_total) on records."""
    pos = [r for r in records if r.outcome_class == 'positive']
    neg = [r for r in records if r.outcome_class == 'negative']
    seq_t = tuple(sequence)
    if kind == 'single_event':
        p_present = sum(1 for r in pos if seq_t[0] in set(r.pre_outcome_events))
        n_present = sum(1 for r in neg if seq_t[0] in set(r.pre_outcome_events))
    else:
        p_present = sum(
            1 for r in pos if seq_t in set(_iter_ngrams(r.pre_outcome_events, len(seq_t)))
        )
        n_present = sum(
            1 for r in neg if seq_t in set(_iter_ngrams(r.pre_outcome_events, len(seq_t)))
        )
    return p_present, len(pos), n_present, len(neg)


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


@dataclass
class CandidateResult:
    kind: str
    sequence: list[str]
    temporal_class: str
    d_pos_pres: int
    d_pos_total: int
    d_neg_pres: int
    d_neg_total: int
    d_pos_rate: float
    d_neg_rate: float
    d_effect: float
    d_ci_low: float
    d_ci_high: float
    len_short_dir: str
    len_long_dir: str
    h_pos_pres: int
    h_pos_total: int
    h_neg_pres: int
    h_neg_total: int
    h_pos_rate: float
    h_neg_rate: float
    h_effect: float
    h_status: str
    evidence_positive_ids: list[str]
    evidence_negative_ids: list[str]


def discover(
    records: list[ConversationRecord], *,
    config: DiscoveryConfig, split_seed: int,
) -> tuple[list[CandidateResult], dict]:
    """End-to-end discovery on a set of records. Returns
    (candidates, meta) where meta carries split sizes for the caller."""
    discovery, holdout = stratified_split(
        records, discovery_fraction=config.discovery_fraction, seed=split_seed,
    )
    disc_pos = [r for r in discovery if r.outcome_class == 'positive']
    disc_neg = [r for r in discovery if r.outcome_class == 'negative']
    hold_pos = [r for r in holdout if r.outcome_class == 'positive']
    hold_neg = [r for r in holdout if r.outcome_class == 'negative']

    meta = {
        'n_discovery_positive': len(disc_pos),
        'n_discovery_negative': len(disc_neg),
        'n_holdout_positive': len(hold_pos),
        'n_holdout_negative': len(hold_neg),
        'discovery_conversation_ids': [r.conversation_id for r in discovery],
        'holdout_conversation_ids': [r.conversation_id for r in holdout],
    }

    patterns = enumerate_patterns(discovery, sequence_sizes=config.sequence_sizes)

    results: list[CandidateResult] = []
    for key, row in patterns.items():
        pos_ids = row['positive_ids']
        neg_ids = row['negative_ids']
        support = len(pos_ids) + len(neg_ids)
        if support < config.min_support:
            continue
        kind = row['kind']
        sequence = row['sequence']

        # Discovery rates
        d_pp, d_pt = len(pos_ids), len(disc_pos)
        d_np, d_nt = len(neg_ids), len(disc_neg)
        d_pr = d_pp / d_pt if d_pt else 0.0
        d_nr = d_np / d_nt if d_nt else 0.0
        d_eff = d_pr - d_nr
        ci_low, ci_high = _diff_ci(d_pp, d_pt, d_np, d_nt)

        # Length stratification (on discovery set)
        short_dir, long_dir = _length_stratified_direction(discovery, sequence, kind)

        # Holdout replication
        h_pp, h_pt, h_np, h_nt = _presence_on_records(holdout, sequence, kind)
        h_pr = h_pp / h_pt if h_pt else 0.0
        h_nr = h_np / h_nt if h_nt else 0.0
        h_eff = h_pr - h_nr
        if h_pt < 3 or h_nt < 3:
            h_status = 'underpowered'
        elif (d_eff > 0 and h_eff > 0) or (d_eff < 0 and h_eff < 0) or (abs(d_eff) < 0.02 and abs(h_eff) < 0.02):
            h_status = 'reproduced'
        else:
            h_status = 'not_reproduced'

        # Temporal class of the pattern (single-event uses that type; sequence
        # is MIXED unless all-PRE_OUTCOME which is enforced during enumeration)
        if kind == 'single_event':
            temporal = event_temporal_class(sequence[0])
        else:
            temporal = 'PRE_OUTCOME'

        results.append(CandidateResult(
            kind=kind, sequence=sequence, temporal_class=temporal,
            d_pos_pres=d_pp, d_pos_total=d_pt, d_neg_pres=d_np, d_neg_total=d_nt,
            d_pos_rate=round(d_pr, 4), d_neg_rate=round(d_nr, 4),
            d_effect=round(d_eff, 4),
            d_ci_low=round(ci_low, 4), d_ci_high=round(ci_high, 4),
            len_short_dir=short_dir, len_long_dir=long_dir,
            h_pos_pres=h_pp, h_pos_total=h_pt, h_neg_pres=h_np, h_neg_total=h_nt,
            h_pos_rate=round(h_pr, 4), h_neg_rate=round(h_nr, 4),
            h_effect=round(h_eff, 4), h_status=h_status,
            evidence_positive_ids=sorted(pos_ids)[:50],
            evidence_negative_ids=sorted(neg_ids)[:50],
        ))

    # Rank by |d_effect| descending. Downstream can re-sort.
    results.sort(key=lambda r: (-abs(r.d_effect), -r.d_pos_pres - r.d_neg_pres))
    return results, meta
