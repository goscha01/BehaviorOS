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
