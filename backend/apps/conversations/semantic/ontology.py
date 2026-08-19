"""Event Ontology V1 for Pipeline 1B-1 semantic extraction.

Bounded, versioned taxonomy of behaviors we expect to see in sales-oriented
conversations (Spotless Homes cleaning service via Thumbtack/Yelp/OpenPhone).

**Do not let the LLM invent new event types.** The extractor validates
every returned `event_type` against `EVENT_TYPES` and rejects unknowns
(they become extraction errors on that record, never new taxonomy).

When the eval batch surfaces missing coverage, bump ONTOLOGY_VERSION and
add new event types deliberately — never silently.
"""

from __future__ import annotations

ONTOLOGY_VERSION = 'ontology-v2'
# v1 → v2 additions (2026-08-19): CUSTOMER_DEFERRED (deferral, not decline)
# + LEAD_MISMATCH (wrong-intent lead — e.g. customer applying for a job).
# No other ontology changes; deliberate discipline to avoid ontology drift
# before sequence analysis reveals which gaps actually matter.


# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------

ACTOR_CUSTOMER = 'customer'
ACTOR_AGENT = 'agent'
ACTOR_SYSTEM = 'system'
ACTOR_MIXED = 'mixed'
ACTOR_UNKNOWN = 'unknown'

ACTORS: frozenset[str] = frozenset({
    ACTOR_CUSTOMER, ACTOR_AGENT, ACTOR_SYSTEM, ACTOR_MIXED, ACTOR_UNKNOWN,
})


# ---------------------------------------------------------------------------
# Event types — organized by category
# ---------------------------------------------------------------------------

# Customer need / intent
SERVICE_INQUIRY = 'SERVICE_INQUIRY'
SERVICE_DETAILS_PROVIDED = 'SERVICE_DETAILS_PROVIDED'
PRICE_REQUESTED = 'PRICE_REQUESTED'
AVAILABILITY_REQUESTED = 'AVAILABILITY_REQUESTED'
BOOKING_REQUESTED = 'BOOKING_REQUESTED'
CALL_REQUESTED = 'CALL_REQUESTED'
QUESTION_FAQ = 'QUESTION_FAQ'

# Qualification
QUALIFICATION_QUESTION = 'QUALIFICATION_QUESTION'
QUALIFICATION_ANSWER = 'QUALIFICATION_ANSWER'
PROPERTY_DETAILS_PROVIDED = 'PROPERTY_DETAILS_PROVIDED'
SERVICE_SCOPE_CLARIFIED = 'SERVICE_SCOPE_CLARIFIED'

# Pricing
PRICE_GIVEN = 'PRICE_GIVEN'
PRICE_RANGE_GIVEN = 'PRICE_RANGE_GIVEN'
DISCOUNT_OFFERED = 'DISCOUNT_OFFERED'
DISCOUNT_REQUESTED = 'DISCOUNT_REQUESTED'
PRICE_EXPLAINED = 'PRICE_EXPLAINED'

# Availability / booking
AVAILABILITY_GIVEN = 'AVAILABILITY_GIVEN'
TIME_SLOT_OFFERED = 'TIME_SLOT_OFFERED'
BOOKING_ATTEMPT = 'BOOKING_ATTEMPT'
BOOKING_CONFIRMED = 'BOOKING_CONFIRMED'
RESCHEDULE_REQUESTED = 'RESCHEDULE_REQUESTED'
RESCHEDULE_CONFIRMED = 'RESCHEDULE_CONFIRMED'

# Objections / friction
PRICE_OBJECTION = 'PRICE_OBJECTION'
TIMING_OBJECTION = 'TIMING_OBJECTION'
TRUST_OBJECTION = 'TRUST_OBJECTION'
SERVICE_OBJECTION = 'SERVICE_OBJECTION'
COMPETITOR_MENTIONED = 'COMPETITOR_MENTIONED'
CUSTOMER_HESITATION = 'CUSTOMER_HESITATION'
CUSTOMER_DECLINED = 'CUSTOMER_DECLINED'
# v2: distinct from CUSTOMER_DECLINED. Deferral ("get back to you next
# week", "not right now, maybe later") is NOT a decline — the sales
# process is still alive.
CUSTOMER_DEFERRED = 'CUSTOMER_DEFERRED'

