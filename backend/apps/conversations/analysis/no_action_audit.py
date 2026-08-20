"""Pipeline 1B-4A: classify every NO_ACTION observation from a 1B-3
conditional analysis run.

`NO_ACTION` in the 1B-3 analyzer means: no qualifying AGENT_ACTION
event was found inside the response window after a customer signal C.
That's a coarse bucket — the actual cause can be very different:

    TRUE_NO_RESPONSE                     conversation has no agent turn at all
    EXTRACTION_MISS                      agent DID reply inside window but the
                                          extractor produced no AGENT_ACTION event
    SYSTEM_AUTOMATION_RESPONSE           only a system-authored turn inside window
    OUTCOME_PROXY_TRUNCATED_WINDOW       an outcome event terminated the window
                                          before the agent had a chance
    CUSTOMER_IMMEDIATELY_SENT_NEXT_SIGNAL  customer sent another C-signal on the
                                          next turn; window closed before response
    AGENT_REPLIED_OUTSIDE_RESPONSE_WINDOW agent replied but only past the window
    CONVERSATION_ENDED_BEFORE_REPLY      window expired and conversation ended
                                          without ever seeing an agent turn after C
    OTHER                                fallback

These cause 40–47% NO_ACTION rates in the 1B-3 output for major
conditions (`PROPERTY_DETAILS_PROVIDED`, `AVAILABILITY_REQUESTED`).
1B-4A splits that bucket so we can distinguish artifact from silence.

Not persisted — derived analytical state. Output is a per-condition
per-reason breakdown with outcome rates.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from apps.conversations.analysis.conditional import (
    Event, NO_ACTION, find_first_response,
)
from apps.conversations.semantic.ontology import (
    CUSTOMER_SIGNAL_EVENTS, OUTCOME_PROXY_EVENTS, POST_OUTCOME_EVENTS,
    event_behavioral_class, event_temporal_class,
)


AUDITOR_VERSION = 'no-action-auditor-v1'


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class NoActionEntry:
    conversation_id: str
    condition_event: str
    outcome_class: str            # 'positive' | 'negative'
    reason_raw: str               # from find_first_response
    reason_fine: str              # one of the categories above
    window_end_turn: int          # inclusive; where the window closed
    c_turn_start: int


@dataclass
class NoActionAuditResult:
    entries: list[NoActionEntry] = field(default_factory=list)

    def by_condition_and_reason(self) -> dict[tuple[str, str], list[NoActionEntry]]:
        out: dict[tuple[str, str], list[NoActionEntry]] = defaultdict(list)
        for e in self.entries:
            out[(e.condition_event, e.reason_fine)].append(e)
        return out


# ---------------------------------------------------------------------------
# Turn helper
# ---------------------------------------------------------------------------


@dataclass
class RawTurn:
    """Lightweight view of ConversationTurn we need for classification."""
    ordinal: int         # position in occurred_at order; matches semantic-event turn_start
    speaker: str         # 'customer' | 'agent' | 'system' | 'unknown'


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def _find_window_end(
    reason_raw: str,
    c_event: Event,
    all_events: list[Event],
    all_turns_count: int,
    max_turn_distance: int,
) -> int:
    """Return the inclusive turn index at which the response window
    closed. Semantics mirror find_first_response's termination logic
    but return the boundary rather than the action."""
    c_turn = c_event.turn_start
    if reason_raw == 'reached_outcome':
        # First OUTCOME_PROXY or POST_OUTCOME event on a turn > c_turn
        for ev in all_events:
            if ev.turn_start <= c_turn:
                continue
            if event_temporal_class(ev.event_type) in ('OUTCOME_PROXY', 'POST_OUTCOME'):
                return ev.turn_start
        return all_turns_count - 1
    if reason_raw == 'next_customer_signal':
        for ev in all_events:
            if ev.turn_start <= c_turn:
                continue
            if event_behavioral_class(ev.event_type) == 'CUSTOMER_SIGNAL':
                return ev.turn_start
        return all_turns_count - 1
    if reason_raw == 'window_expired':
        return c_turn + max_turn_distance
    # end_of_conversation
    return max(all_turns_count - 1, c_turn)


def classify_no_action(
    *,
    reason_raw: str,
    c_event: Event,
    all_events: list[Event],
    all_turns: list[RawTurn],
    max_turn_distance: int,
) -> tuple[str, int]:
    """Return (reason_fine, window_end_turn) for one NO_ACTION observation.

    The classifier operates on the untruncated event + turn lists so it
    can distinguish "agent replied but outside the response window" from
    "no agent turn ever."

    Note on OUTCOME_PROXY: the 1B-3 analyzer runs against a truncated
    event list (OUTCOME_PROXY events removed), so its response-window
    walk never actually returns `reached_outcome` in the conditional
    pipeline — a nearby BOOKING_CONFIRMED / CUSTOMER_DECLINED /
    CUSTOMER_STOPPED_RESPONDING event surfaces as `end_of_conversation`
    to the walk. The auditor cross-checks the untruncated events to
    reclassify those cases as OUTCOME_PROXY_TRUNCATED_WINDOW.
    """
    n_turns = len(all_turns)
    c_turn = c_event.turn_start
    # If the untruncated events include an OUTCOME_PROXY on a turn > C
    # AND that outcome comes before any agent-authored turn, this
    # NO_ACTION is a truncation artifact, not silence.
    outcome_after_c = next(
        (e for e in all_events
         if e.turn_start > c_turn
         and event_temporal_class(e.event_type) in ('OUTCOME_PROXY', 'POST_OUTCOME')),
        None,
    )
    if outcome_after_c is not None and reason_raw == 'end_of_conversation':
        # See if there's an agent turn strictly between C and outcome.
        # If yes, it's an EXTRACTION_MISS scenario, fall through below.
        # If no, this is the outcome-truncation case.
        agent_between = any(
            t.speaker == 'agent'
            and c_turn < t.ordinal <= outcome_after_c.turn_start
            for t in all_turns
        )
        if not agent_between:
            return 'OUTCOME_PROXY_TRUNCATED_WINDOW', outcome_after_c.turn_start
        # else: fall through to the standard path — the inside-agent
        # branch below will pick it up as EXTRACTION_MISS.
    window_end = _find_window_end(
        reason_raw, c_event, all_events, n_turns, max_turn_distance,
    )

    # Global: was there any agent turn ANYWHERE in the conversation?
    if not any(t.speaker == 'agent' for t in all_turns):
        return 'TRUE_NO_RESPONSE', window_end

    # Turns strictly inside the response window (c_turn, window_end]
    # We deliberately include window_end so a turn that co-occurs with
    # the terminator counts as inside.
    inside_turns = [
        t for t in all_turns
        if c_turn < t.ordinal <= window_end
    ]
    inside_agent = [t for t in inside_turns if t.speaker == 'agent']
    inside_system = [t for t in inside_turns if t.speaker == 'system']

    # If an agent turn appears inside the window but no AGENT_ACTION
    # event was extracted → the extractor missed it.
    if inside_agent:
        return 'EXTRACTION_MISS', window_end

    # Only system turns inside the window → automation, not agent behavior
    if inside_system and not inside_agent:
        return 'SYSTEM_AUTOMATION_RESPONSE', window_end

    # No agent turn inside window. Look outside.
    outside_agent = [
        t for t in all_turns
        if t.ordinal > window_end and t.speaker == 'agent'
    ]

    if reason_raw == 'reached_outcome':
        return 'OUTCOME_PROXY_TRUNCATED_WINDOW', window_end
    if reason_raw == 'next_customer_signal':
        return 'CUSTOMER_IMMEDIATELY_SENT_NEXT_SIGNAL', window_end
    if reason_raw == 'window_expired':
        if outside_agent:
            return 'AGENT_REPLIED_OUTSIDE_RESPONSE_WINDOW', window_end
        return 'CONVERSATION_ENDED_BEFORE_REPLY', window_end
    if reason_raw == 'end_of_conversation':
        # end_of_conversation with agent turn later than window_end is
        # unusual (window extends to end-of-conv) but possible if
        # extractor emitted events past the last turn. Bucket separately.
        if outside_agent:
            return 'AGENT_REPLIED_OUTSIDE_RESPONSE_WINDOW', window_end
        return 'CONVERSATION_ENDED_BEFORE_REPLY', window_end
    return 'OTHER', window_end


# ---------------------------------------------------------------------------
# Audit orchestration
# ---------------------------------------------------------------------------


def audit(
    *,
    conversation_events: dict[str, list[Event]],   # UNTRUNCATED events per conv
    conversation_turns: dict[str, list[RawTurn]],  # sorted by ordinal
    conversation_outcomes: dict[str, str],         # 'positive' | 'negative'
    conditions: Iterable[str],
    max_turn_distance: int = 20,
) -> NoActionAuditResult:
    """For each conversation, for each first occurrence of a condition
    in `conditions`, if the response is NO_ACTION, classify.

    `conversation_events` should include ALL semantic events for the
    conversation (including OUTCOME_PROXY / POST_OUTCOME events) — the
    auditor needs them to reconstruct window boundaries and to detect
    'agent replied outside window'.
    """
    cond_set = set(conditions)
    result = NoActionAuditResult()

    for conv_id, all_events in conversation_events.items():
        if conv_id not in conversation_outcomes:
            continue
        outcome = conversation_outcomes[conv_id]
        turns = conversation_turns.get(conv_id, [])
        # Deterministic ordering: ordinal ascending.
        events_sorted = sorted(all_events, key=lambda e: (e.turn_start, e.ordinal))
        seen_c_types: set[str] = set()
        for i, ev in enumerate(events_sorted):
            if ev.event_type not in cond_set:
                continue
            if ev.event_type not in CUSTOMER_SIGNAL_EVENTS:
                continue
            if ev.event_type in seen_c_types:
                continue
            seen_c_types.add(ev.event_type)
            # Reuse the exact 1B-3 response-window walk. But the walk
            # expects a truncated event list (pre-outcome-proxy). Feed
            # it the same view build_records would produce: truncate at
            # first OUTCOME_PROXY.
            truncated = _truncate(events_sorted)
            # Find C's index in the truncated list
            c_idx = None
            for j, tev in enumerate(truncated):
                if (tev.turn_start == ev.turn_start
                        and tev.ordinal == ev.ordinal
                        and tev.event_type == ev.event_type):
                    c_idx = j
                    break
            if c_idx is None:
                # C was itself an OUTCOME_PROXY-adjacent event; skip.
                continue
            action, reason_raw = find_first_response(
                truncated, c_idx, max_turn_distance=max_turn_distance,
            )
            if action != NO_ACTION:
                continue
            reason_fine, window_end = classify_no_action(
                reason_raw=reason_raw,
                c_event=ev,
                all_events=events_sorted,
                all_turns=turns,
                max_turn_distance=max_turn_distance,
            )
            result.entries.append(NoActionEntry(
                conversation_id=conv_id,
                condition_event=ev.event_type,
                outcome_class=outcome,
                reason_raw=reason_raw,
                reason_fine=reason_fine,
                window_end_turn=window_end,
                c_turn_start=ev.turn_start,
            ))
    return result


def _truncate(events_sorted: list[Event]) -> list[Event]:
    """Same semantics as build_records' _truncate_at_first_outcome_proxy —
    drop everything from the first OUTCOME_PROXY event onward, and
    silently drop POST_OUTCOME events that appear before it."""
    out: list[Event] = []
    for ev in events_sorted:
        if ev.event_type in OUTCOME_PROXY_EVENTS:
            break
        if ev.event_type in POST_OUTCOME_EVENTS:
            continue
        out.append(ev)
    return out
