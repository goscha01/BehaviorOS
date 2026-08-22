"""Source/time/authority-aware precedence engine.

Given a bag of `Observation`s for a single canonical attribute,
returns:
  * the winning `CanonicalAttribute`,
  * an optional `ConflictReport` (present only when at least one
    losing observation actually disagreed with the winner).

Design notes
------------
Blanket "LeadBridge > conversation" would be wrong. A later explicit
customer correction ("actually we have 4 bedrooms not 3") must be
able to supersede an older LB survey answer for the same lead. The
precedence rule below is source/time/authority-aware:

  1. Highest authority wins outright (see `_AUTHORITY_RANK` in
     `types.py`). MANUAL > SOURCE_STRUCTURED > CONVERSATION_EXPLICIT
     > SOURCE_DERIVED > CONVERSATION_LLM.

  2. When two observations have the same authority, the more
     recent `observed_at` wins.

  3. For STABLE attributes (bedrooms/bathrooms/sqft), CONVERSATION_LLM
     never beats SOURCE_STRUCTURED regardless of freshness — LLM
     extraction is too noisy to override a structured survey answer
     for something the source system asks about directly. But
     CONVERSATION_EXPLICIT (a direct customer/agent statement, e.g.
     "actually we have 4 bedrooms") CAN beat SOURCE_STRUCTURED when
     it comes after the source observation, because a corrected
     statement is authoritative for the CURRENT state.

  4. Conflict severity is derived from the closeness of the loser:
       - `informational` — loser is older AND weaker;
       - `warning` — loser has similar authority and freshness;
       - `escalate` — loser has identical authority AND freshness.

The engine is DETERMINISTIC: given the same observation set, it
always returns the same winner. That's required so a re-run against
a frozen corpus reproduces the same pricing verdicts.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from apps.conversations.context.types import (
    STABLE_ATTRIBUTES,
    Authority,
    CanonicalAttribute,
    ConflictReport,
    Observation,
    authority_rank,
)


logger = logging.getLogger(__name__)


# When two authorities differ by ≤ this many rank points and one
# observation is fresh (< STABILITY_WINDOW_SEC newer than the other),
# the fresher observation wins by freshness rather than authority.
# Prevents a stale MANUAL edit from overriding a same-day survey
# refresh, while still letting a customer correction override a
# months-old survey answer.
_CLOSE_AUTHORITY_DELTA = 10
_FRESHNESS_TIEBREAK_SEC = 60 * 60 * 24 * 7  # 7 days


def _values_equal(a, b) -> bool:
    """Loose equality tailored to attribute value shapes.

    - Numeric equality is exact (bedrooms/bathrooms are ints, sqft is
      an int; we don't want two survey answers of "1500" and "1500"
      considered different because one is str and one is int).
    - String equality is trimmed + case-insensitive for enum-like
      attributes.
    - List equality treats order-insensitive equal sets (addons).
    - Everything else falls back to `==`.
    """
    if a is None or b is None:
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    if isinstance(a, list) and isinstance(b, list):
        try:
            return sorted(str(x).strip().lower() for x in a) == sorted(
                str(x).strip().lower() for x in b
            )
        except TypeError:
            return a == b
    return a == b


def _obs_key(o: Observation) -> tuple:
    """Deterministic sort key: (authority_rank, observed_at, source, source_field)
    with higher authority coming FIRST (negated) and more recent coming FIRST."""
    return (
        -authority_rank(o.authority),
        -o.observed_at.timestamp(),
        o.source,
        o.source_field,
    )


def _is_stable_override_blocked(
    attribute: str,
    winner: Observation,
    challenger: Observation,
) -> bool:
    """True iff we should refuse to let `challenger` override `winner`
    because the attribute is STABLE and the challenger is LLM-noise."""
    if attribute not in STABLE_ATTRIBUTES:
        return False
    if winner.authority != Authority.SOURCE_STRUCTURED:
        return False
    if challenger.authority != Authority.CONVERSATION_LLM:
        return False
    return True


def _pick_winner(
    attribute: str,
    observations: list[Observation],
) -> Observation:
    """Return the precedence-winning observation.

    Algorithm:
      1. Sort by (authority desc, observed_at desc). This produces a
         provisional winner.
      2. Walk through subsequent observations. If any one of them:
         - has SIMILAR authority (delta ≤ CLOSE_DELTA),
         - AND is fresher by ≥ FRESHNESS_TIEBREAK_SEC,
         - AND is NOT blocked by the stable-attribute rule,
         then it steals the win. This is how "later explicit
         customer correction beats older LB survey" gets enforced
         without letting LLM noise do the same.
    """
    ordered = sorted(observations, key=_obs_key)
    winner = ordered[0]
    for challenger in ordered[1:]:
        if _values_equal(challenger.value, winner.value):
            continue
        if _is_stable_override_blocked(attribute, winner, challenger):
            continue
        rank_delta = (
            authority_rank(winner.authority) - authority_rank(challenger.authority)
        )
        # Fresh customer correction beats older source-structured.
        # rank_delta > 0 means winner has higher authority than challenger.
        # We only allow freshness upset when authorities are close AND
        # challenger is strictly newer by the tiebreak window.
        if 0 < rank_delta <= _CLOSE_AUTHORITY_DELTA:
            time_delta = (
                challenger.observed_at - winner.observed_at
            ).total_seconds()
            if time_delta >= _FRESHNESS_TIEBREAK_SEC:
                # If challenger is a CONVERSATION_EXPLICIT correction of
                # a stable attribute, it's exactly the customer-correction
                # case we want to honor.
                winner = challenger
                continue
    return winner


def _severity_for_conflict(
    winner: Observation,
    loser: Observation,
    time_gap_days: float,
) -> str:
    same_authority = winner.authority == loser.authority
    close_authority = (
        abs(
            authority_rank(winner.authority) - authority_rank(loser.authority)
        ) <= _CLOSE_AUTHORITY_DELTA
    )
    close_time = time_gap_days <= 7
    if same_authority and close_time:
        return 'escalate'
    if (same_authority or close_authority) and close_time:
        return 'warning'
    return 'informational'


def _explain(
    attribute: str,
    winner: Observation,
    losers: list[Observation],
) -> str:
    """Human-readable one-line reason for the resolver decision."""
    if not losers:
        return (
            f'only source: {winner.source}({winner.source_field}) '
            f'authority={winner.authority.value}'
        )
    parts = [
        f'chose {winner.source}({winner.source_field}) '
        f'authority={winner.authority.value} '
        f'observed={winner.observed_at.isoformat()}',
    ]
    for lo in losers[:3]:
        parts.append(
            f'over {lo.source}({lo.source_field}) '
            f'authority={lo.authority.value} '
            f'observed={lo.observed_at.isoformat()}'
        )
    if len(losers) > 3:
        parts.append(f'... and {len(losers) - 3} more')
    return '; '.join(parts)


def resolve_precedence(
    attribute: str,
    observations: Iterable[Observation],
) -> tuple[Optional[CanonicalAttribute], Optional[ConflictReport], list[Observation]]:
    """Given raw observations for an attribute, return
    (canonical winner, optional conflict report, deterministic
    observations list preserving all inputs sorted for reproducibility).

    Returns (None, None, []) when the observation list is empty.
    Never raises.
    """
    obs_list = list(observations)
    if not obs_list:
        return None, None, []

    ordered = sorted(obs_list, key=_obs_key)
    winner = _pick_winner(attribute, ordered)
    winning_index = ordered.index(winner)
    canonical = CanonicalAttribute(
        attribute=attribute,
        value=winner.value,
        winning_observation_index=winning_index,
        reason=_explain(attribute, winner, [o for o in ordered if o is not winner]),
    )

    # Conflict = any observation whose value disagrees with the winner.
    disagreeing = [
        o for o in ordered
        if o is not winner and not _values_equal(o.value, winner.value)
    ]
    if not disagreeing:
        return canonical, None, ordered

    # Severity based on the strongest / freshest disagreer.
    disagreeing_sorted = sorted(disagreeing, key=_obs_key)
    top_loser = disagreeing_sorted[0]
    time_gap_days = abs(
        (winner.observed_at - top_loser.observed_at).total_seconds()
    ) / 86400.0
    severity = _severity_for_conflict(winner, top_loser, time_gap_days)

    conflict = ConflictReport(
        attribute=attribute,
        winning_value=winner.value,
        losing_values=[o.value for o in disagreeing],
        severity=severity,
        explanation=(
            f'{len(disagreeing)} disagreeing observation(s); '
            f'top loser: {top_loser.source}({top_loser.source_field}) '
            f'authority={top_loser.authority.value} '
            f'observed={top_loser.observed_at.isoformat()}'
        ),
    )
    return canonical, conflict, ordered