# Sales behavior
FOLLOW_UP_SENT = 'FOLLOW_UP_SENT'
CUSTOMER_REENGAGED = 'CUSTOMER_REENGAGED'
CALL_ATTEMPT = 'CALL_ATTEMPT'
HUMAN_HANDOFF = 'HUMAN_HANDOFF'
URGENCY_CREATED = 'URGENCY_CREATED'
SOCIAL_PROOF_USED = 'SOCIAL_PROOF_USED'
SCOPE_VALUE_EXPLAINED = 'SCOPE_VALUE_EXPLAINED'

# Conversation progression
CUSTOMER_RESPONDED = 'CUSTOMER_RESPONDED'
CUSTOMER_STOPPED_RESPONDING = 'CUSTOMER_STOPPED_RESPONDING'
CONVERSATION_STALLED = 'CONVERSATION_STALLED'
CONVERSATION_RESUMED = 'CONVERSATION_RESUMED'

# Operational / relationship
EXISTING_CUSTOMER_REFERENCE = 'EXISTING_CUSTOMER_REFERENCE'
PREVIOUS_SERVICE_REFERENCE = 'PREVIOUS_SERVICE_REFERENCE'
COMPLAINT = 'COMPLAINT'
SATISFACTION_SIGNAL = 'SATISFACTION_SIGNAL'
# v2: customer's intent doesn't match the service being offered — e.g.
# customer wants a cleaning JOB (employment), not to hire a cleaner.
# Distinct from CUSTOMER_DECLINED (which implies they were considering
# and passed) — this is "you're not talking to the right kind of lead."
LEAD_MISMATCH = 'LEAD_MISMATCH'


EVENT_TYPES: frozenset[str] = frozenset({
    # customer need / intent
    SERVICE_INQUIRY, SERVICE_DETAILS_PROVIDED, PRICE_REQUESTED,
    AVAILABILITY_REQUESTED, BOOKING_REQUESTED, CALL_REQUESTED, QUESTION_FAQ,
    # qualification
    QUALIFICATION_QUESTION, QUALIFICATION_ANSWER,
    PROPERTY_DETAILS_PROVIDED, SERVICE_SCOPE_CLARIFIED,
    # pricing
    PRICE_GIVEN, PRICE_RANGE_GIVEN, DISCOUNT_OFFERED, DISCOUNT_REQUESTED,
    PRICE_EXPLAINED,
    # availability / booking
    AVAILABILITY_GIVEN, TIME_SLOT_OFFERED, BOOKING_ATTEMPT, BOOKING_CONFIRMED,
    RESCHEDULE_REQUESTED, RESCHEDULE_CONFIRMED,
    # objections
    PRICE_OBJECTION, TIMING_OBJECTION, TRUST_OBJECTION, SERVICE_OBJECTION,
    COMPETITOR_MENTIONED, CUSTOMER_HESITATION, CUSTOMER_DECLINED,
    CUSTOMER_DEFERRED,
    # sales behavior
    FOLLOW_UP_SENT, CUSTOMER_REENGAGED, CALL_ATTEMPT, HUMAN_HANDOFF,
    URGENCY_CREATED, SOCIAL_PROOF_USED, SCOPE_VALUE_EXPLAINED,
    # progression
    CUSTOMER_RESPONDED, CUSTOMER_STOPPED_RESPONDING,
    CONVERSATION_STALLED, CONVERSATION_RESUMED,
    # operational
    EXISTING_CUSTOMER_REFERENCE, PREVIOUS_SERVICE_REFERENCE,
    COMPLAINT, SATISFACTION_SIGNAL, LEAD_MISMATCH,
})


def is_valid_event_type(value: str) -> bool:
    return value in EVENT_TYPES


def is_valid_actor(value: str) -> bool:
    return value in ACTORS


# ---------------------------------------------------------------------------
# Temporal / analytical classification for Pipeline 1B-2
# ---------------------------------------------------------------------------
#
# Analyzing SATISFACTION_SIGNAL as a predictor of 'completed' outcome is
# tautology — the customer only left a satisfaction signal BECAUSE a
# cleaning was completed. Same for COMPLAINT, RESCHEDULE_*, etc.
#
# BOOKING_CONFIRMED is the outcome itself for booked+ statuses. Same with
# CUSTOMER_DECLINED being ≈ 'lost'. These are OUTCOME_PROXY events.
#
# Pipeline 1B-2 uses this classification to (a) truncate each conversation
# at its FIRST OUTCOME_PROXY event before extracting predictive sequences,
# and (b) exclude POST_OUTCOME events from candidate pattern generation.

