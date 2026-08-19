"""Pipeline 1B-3 conditional-action analysis.

The 1B-2 predecessor asked: which event sequences correlate with which
outcomes? That produced 127 candidates but many were customer intent
signals (e.g. BOOKING_REQUESTED at +0.48) that we cannot teach an agent
to elicit. This module asks the follow-up question:

    Given the same customer signal C, which agent action A appears to
    work better than the other agent actions available?

Only AGENT_ACTIONs are teachable behaviors. This pipeline enumerates
(condition, action) cells across the corpus and computes:

    primary  : rate_positive( C + A )  vs  rate_positive( C + other AGENT_ACTION )
    secondary: rate_positive( C + A )  vs  rate_positive( C + no response )

The primary comparator (within-condition, action-vs-other-action) is the
one that answers the business question. The secondary "no response"
baseline is reported but not the ranking metric — no-response is
confounded by channel gaps, handoffs, and conversation endings.

Guardrails (per the 1B-3 design):
- LEAD_MISMATCH conversations are excluded from denominators entirely.
- Analysis window is truncated at the first OUTCOME_PROXY event.
- Per (conversation, C-type) we take only the FIRST occurrence of C and
  its first-response A. This prevents one price objection followed by
  five agent messages from generating five correlated pseudo-treatments.
- The response-window terminator is event-based (next CUSTOMER_SIGNAL,
  OUTCOME_PROXY, or POST_OUTCOME) — NOT a turn-count window. A
  configurable max_turn_distance is applied on top as a safety bound.
- Same 80/20 stratified split as 1B-2 for holdout replication (reuses
  the seed).
- Overall status per cell: SUPPORTED / DIRECTIONAL_ONLY / UNDERPOWERED.
- Holdout: HOLDOUT_REPRODUCED / HOLDOUT_FAILED / UNDERPOWERED.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import Optional

from apps.conversations.analysis.discovery import (
    DiscoveryConfig, _diff_ci, stratified_split,
)
from apps.conversations.models import (
    ConversationSemanticEvent, LearningCorpus, LearningCorpusMember,
    SemanticExtractionRun,
)
from apps.conversations.semantic.ontology import (
    AGENT_ACTION_EVENTS, CUSTOMER_SIGNAL_EVENTS, LEAD_MISMATCH,
    OUTCOME_PROXY_EVENTS, POST_OUTCOME_EVENTS, event_behavioral_class,
    event_temporal_class,
)

logger = logging.getLogger(__name__)


ANALYZER_VERSION = 'conditional-analyzer-v1'

# Sentinel action type used to represent "no eligible agent response was
# found in the window after the customer signal." Not an ontology event
# type — analyzer-only.
NO_ACTION = 'NO_ACTION'


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


DEFAULT_MIN_CELL_SUPPORT = 8
DEFAULT_MAX_TURN_DISTANCE = 20


@dataclass
class ConditionalConfig:
    # Reuse split + outcome definitions from 1B-2 discovery for consistency.
    positive_statuses: tuple[str, ...] = DiscoveryConfig().positive_statuses
    negative_statuses: tuple[str, ...] = DiscoveryConfig().negative_statuses
    discovery_fraction: float = DiscoveryConfig().discovery_fraction
    # NEW: minimum conversations in the (C, A) cell to attempt an effect
    # estimate. Below this we mark UNDERPOWERED and store the counts but
    # do not report as SUPPORTED.
    min_cell_support: int = DEFAULT_MIN_CELL_SUPPORT
    # NEW: safety bound on the response window. Event-based terminators
    # (next customer signal, outcome proxy, post-outcome) usually close
    # the window sooner; this is a hard cap for pathological gaps.
    max_turn_distance: int = DEFAULT_MAX_TURN_DISTANCE

    def as_dict(self) -> dict:
        return {
            'positive_statuses': list(self.positive_statuses),
            'negative_statuses': list(self.negative_statuses),
            'discovery_fraction': self.discovery_fraction,
            'min_cell_support': self.min_cell_support,
            'max_turn_distance': self.max_turn_distance,
        }


# ---------------------------------------------------------------------------
# Per-conversation preparation
# ---------------------------------------------------------------------------


@dataclass
class Event:
    """Lightweight event tuple used by the response-window walk."""
    event_type: str
    turn_start: int
    ordinal: int


@dataclass
class ConversationRecord:
    conversation_id: str
    outcome_class: str            # 'positive' | 'negative'
    lb_status: str
    turn_count: int
    events: list[Event] = field(default_factory=list)   # already truncated


def _truncate_at_first_outcome_proxy(events: list[Event]) -> list[Event]:
    """Drop everything from the first OUTCOME_PROXY event onward, and
    silently drop any POST_OUTCOME events that (rarely) appear before it.
    Mirrors the 1B-2 truncation semantics."""
    out: list[Event] = []
    for ev in events:
        if ev.event_type in OUTCOME_PROXY_EVENTS:
            break
        if ev.event_type in POST_OUTCOME_EVENTS:
            continue
        out.append(ev)
    return out


def build_records(
    corpus: LearningCorpus, run: SemanticExtractionRun,
    *, config: ConditionalConfig,
) -> tuple[list[ConversationRecord], dict]:
    """Load semantic events for corpus × extraction_run and build
    per-conversation records with truncated pre-outcome event lists.

    Returns (records, meta) where meta includes counts of excluded
    conversations (LEAD_MISMATCH + status-not-in-binary-classes).
    """
    members = list(LearningCorpusMember.objects.filter(corpus=corpus)
                   .select_related('conversation'))
    events_by_conv: dict = defaultdict(list)
    for ev in (ConversationSemanticEvent.objects
               .filter(extraction_run=run,
                       conversation_id__in=[m.conversation_id for m in members])
               .order_by('conversation_id', 'ordinal')
               .values('conversation_id', 'event_type',
                       'turn_start', 'ordinal')):
        events_by_conv[ev['conversation_id']].append(Event(
            event_type=ev['event_type'],
            turn_start=ev['turn_start'],
            ordinal=ev['ordinal'],
        ))

    positive_set = set(config.positive_statuses)
    negative_set = set(config.negative_statuses)

    records: list[ConversationRecord] = []
    n_excluded_lead_mismatch = 0
    n_excluded_status = 0
    for member in members:
        raw_events = events_by_conv.get(member.conversation_id, [])
        # LEAD_MISMATCH short-circuit — asking about a cleaning job is
        # not a failed cleaning sale. Preserve the evidence in the
        # database (we do not delete the events), just remove this
        # conversation from sales-effectiveness comparisons.
        if any(ev.event_type == LEAD_MISMATCH for ev in raw_events):
            n_excluded_lead_mismatch += 1
            continue
        status = member.lb_status_at_freeze
        if status in positive_set:
            outcome_class = 'positive'
        elif status in negative_set:
            outcome_class = 'negative'
        else:
            n_excluded_status += 1
            continue
        records.append(ConversationRecord(
            conversation_id=str(member.conversation_id),
            outcome_class=outcome_class,
            lb_status=status,
            turn_count=member.turn_count_at_freeze,
            events=_truncate_at_first_outcome_proxy(raw_events),
        ))

    meta = {
        'n_excluded_lead_mismatch': n_excluded_lead_mismatch,
        'n_excluded_status': n_excluded_status,
        'n_included': len(records),
    }
    return records, meta


# ---------------------------------------------------------------------------
# Response-window primitive
# ---------------------------------------------------------------------------


def find_first_response(
    events: list[Event], c_idx: int, *, max_turn_distance: int,
) -> tuple[str, str]:
    """Walk forward from the event at c_idx (a customer signal) and
    return (action_event_type, reason).

    Terminates the walk on the first of:
    - an AGENT_ACTION event (that is the response) → (action_type, 'response_found')
    - an OUTCOME_PROXY / POST_OUTCOME event → (NO_ACTION, 'reached_outcome')
    - a CUSTOMER_SIGNAL event on a LATER turn than the C → (NO_ACTION, 'next_customer_signal')
      Same-turn customer signals do NOT terminate — a single customer
      message can produce multiple simultaneous signals.
    - turn distance safety bound exceeded → (NO_ACTION, 'window_expired')
    - end of events → (NO_ACTION, 'end_of_conversation')
    """
    c = events[c_idx]
    c_turn = c.turn_start
    for j in range(c_idx + 1, len(events)):
        ev = events[j]
        # Safety bound applied before behavioral checks so pathological
        # gaps don't sneak past termination.
        if ev.turn_start - c_turn > max_turn_distance:
            return NO_ACTION, 'window_expired'
        et = ev.event_type
        temporal = event_temporal_class(et)
        if temporal in ('OUTCOME_PROXY', 'POST_OUTCOME'):
            return NO_ACTION, 'reached_outcome'
        behavioral = event_behavioral_class(et)
        if behavioral == 'CUSTOMER_SIGNAL' and ev.turn_start > c_turn:
            return NO_ACTION, 'next_customer_signal'
        if behavioral == 'AGENT_ACTION':
            return et, 'response_found'
        # CONVERSATION_STATE / same-turn CUSTOMER_SIGNAL / same-turn
        # AGENT_ACTION already handled → skip and continue walking.
    return NO_ACTION, 'end_of_conversation'


# ---------------------------------------------------------------------------
# Per-conversation observation enumeration
# ---------------------------------------------------------------------------


def enumerate_observations(
    records: list[ConversationRecord], *, max_turn_distance: int,
) -> dict[str, dict[str, str]]:
    """For each conversation, for each customer-signal type present, record
    the FIRST occurrence's response action.

    Returns:
        {conversation_id: {c_type: a_type or NO_ACTION}}

    We deliberately keep only the FIRST occurrence per (conv, C-type).
    Multiple occurrences of the same C-type in one conversation collapse
    to one observation — this prevents inflating cell counts from
    correlated same-conversation restatements.
    """
    out: dict[str, dict[str, str]] = {}
    for rec in records:
        seen_c_types: dict[str, str] = {}
        for i, ev in enumerate(rec.events):
            et = ev.event_type
            if event_behavioral_class(et) != 'CUSTOMER_SIGNAL':
                continue
            if et in seen_c_types:
                continue
            action, _reason = find_first_response(
                rec.events, i, max_turn_distance=max_turn_distance,
            )
            seen_c_types[et] = action
        if seen_c_types:
            out[rec.conversation_id] = seen_c_types
    return out


# ---------------------------------------------------------------------------
# Cell computation
# ---------------------------------------------------------------------------


@dataclass
class CellCounts:
    """Counts for a (C, A) cell and its within-condition comparators."""
    # (C, A) cell
    ca_pos_ids: set[str] = field(default_factory=set)
    ca_neg_ids: set[str] = field(default_factory=set)
    # (C, other AGENT_ACTION) — same C, any A' ≠ A ≠ NO_ACTION
    co_pos_ids: set[str] = field(default_factory=set)
    co_neg_ids: set[str] = field(default_factory=set)
    # (C, NO_ACTION) — secondary baseline
    cn_pos_ids: set[str] = field(default_factory=set)
    cn_neg_ids: set[str] = field(default_factory=set)


def compute_cells(
    records: list[ConversationRecord],
    observations: dict[str, dict[str, str]],
) -> dict[tuple[str, str], CellCounts]:
    """Aggregate per-conversation observations into (C, A) cells.

    For each C-type observed in the corpus, and each A-type observed as
    a first-response to that C, produce a CellCounts with the C+A cell
    (positive/negative conversation IDs), the C+other-A comparator, and
    the C+no-A secondary baseline.
    """
    # Index outcomes by conversation_id for fast lookup.
    outcome_by_conv = {r.conversation_id: r.outcome_class for r in records}

    # First pass: discover (C, A) pairs actually present, and index
    # per-C the full set of {conv_id: a_type} so we can compute
    # comparators.
    c_index: dict[str, dict[str, str]] = defaultdict(dict)
    for conv_id, per_c in observations.items():
        for c_type, a_type in per_c.items():
            c_index[c_type][conv_id] = a_type

    cells: dict[tuple[str, str], CellCounts] = {}
    for c_type, conv_map in c_index.items():
        # Distinct A-types seen for this C, excluding the NO_ACTION
        # sentinel (which is only a baseline, never a candidate cell).
        a_types = {a for a in conv_map.values() if a != NO_ACTION}
        for a_type in a_types:
            cc = CellCounts()
            for conv_id, observed_a in conv_map.items():
                outcome = outcome_by_conv[conv_id]
                if observed_a == a_type:
                    if outcome == 'positive':
                        cc.ca_pos_ids.add(conv_id)
                    else:
                        cc.ca_neg_ids.add(conv_id)
                elif observed_a == NO_ACTION:
                    if outcome == 'positive':
                        cc.cn_pos_ids.add(conv_id)
                    else:
                        cc.cn_neg_ids.add(conv_id)
                else:
                    # some other AGENT_ACTION
                    if outcome == 'positive':
                        cc.co_pos_ids.add(conv_id)
                    else:
                        cc.co_neg_ids.add(conv_id)
            cells[(c_type, a_type)] = cc
    return cells


# ---------------------------------------------------------------------------
# Rates + effect helpers
# ---------------------------------------------------------------------------


def _rate(pos: int, neg: int) -> float:
    total = pos + neg
    return pos / total if total > 0 else 0.0


def _rate_diff(pos_a: int, neg_a: int, pos_b: int, neg_b: int) -> float:
    return _rate(pos_a, neg_a) - _rate(pos_b, neg_b)


def _rate_diff_ci(
    pos_a: int, neg_a: int, pos_b: int, neg_b: int,
) -> tuple[float, float]:
    """95% CI on rate_a - rate_b where rate_x = pos_x / (pos_x + neg_x).

    Reuses the discovery module's _diff_ci which computes a Wald-on-diff
    interval on two independent proportions.
    """
    n_a = pos_a + neg_a
    n_b = pos_b + neg_b
    return _diff_ci(pos_a, n_a, pos_b, n_b)


# ---------------------------------------------------------------------------
# Length-stratified direction (per-cell)
# ---------------------------------------------------------------------------


def _length_stratified_direction(
    records: list[ConversationRecord],
    observations: dict[str, dict[str, str]],
    c_type: str, a_type: str,
) -> tuple[str, str]:
    """Recompute the primary within-condition direction on short vs long
    halves. Returns ('positive'/'negative'/'null'/'') per stratum.

    Empty string means insufficient sample in the stratum (< 3
    conversations in either the CA or C-otherA cell).
    """
    if not records:
        return '', ''
    turns = sorted(r.turn_count for r in records)
    med = median(turns)
    short = [r for r in records if r.turn_count < med]
    long_ = [r for r in records if r.turn_count >= med]

    def _dir(subset: list[ConversationRecord]) -> str:
        sub_ids = {r.conversation_id for r in subset}
        out_by = {r.conversation_id: r.outcome_class for r in subset}
        pos_ca = neg_ca = pos_co = neg_co = 0
        for conv_id in sub_ids:
            observed = observations.get(conv_id, {}).get(c_type)
            if observed is None or observed == NO_ACTION:
                # not eligible for the primary comparator
                continue
            outcome = out_by[conv_id]
            if observed == a_type:
                if outcome == 'positive':
                    pos_ca += 1
                else:
                    neg_ca += 1
            else:
                if outcome == 'positive':
                    pos_co += 1
                else:
                    neg_co += 1
        n_ca = pos_ca + neg_ca
        n_co = pos_co + neg_co
        if n_ca < 3 or n_co < 3:
            return ''
        d = _rate_diff(pos_ca, neg_ca, pos_co, neg_co)
        if abs(d) < 0.02:
            return 'null'
        return 'positive' if d > 0 else 'negative'

    return _dir(short), _dir(long_)


# ---------------------------------------------------------------------------
# Result dataclass + orchestration
# ---------------------------------------------------------------------------


@dataclass
class ConditionalPatternResult:
    condition_event: str
    action_event: str
    # Discovery cell counts
    d_ca_pos: int
    d_ca_neg: int
    d_co_pos: int
    d_co_neg: int
    d_cn_pos: int
    d_cn_neg: int
    # Discovery rates
    d_ca_rate: float
    d_co_rate: float
    d_cn_rate: float
    # Discovery effects
    d_primary_effect: float
    d_primary_ci_low: float
    d_primary_ci_high: float
    d_secondary_effect: float
    d_secondary_ci_low: float
    d_secondary_ci_high: float
    # Length stratification (primary direction only)
    len_short_dir: str
    len_long_dir: str
    # Holdout cell counts
    h_ca_pos: int
    h_ca_neg: int
    h_co_pos: int
    h_co_neg: int
    h_cn_pos: int
    h_cn_neg: int
    # Holdout effects
    h_primary_effect: float
    h_secondary_effect: float
    # Status
    overall_status: str        # SUPPORTED | DIRECTIONAL_ONLY | UNDERPOWERED
    holdout_status: str        # HOLDOUT_REPRODUCED | HOLDOUT_FAILED | UNDERPOWERED
    # Evidence
    evidence_positive_ids: list[str]
    evidence_negative_ids: list[str]


def _classify_overall(
    ca_total: int, co_total: int, min_cell_support: int,
) -> str:
    if ca_total < min_cell_support:
        return 'UNDERPOWERED'
    if co_total < min_cell_support:
        # We have enough C+A observations to say something about the
        # cell, but not enough C+other-A to compare against. Report the
        # rate but don't claim a supported comparison.
        return 'DIRECTIONAL_ONLY'
    return 'SUPPORTED'


def _classify_holdout(
    h_ca_pos: int, h_ca_neg: int, h_co_pos: int, h_co_neg: int,
    d_primary_effect: float,
) -> tuple[str, float]:
    """Return (status, holdout_effect). Underpowered if either cell has
    fewer than 3 conversations."""
    n_ca = h_ca_pos + h_ca_neg
    n_co = h_co_pos + h_co_neg
    if n_ca < 3 or n_co < 3:
        return 'UNDERPOWERED', 0.0
    h_eff = _rate_diff(h_ca_pos, h_ca_neg, h_co_pos, h_co_neg)
    # Both near-null counts as reproduced — the discovery finding was
    # itself weak and the holdout agrees it's weak.
    if abs(d_primary_effect) < 0.02 and abs(h_eff) < 0.02:
        return 'HOLDOUT_REPRODUCED', h_eff
    same_sign = (d_primary_effect >= 0 and h_eff >= 0) or (d_primary_effect < 0 and h_eff < 0)
    return ('HOLDOUT_REPRODUCED' if same_sign else 'HOLDOUT_FAILED'), h_eff


def analyze(
    records: list[ConversationRecord], *,
    config: ConditionalConfig, split_seed: int,
) -> tuple[list[ConditionalPatternResult], dict]:
    """End-to-end conditional analysis on a set of records. Returns
    (results, meta) with split sizes for the caller."""
    discovery, holdout = stratified_split(
        records, discovery_fraction=config.discovery_fraction, seed=split_seed,
    )
    disc_pos = [r for r in discovery if r.outcome_class == 'positive']
    disc_neg = [r for r in discovery if r.outcome_class == 'negative']
    hold_pos = [r for r in holdout if r.outcome_class == 'positive']
    hold_neg = [r for r in holdout if r.outcome_class == 'negative']

    disc_obs = enumerate_observations(
        discovery, max_turn_distance=config.max_turn_distance,
    )
    hold_obs = enumerate_observations(
        holdout, max_turn_distance=config.max_turn_distance,
    )
    disc_cells = compute_cells(discovery, disc_obs)
    hold_cells = compute_cells(holdout, hold_obs)

    results: list[ConditionalPatternResult] = []
    for (c_type, a_type), dc in disc_cells.items():
        # Discovery cell counts
        d_ca_pos, d_ca_neg = len(dc.ca_pos_ids), len(dc.ca_neg_ids)
        d_co_pos, d_co_neg = len(dc.co_pos_ids), len(dc.co_neg_ids)
        d_cn_pos, d_cn_neg = len(dc.cn_pos_ids), len(dc.cn_neg_ids)
        ca_total = d_ca_pos + d_ca_neg
        co_total = d_co_pos + d_co_neg
        cn_total = d_cn_pos + d_cn_neg

        overall = _classify_overall(ca_total, co_total, config.min_cell_support)
        # Even underpowered cells get stored — user asked for preservation
        # of low-N comparisons, just not promotion.

        d_ca_rate = _rate(d_ca_pos, d_ca_neg)
        d_co_rate = _rate(d_co_pos, d_co_neg)
        d_cn_rate = _rate(d_cn_pos, d_cn_neg)

        d_primary = _rate_diff(d_ca_pos, d_ca_neg, d_co_pos, d_co_neg)
        d_pci_lo, d_pci_hi = _rate_diff_ci(
            d_ca_pos, d_ca_neg, d_co_pos, d_co_neg,
        )
        d_secondary = _rate_diff(d_ca_pos, d_ca_neg, d_cn_pos, d_cn_neg)
        d_sci_lo, d_sci_hi = _rate_diff_ci(
            d_ca_pos, d_ca_neg, d_cn_pos, d_cn_neg,
        )

        short_dir, long_dir = _length_stratified_direction(
            discovery, disc_obs, c_type, a_type,
        )

        # Holdout — same cell if present, else zeros
        hc = hold_cells.get((c_type, a_type))
        if hc is None:
            h_ca_pos = h_ca_neg = 0
            h_co_pos = h_co_neg = 0
            h_cn_pos = h_cn_neg = 0
        else:
            h_ca_pos, h_ca_neg = len(hc.ca_pos_ids), len(hc.ca_neg_ids)
            h_co_pos, h_co_neg = len(hc.co_pos_ids), len(hc.co_neg_ids)
            h_cn_pos, h_cn_neg = len(hc.cn_pos_ids), len(hc.cn_neg_ids)
        h_status, h_primary = _classify_holdout(
            h_ca_pos, h_ca_neg, h_co_pos, h_co_neg, d_primary,
        )
        h_secondary = 0.0
        if (h_ca_pos + h_ca_neg) >= 3 and (h_cn_pos + h_cn_neg) >= 3:
            h_secondary = _rate_diff(h_ca_pos, h_ca_neg, h_cn_pos, h_cn_neg)

        results.append(ConditionalPatternResult(
            condition_event=c_type,
            action_event=a_type,
            d_ca_pos=d_ca_pos, d_ca_neg=d_ca_neg,
            d_co_pos=d_co_pos, d_co_neg=d_co_neg,
            d_cn_pos=d_cn_pos, d_cn_neg=d_cn_neg,
            d_ca_rate=round(d_ca_rate, 4),
            d_co_rate=round(d_co_rate, 4),
            d_cn_rate=round(d_cn_rate, 4),
            d_primary_effect=round(d_primary, 4),
            d_primary_ci_low=round(d_pci_lo, 4),
            d_primary_ci_high=round(d_pci_hi, 4),
            d_secondary_effect=round(d_secondary, 4),
            d_secondary_ci_low=round(d_sci_lo, 4),
            d_secondary_ci_high=round(d_sci_hi, 4),
            len_short_dir=short_dir, len_long_dir=long_dir,
            h_ca_pos=h_ca_pos, h_ca_neg=h_ca_neg,
            h_co_pos=h_co_pos, h_co_neg=h_co_neg,
            h_cn_pos=h_cn_pos, h_cn_neg=h_cn_neg,
            h_primary_effect=round(h_primary, 4),
            h_secondary_effect=round(h_secondary, 4),
            overall_status=overall,
            holdout_status=h_status,
            evidence_positive_ids=sorted(dc.ca_pos_ids)[:50],
            evidence_negative_ids=sorted(dc.ca_neg_ids)[:50],
        ))

    # Rank: SUPPORTED first, then by |primary_effect| descending.
    status_order = {'SUPPORTED': 0, 'DIRECTIONAL_ONLY': 1, 'UNDERPOWERED': 2}
    results.sort(key=lambda r: (
        status_order.get(r.overall_status, 3),
        -abs(r.d_primary_effect),
        -(r.d_ca_pos + r.d_ca_neg),
    ))

    meta = {
        'n_discovery_positive': len(disc_pos),
        'n_discovery_negative': len(disc_neg),
        'n_holdout_positive': len(hold_pos),
        'n_holdout_negative': len(hold_neg),
        'discovery_conversation_ids': [r.conversation_id for r in discovery],
        'holdout_conversation_ids': [r.conversation_id for r in holdout],
        'n_discovery_observations': sum(len(v) for v in disc_obs.values()),
        'n_holdout_observations': sum(len(v) for v in hold_obs.values()),
    }
    return results, meta
