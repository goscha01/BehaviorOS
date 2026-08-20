"""CustomerState v1 — deterministic state-inference primitive.

Consumes normalized BehaviorOS semantic events (source-agnostic — works
identically whether events came from LB/Sigcore, future Callio, website
chat, or any other channel) and emits a state history per conversation.

Design constraints (from 1B-6 spec):
- State is NOT a 1:1 rename of a semantic event. Multiple event paths
  must be able to enter the same state, and the state model has to
  demonstrate useful aggregation over its constituent signals.
- CUSTOMER_HESITATION is QUARANTINED — the Phase-0 audit showed only
  ~13% semantic precision. It's preserved in the source events with
  `UNRELIABLE_FOR_STATE_V1` provenance but never drives state.
- AT_RISK is CONDITIONAL. It's inferred structurally when
  candidate-risk signals fire, but only reported as a validated state
  if outcome analysis actually shows loss correlation. Otherwise
  1B-6 v1 reports "no validated risk-state" rather than manufacturing
  one.
- Non-monotonic progression allowed: a HIGH_INTENT customer who later
  raises an objection can move to AT_RISK, and back to HIGH_INTENT if
  a later signal supports it.

State ordering for the "move-up" rule:
    UNKNOWN < EXPLORING < ENGAGED < HIGH_INTENT < BOOKING_INTENT
AT_RISK is a LATERAL state (any prior state can enter it; entering it
does not lose the fact that we previously were HIGH_INTENT — the
transition record preserves that).

Versioned: `INFERENCE_VERSION` bumps when rules change. Rerunning
against the same conversation set with a bumped version creates a
NEW InferredCustomerState set — v1 rows preserved untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from apps.conversations.analysis.conditional import Event


INFERENCE_VERSION = 'customer-state-v1'


# ---------------------------------------------------------------------------
# State taxonomy
# ---------------------------------------------------------------------------


STATE_UNKNOWN = 'UNKNOWN'
STATE_EXPLORING = 'EXPLORING'
STATE_ENGAGED = 'ENGAGED'
STATE_HIGH_INTENT = 'HIGH_INTENT'
STATE_BOOKING_INTENT = 'BOOKING_INTENT'
STATE_AT_RISK = 'AT_RISK'
STATE_TERMINAL = 'TERMINAL'  # reserved for post-outcome; not inferred in v1

STATES: tuple[str, ...] = (
    STATE_UNKNOWN, STATE_EXPLORING, STATE_ENGAGED,
    STATE_HIGH_INTENT, STATE_BOOKING_INTENT, STATE_AT_RISK, STATE_TERMINAL,
)

# Total order among monotonic states (used to decide "moving up").
# AT_RISK and TERMINAL are lateral / terminal, not on this axis.
_STATE_LEVEL: dict[str, int] = {
    STATE_UNKNOWN: 0,
    STATE_EXPLORING: 1,
    STATE_ENGAGED: 2,
    STATE_HIGH_INTENT: 3,
    STATE_BOOKING_INTENT: 4,
}


# ---------------------------------------------------------------------------
# Signal → state contribution (evidence buckets)
# ---------------------------------------------------------------------------


# Signals whose extractor precision was < 20% in the Phase-0 audit.
# Preserved in source events with UNRELIABLE_FOR_STATE_V1 provenance
# but never drive state. Extractor-v4 revisit is a separate task.
QUARANTINED_SIGNALS: frozenset[str] = frozenset({'CUSTOMER_HESITATION'})

# Multiple event types contribute to the same state — this is the
# whole point of the state layer.
EXPLORING_SIGNALS: frozenset[str] = frozenset({
    'SERVICE_INQUIRY',
    'QUESTION_FAQ',
    'SERVICE_DETAILS_PROVIDED',
    'CALL_REQUESTED',
})
ENGAGED_SIGNALS: frozenset[str] = frozenset({
    'PROPERTY_DETAILS_PROVIDED',
    'QUALIFICATION_ANSWER',
})
HIGH_INTENT_SIGNALS: frozenset[str] = frozenset({
    'PRICE_REQUESTED',
    'AVAILABILITY_REQUESTED',
    'DISCOUNT_REQUESTED',
})
BOOKING_INTENT_SIGNALS: frozenset[str] = frozenset({
    'BOOKING_REQUESTED',
})
# Candidate AT_RISK inputs. Entering AT_RISK is a STRUCTURAL possibility
# but the outcome validator decides whether AT_RISK is a REAL state in
# v1. If negative correlation is not observed, AT_RISK is downgraded to
# "candidate / unvalidated" in the report.
CANDIDATE_AT_RISK_SIGNALS: frozenset[str] = frozenset({
    'CUSTOMER_DEFERRED',
    'PRICE_OBJECTION',
    'TIMING_OBJECTION',
    'TRUST_OBJECTION',
    'SERVICE_OBJECTION',
    'COMPETITOR_MENTIONED',
})

# Number of AT_RISK-candidate signals that must fire before we mark
# AT_RISK. Two-signal threshold prevents a single objection from
# derailing a booking-intent customer.
AT_RISK_MIN_SIGNALS = 2


# ---------------------------------------------------------------------------
# Inference output types
# ---------------------------------------------------------------------------


@dataclass
class StateTransition:
    """One state entry in a conversation's state history."""
    ordinal: int                      # 0-based position in the history
    state: str                        # target state
    previous_state: str               # state before this transition
    trigger_event_types: list[str]    # event types that produced the move
    trigger_event_ordinals: list[int] # semantic-event ordinals (provenance)
    effective_turn: int               # DB parent turn_start of the latest trigger
    reason: str                       # human-readable
    inference_version: str = INFERENCE_VERSION


