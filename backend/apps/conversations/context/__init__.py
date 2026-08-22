"""Canonical Conversation Context Resolution Layer.

Purpose
-------
BehaviorOS analytical engines must not each independently interpret raw
source payloads to figure out "how many bedrooms did this conversation
involve." Doing so:

  * duplicates provider-parsing logic that LeadBridge already owns
    (Thumbtack `request.details[]`, Yelp `project.survey_answers[]`);
  * produces incompatible answers when two analyzers disagree about
    the raw payload's shape;
  * cannot cleanly express provenance ("did we get 3 bedrooms from
    the LB structured survey, from an LLM inference on turn 12,
    or from a mid-conversation customer correction?").

This module owns the ONE resolution: given a `Conversation`, return a
`CanonicalConversationContext` that carries semantic attributes with
per-attribute provenance. Pricing 1D is consumer #1. Conversation
Quality Manager will be consumer #2.

Ownership boundary
------------------
LB owns Thumbtack/Yelp payload parsing (via `extractLeadDetails()` +
canonical `/api/v1/learning/leads/context` endpoint). BehaviorOS
NEVER re-parses `request.details[]` / `project.survey_answers[]`.

BOS owns:
  * calling source systems for their canonical view,
  * running conversation-derived extractors (Pricing P3
    `resolved_context`, future qualification, FAQ, etc.),
  * resolving conflicts across sources with source/time/authority
    precedence,
  * preserving every raw observation with provenance,
  * caching the result so N analyzers = 1 source fetch.

Public surface
--------------
Consumers should touch ONLY the top-level orchestrator:

    from apps.conversations.context import resolve_conversation_context

    ctx = resolve_conversation_context(conversation)
    ctx.attributes['bedrooms']   # -> CanonicalAttribute | None
    ctx.observations['bedrooms']  # -> list[Observation] (all raw values)
    ctx.conflicts                 # -> dict[str, ConflictReport]
"""

from apps.conversations.context.resolver import (
    resolve_conversation_context,
)
from apps.conversations.context.types import (
    ALL_ATTRIBUTES,
    STABLE_ATTRIBUTES,
    Attr,
    Authority,
    CanonicalAttribute,
    CanonicalConversationContext,
    ConflictReport,
    Observation,
)

__all__ = [
    'ALL_ATTRIBUTES',
    'STABLE_ATTRIBUTES',
    'Attr',
    'Authority',
    'CanonicalAttribute',
    'CanonicalConversationContext',
    'ConflictReport',
    'Observation',
    'resolve_conversation_context',
]