PRE_OUTCOME_EVENTS: frozenset[str] = frozenset({
    # customer intent / signalling — predictive candidates
    SERVICE_INQUIRY, SERVICE_DETAILS_PROVIDED, PRICE_REQUESTED,
    AVAILABILITY_REQUESTED, BOOKING_REQUESTED, CALL_REQUESTED, QUESTION_FAQ,
    # qualification exchanges
    QUALIFICATION_QUESTION, QUALIFICATION_ANSWER,
    PROPERTY_DETAILS_PROVIDED, SERVICE_SCOPE_CLARIFIED,
    # pricing
    PRICE_GIVEN, PRICE_RANGE_GIVEN, DISCOUNT_OFFERED, DISCOUNT_REQUESTED,
    PRICE_EXPLAINED,
    # availability offered by agent
    AVAILABILITY_GIVEN, TIME_SLOT_OFFERED, BOOKING_ATTEMPT,
    # objections
    PRICE_OBJECTION, TIMING_OBJECTION, TRUST_OBJECTION, SERVICE_OBJECTION,
    COMPETITOR_MENTIONED, CUSTOMER_HESITATION, CUSTOMER_DEFERRED,
    # sales behaviors
    FOLLOW_UP_SENT, URGENCY_CREATED, SOCIAL_PROOF_USED, SCOPE_VALUE_EXPLAINED,
    CUSTOMER_RESPONDED, CONVERSATION_STALLED,
    # LEAD_MISMATCH is pre-outcome — it EXPLAINS a loss, doesn't ARE a loss.
    LEAD_MISMATCH,
})


OUTCOME_PROXY_EVENTS: frozenset[str] = frozenset({
    BOOKING_CONFIRMED,             # ≈ booking outcome itself
    CUSTOMER_DECLINED,             # ≈ lost outcome itself
    CUSTOMER_STOPPED_RESPONDING,   # near-lost for the LB status pipeline
})


POST_OUTCOME_EVENTS: frozenset[str] = frozenset({
    # only meaningful if a booking already happened
    RESCHEDULE_REQUESTED, RESCHEDULE_CONFIRMED,
    # observed after a cleaning
    SATISFACTION_SIGNAL, COMPLAINT,
    # imply prior interaction
    CUSTOMER_REENGAGED, CONVERSATION_RESUMED,
    EXISTING_CUSTOMER_REFERENCE, PREVIOUS_SERVICE_REFERENCE,
})


UNKNOWN_TIMING_EVENTS: frozenset[str] = frozenset({
    # could be initial outbound OR post-booking follow-up
    CALL_ATTEMPT,
    HUMAN_HANDOFF,
})


def event_temporal_class(event_type: str) -> str:
    """Return 'PRE_OUTCOME' | 'OUTCOME_PROXY' | 'POST_OUTCOME' | 'UNKNOWN'."""
    if event_type in PRE_OUTCOME_EVENTS:
        return 'PRE_OUTCOME'
    if event_type in OUTCOME_PROXY_EVENTS:
        return 'OUTCOME_PROXY'
    if event_type in POST_OUTCOME_EVENTS:
        return 'POST_OUTCOME'
    return 'UNKNOWN'


# Sanity: every ontology event type must be classified somewhere.
_ALL_CLASSIFIED = (
    PRE_OUTCOME_EVENTS | OUTCOME_PROXY_EVENTS
    | POST_OUTCOME_EVENTS | UNKNOWN_TIMING_EVENTS
)
assert _ALL_CLASSIFIED == EVENT_TYPES, (
    f'ontology / temporal classification drift: '
    f'unclassified = {sorted(EVENT_TYPES - _ALL_CLASSIFIED)}, '
    f'unknown types classified = {sorted(_ALL_CLASSIFIED - EVENT_TYPES)}'
)