@dataclass
class ConversationStateHistory:
    conversation_id: str
    transitions: list[StateTransition] = field(default_factory=list)

    def final_state(self) -> str:
        if not self.transitions:
            return STATE_UNKNOWN
        return self.transitions[-1].state

    def states_visited(self) -> set[str]:
        return {t.state for t in self.transitions}

    def entered(self, state: str) -> bool:
        """Did the conversation enter `state` at any point?"""
        return any(t.state == state for t in self.transitions)


# ---------------------------------------------------------------------------
# Rule application
# ---------------------------------------------------------------------------


def _state_for_signal(event_type: str) -> str | None:
    """Return the state a single event type unambiguously evidences.
    Returns None for events that are not state-driving (e.g. quarantined,
    AGENT_ACTION types, OUTCOME_PROXY)."""
    if event_type in QUARANTINED_SIGNALS:
        return None
    if event_type in BOOKING_INTENT_SIGNALS:
        return STATE_BOOKING_INTENT
    if event_type in HIGH_INTENT_SIGNALS:
        return STATE_HIGH_INTENT
    if event_type in ENGAGED_SIGNALS:
        return STATE_ENGAGED
    if event_type in EXPLORING_SIGNALS:
        return STATE_EXPLORING
    return None


def _should_move_up(current: str, candidate: str) -> bool:
    """True if candidate is a strictly higher state on the monotonic
    axis than current. AT_RISK / TERMINAL / UNKNOWN candidates are
    handled separately."""
    if candidate not in _STATE_LEVEL:
        return False
    if current not in _STATE_LEVEL:
        return True
    return _STATE_LEVEL[candidate] > _STATE_LEVEL[current]