# ---------------------------------------------------------------------------
# Behavioral / controllability classification for Pipeline 1B-3
# ---------------------------------------------------------------------------
#
# Orthogonal to the temporal classification above. Answers a different
# question: which of these events is the AGENT choosing to do, vs. which
# describes something the customer signalled, vs. which is a state?
#
# The frame: BehaviorOS ships behavior rules to LeadBridge (text) and
# Callio (voice). A rule can only fire on an agent action. So Pipeline
# 1B-3 enumerates comparisons of the form:
#
#   given customer_signal C occurred
#     → which AGENT_ACTION response A appeared to work best?
#
# CONVERSATION_STATE events (currently LEAD_MISMATCH, CONVERSATION_STALLED)
# are neither a customer intent expression nor an agent choice — they
# describe the situation. LEAD_MISMATCH specifically triggers conversation
# exclusion from sales-effectiveness comparisons (asking about a cleaning
# job is not a failed cleaning sale).
#
# One event type has exactly ONE behavioral role. If a new analysis needs
# a state like "customer stalled", derive it in the analyzer from timing
# + event absence — do NOT dual-classify existing events.

CUSTOMER_SIGNAL_EVENTS: frozenset[str] = frozenset({
    # customer need / intent
    SERVICE_INQUIRY, SERVICE_DETAILS_PROVIDED, PRICE_REQUESTED,
    AVAILABILITY_REQUESTED, BOOKING_REQUESTED, CALL_REQUESTED, QUESTION_FAQ,
    # qualification info coming FROM the customer
    QUALIFICATION_ANSWER, PROPERTY_DETAILS_PROVIDED,
    # discount ask
    DISCOUNT_REQUESTED,
    # objections + hesitation + deferral (all customer-initiated)
    PRICE_OBJECTION, TIMING_OBJECTION, TRUST_OBJECTION, SERVICE_OBJECTION,
    COMPETITOR_MENTIONED, CUSTOMER_HESITATION, CUSTOMER_DEFERRED,
    # weak signal: customer replied at all
    CUSTOMER_RESPONDED,
})


AGENT_ACTION_EVENTS: frozenset[str] = frozenset({
    # qualification led by agent
    QUALIFICATION_QUESTION, SERVICE_SCOPE_CLARIFIED,
    # pricing plays
    PRICE_GIVEN, PRICE_RANGE_GIVEN, DISCOUNT_OFFERED, PRICE_EXPLAINED,
    # availability + booking plays
    AVAILABILITY_GIVEN, TIME_SLOT_OFFERED, BOOKING_ATTEMPT,
    # outbound / cadence / persuasion
    FOLLOW_UP_SENT, CALL_ATTEMPT, HUMAN_HANDOFF, URGENCY_CREATED,
    SOCIAL_PROOF_USED, SCOPE_VALUE_EXPLAINED,
})


CONVERSATION_STATE_EVENTS: frozenset[str] = frozenset({
    # inferred conversation state, not a choice by either party
    CONVERSATION_STALLED,
    # wrong-intent lead — triggers sales-effectiveness exclusion downstream
    LEAD_MISMATCH,
})


# OUTCOME_PROXY_EVENTS and POST_OUTCOME_EVENTS above serve BOTH the
# temporal and behavioral classifications — an event's temporal role of
# OUTCOME_PROXY implies the same behavioral role, and same for
# POST_OUTCOME. We do not re-declare them here; the behavioral lookup
# below defers to those sets for those two classes.


def event_behavioral_class(event_type: str) -> str:
    """Return one of:
    'CUSTOMER_SIGNAL' | 'AGENT_ACTION' | 'CONVERSATION_STATE' |
    'OUTCOME_PROXY' | 'POST_OUTCOME'.

    Guaranteed to return one of the above for any type in EVENT_TYPES
    (see sanity assert below).
    """
    if event_type in CUSTOMER_SIGNAL_EVENTS:
        return 'CUSTOMER_SIGNAL'
    if event_type in AGENT_ACTION_EVENTS:
        return 'AGENT_ACTION'
    if event_type in CONVERSATION_STATE_EVENTS:
        return 'CONVERSATION_STATE'
    if event_type in OUTCOME_PROXY_EVENTS:
        return 'OUTCOME_PROXY'
    if event_type in POST_OUTCOME_EVENTS:
        return 'POST_OUTCOME'
    # UNKNOWN_TIMING_EVENTS all happen to be AGENT_ACTION (CALL_ATTEMPT,
    # HUMAN_HANDOFF). Enforce that in the sanity check below.
    raise ValueError(f'unclassified event_type: {event_type!r}')