def infer_state_history(
    events: list[Event], conversation_id: str,
    *, at_risk_min_signals: int = AT_RISK_MIN_SIGNALS,
) -> ConversationStateHistory:
    """Walk semantic events chronologically, emit state transitions.

    Events assumed to be pre-outcome-truncated (as build_records does).
    Non-CUSTOMER_SIGNAL events are ignored for state transitions but
    their provenance is preserved by the extraction run.

    Non-monotonic rules:
    - Moving UP the axis (EXPLORING → ENGAGED → HIGH_INTENT →
      BOOKING_INTENT) fires whenever a higher-state signal appears.
    - AT_RISK fires laterally when >= at_risk_min_signals
      candidate-risk signals have accumulated. On entering AT_RISK
      the previous state is preserved in the transition record — so
      we can measure conversion back to HIGH_INTENT / BOOKING_INTENT
      later.
    - A HIGH_INTENT or BOOKING_INTENT signal AFTER AT_RISK moves back
      to that higher state.

    A same-state re-affirmation (e.g. two HIGH_INTENT signals in a row)
    does NOT emit a transition — history only records CHANGES.

    Provenance: each transition carries the ordinals of the semantic
    events that triggered it. Multi-event triggers (e.g. AT_RISK
    firing on the second candidate-risk signal) list all contributing
    events.
    """
    events_sorted = sorted(
        events, key=lambda e: (e.turn_start, e.ordinal),
    )
    history = ConversationStateHistory(conversation_id=conversation_id)

    current = STATE_UNKNOWN
    # AT_RISK accumulator — list of (event_type, ordinal, turn_start)
    at_risk_hits: list[tuple[str, int, int]] = []
    # Buffer for events that contributed to the LAST transition (so we
    # can attribute an evidence trail even for state-holding events).
    # For v1 we keep it simple: each transition attributes only the
    # single event that triggered it (or, for AT_RISK, the two).
    ordinal = 0

    def _emit_transition(
        new_state: str, triggers: list[Event], reason: str,
    ) -> None:
        nonlocal current, ordinal
        history.transitions.append(StateTransition(
            ordinal=ordinal,
            state=new_state,
            previous_state=current,
            trigger_event_types=[e.event_type for e in triggers],
            trigger_event_ordinals=[e.ordinal for e in triggers],
            effective_turn=triggers[-1].turn_start,
            reason=reason,
        ))
        current = new_state
        ordinal += 1

    for ev in events_sorted:
        et = ev.event_type

        # ---- AT_RISK accumulation ----
        if et in CANDIDATE_AT_RISK_SIGNALS:
            at_risk_hits.append((et, ev.ordinal, ev.turn_start))
            if len(at_risk_hits) >= at_risk_min_signals and current != STATE_AT_RISK:
                # Look up the triggering events for provenance
                trigger_ords = {e[1] for e in at_risk_hits}
                triggers = [
                    e for e in events_sorted if e.ordinal in trigger_ords
                ]
                _emit_transition(
                    STATE_AT_RISK,
                    triggers,
                    f'accumulated {len(at_risk_hits)} risk-candidate signals: '
                    f'{[t[0] for t in at_risk_hits]}',
                )
            # A candidate-risk signal doesn't ALSO push you up the axis;
            # continue to next event.
            continue

        # ---- Positive move UP the monotonic axis ----
        candidate = _state_for_signal(et)
        if candidate is None:
            continue

        if current == STATE_AT_RISK:
            # A higher-state signal AFTER AT_RISK moves us back to it.
            _emit_transition(
                candidate,
                [ev],
                f'recovery from AT_RISK on {et}',
            )
            continue

        if _should_move_up(current, candidate):
            _emit_transition(
                candidate,
                [ev],
                f'{et} evidences {candidate}',
            )

    return history


# ---------------------------------------------------------------------------
# Batch orchestration
# ---------------------------------------------------------------------------


def infer_all(
    events_by_conversation: dict[str, list[Event]],
    *, at_risk_min_signals: int = AT_RISK_MIN_SIGNALS,
) -> list[ConversationStateHistory]:
    """Infer state histories for a whole batch."""
    return [
        infer_state_history(
            evs, conv_id, at_risk_min_signals=at_risk_min_signals,
        )
        for conv_id, evs in events_by_conversation.items()
    ]