# Sanity: every ontology event type must have exactly one behavioral role,
# AND UNKNOWN_TIMING events must all resolve to AGENT_ACTION (which is
# where the current members belong).
_ALL_BEHAVIORAL_CLASSIFIED = (
    CUSTOMER_SIGNAL_EVENTS | AGENT_ACTION_EVENTS | CONVERSATION_STATE_EVENTS
    | OUTCOME_PROXY_EVENTS | POST_OUTCOME_EVENTS
)
assert _ALL_BEHAVIORAL_CLASSIFIED == EVENT_TYPES, (
    f'ontology / behavioral classification drift: '
    f'unclassified = {sorted(EVENT_TYPES - _ALL_BEHAVIORAL_CLASSIFIED)}, '
    f'unknown types classified = {sorted(_ALL_BEHAVIORAL_CLASSIFIED - EVENT_TYPES)}'
)
# Behavioral classes must not overlap with each other (a type has ONE role).
_BEHAVIORAL_SETS = [
    ('CUSTOMER_SIGNAL', CUSTOMER_SIGNAL_EVENTS),
    ('AGENT_ACTION', AGENT_ACTION_EVENTS),
    ('CONVERSATION_STATE', CONVERSATION_STATE_EVENTS),
    ('OUTCOME_PROXY', OUTCOME_PROXY_EVENTS),
    ('POST_OUTCOME', POST_OUTCOME_EVENTS),
]
for _i, (_n_a, _s_a) in enumerate(_BEHAVIORAL_SETS):
    for _n_b, _s_b in _BEHAVIORAL_SETS[_i + 1:]:
        _overlap = _s_a & _s_b
        assert not _overlap, (
            f'behavioral classification overlap: {_n_a} ∩ {_n_b} = '
            f'{sorted(_overlap)}'
        )
# UNKNOWN_TIMING events all belong to AGENT_ACTION behaviorally.
assert UNKNOWN_TIMING_EVENTS <= AGENT_ACTION_EVENTS, (
    f'UNKNOWN_TIMING events must be behavioral AGENT_ACTION; '
    f'stragglers = {sorted(UNKNOWN_TIMING_EVENTS - AGENT_ACTION_EVENTS)}'
)


def event_types_by_category() -> dict[str, list[str]]:
    """For docs, prompt construction, distribution reports."""
    return {
        'customer_need_intent': [
            SERVICE_INQUIRY, SERVICE_DETAILS_PROVIDED, PRICE_REQUESTED,
            AVAILABILITY_REQUESTED, BOOKING_REQUESTED, CALL_REQUESTED,
            QUESTION_FAQ,
        ],
        'qualification': [
            QUALIFICATION_QUESTION, QUALIFICATION_ANSWER,
            PROPERTY_DETAILS_PROVIDED, SERVICE_SCOPE_CLARIFIED,
        ],
        'pricing': [
            PRICE_GIVEN, PRICE_RANGE_GIVEN, DISCOUNT_OFFERED,
            DISCOUNT_REQUESTED, PRICE_EXPLAINED,
        ],
        'availability_booking': [
            AVAILABILITY_GIVEN, TIME_SLOT_OFFERED, BOOKING_ATTEMPT,
            BOOKING_CONFIRMED, RESCHEDULE_REQUESTED, RESCHEDULE_CONFIRMED,
        ],
        'objections': [
            PRICE_OBJECTION, TIMING_OBJECTION, TRUST_OBJECTION,
            SERVICE_OBJECTION, COMPETITOR_MENTIONED,
            CUSTOMER_HESITATION, CUSTOMER_DECLINED, CUSTOMER_DEFERRED,
        ],
        'sales_behavior': [
            FOLLOW_UP_SENT, CUSTOMER_REENGAGED, CALL_ATTEMPT, HUMAN_HANDOFF,
            URGENCY_CREATED, SOCIAL_PROOF_USED, SCOPE_VALUE_EXPLAINED,
        ],
        'progression': [
            CUSTOMER_RESPONDED, CUSTOMER_STOPPED_RESPONDING,
            CONVERSATION_STALLED, CONVERSATION_RESUMED,
        ],
        'operational': [
            EXISTING_CUSTOMER_REFERENCE, PREVIOUS_SERVICE_REFERENCE,
            COMPLAINT, SATISFACTION_SIGNAL, LEAD_MISMATCH,
        ],
    }
