"""Top-level canonical-context resolver.

Public entry point:

    resolve_conversation_context(
        conversation,
        *,
        conversation_observations=None,   # from LLM extractors (Pricing P3 etc.)
        lb_context_client=None,
        lb_user_id=None,
        use_cache=True,
    ) -> CanonicalConversationContext

Assembles observations from every available source, runs the
precedence engine per attribute, and returns the fully-materialized
`CanonicalConversationContext` (persisted via
`apps.conversations.context.service` when `use_cache=True`).

Sources currently wired:
  * **LeadBridge** — via `LeadBridgeContextClient` (canonical lead
    context endpoint POST /api/v1/learning/leads/context).
  * **Conversation-derived** — supplied by the caller as pre-built
    `Observation` objects. Pricing 1D's LLM extractor is the first
    producer (P3 `resolved_context` per quote). Future extractors
    (qualification, FAQ) will supply the same shape.

Planned but not wired yet:
  * ServiceFlow entity context (stub client exists in
    `apps.conversations.resolvers.serviceflow`).
  * Callio voice runtime context.
  * Manual owner-review overrides — highest authority.

Rules:
  * Missing dimensions stay missing (absent from
    `canonical.attributes`).
  * Every observation is preserved in `canonical.observations`
    (winners + losers).
  * Conflicts flagged in `canonical.conflicts`.
  * Deterministic — same inputs, same output. Required for
    reproducible pricing verdicts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Union

from apps.conversations.context.lb_client import (
    InMemoryLeadBridgeContextClient,
    LbLeadContext,
    LeadBridgeContextClient,
)
from apps.conversations.context.precedence import resolve_precedence
from apps.conversations.context.types import (
    ALL_ATTRIBUTES,
    CanonicalConversationContext,
    Observation,
)
from apps.conversations.models import (
    Conversation,
    EntityLink,
    OutcomeSnapshot,
    TargetSystem,
    TargetType,
)


logger = logging.getLogger(__name__)


LbClient = Union[LeadBridgeContextClient, InMemoryLeadBridgeContextClient]


def resolve_conversation_context(
    conversation: Conversation,
    *,
    conversation_observations: Optional[Iterable[Observation]] = None,
    lb_context_client: Optional[LbClient] = None,
    lb_user_id: Optional[str] = None,
    use_cache: bool = True,
    max_age_seconds: Optional[int] = None,
) -> CanonicalConversationContext:
    """Build a `CanonicalConversationContext` for one conversation.

    When `use_cache=True` (default), reads/writes a persisted
    `ConversationContext` row keyed by (conversation, source_versions).
    See `apps.conversations.context.service` for the cache adapter.

    `max_age_seconds` controls the "skip-LB fast path" TTL. Pass 0 to
    always call LB (needed for reconstruction runs that must reflect
    current LB state). Omit for the service default (15 min).

    `conversation_observations` — pre-built `Observation` list produced
    by an LLM extractor for this conversation (e.g. Pricing P3
    `resolved_context`). Never derived here to keep this module
    unaware of any particular domain extractor's internals.

    `lb_context_client` and `lb_user_id` are optional; when omitted,
    only the caller-provided conversation observations feed the
    resolver. Safe degradation: pricing keeps running, just against
    the same context it had before the resolver existed.
    """
    conv_obs_list = list(conversation_observations or [])
    if use_cache:
        from apps.conversations.context.service import (
            DEFAULT_MAX_AGE_SECONDS, get_or_build_context,
        )
        return get_or_build_context(
            conversation,
            conversation_observations=conv_obs_list,
            lb_context_client=lb_context_client,
            lb_user_id=lb_user_id,
            max_age_seconds=(
                DEFAULT_MAX_AGE_SECONDS
                if max_age_seconds is None else max_age_seconds
            ),
        )

    return build_context_uncached(
        conversation,
        conversation_observations=conv_obs_list,
        lb_context_client=lb_context_client,
        lb_user_id=lb_user_id,
    )


def build_context_uncached(
    conversation: Conversation,
    *,
    conversation_observations: Iterable[Observation],
    lb_context_client: Optional[LbClient] = None,
    lb_user_id: Optional[str] = None,
) -> CanonicalConversationContext:
    """Actually run the resolver. Split out so the persistence layer
    can call it fresh when the cache is stale.

    Order of operations:
      1. Collect LB lead observations (via HTTP client — one call
         per unique lead_id linked to the conversation).
      2. Include caller-supplied conversation observations
         (Pricing P3 resolved_context, etc.).
      3. Note OutcomeSnapshot presence for source_versions
         (identity anchor — no attribute observations emitted here;
         source_payload['lb_lead'] is thin and superseded by the
         LB context endpoint).
      4. Per attribute: run the precedence engine.
      5. Assemble the CanonicalConversationContext.
    """
    observations_by_attr: dict[str, list[Observation]] = {
        a: [] for a in ALL_ATTRIBUTES
    }
    source_versions: dict[str, dict[str, Any]] = {}

    # ---- LB lead context ---------------------------------------------------
    lb_lead_ids = _linked_lb_lead_ids(conversation)
    lb_contexts: list[LbLeadContext] = []
    if lb_context_client is not None and lb_lead_ids:
        try:
            lb_contexts = lb_context_client.fetch(lb_lead_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                'lb-context client raised (%s); continuing with '
                'conversation-only evidence', exc,
            )
            lb_contexts = []

    for lb_ctx in lb_contexts:
        source_versions[f'lb_lead:{lb_ctx.lead_id}'] = {
            'lead_id': lb_ctx.lead_id,
            'platform': lb_ctx.platform,
            'updated_at': lb_ctx.updated_at.isoformat(),
            'mapping_version': lb_ctx.mapping_version,
        }
        for obs in lb_ctx.observations_for(lb_user_id or 'unknown'):
            if obs.attribute in observations_by_attr:
                observations_by_attr[obs.attribute].append(obs)

    # ---- Caller-supplied conversation observations -------------------------
    conv_obs_list = list(conversation_observations)
    for obs in conv_obs_list:
        if obs.attribute in observations_by_attr:
            observations_by_attr[obs.attribute].append(obs)
    if conv_obs_list:
        source_versions['conversation_observations'] = {
            'conversation_id': str(conversation.id),
            'count': len(conv_obs_list),
        }

    # ---- OutcomeSnapshot identity anchor -----------------------------------
    outcome_snap = _latest_outcome_snapshot(conversation)
    if outcome_snap is not None:
        source_versions['outcome_snapshot'] = {
            'captured_at': outcome_snap.captured_at.isoformat(),
        }
        # No attribute observations emitted from OutcomeSnapshot —
        # the thin source_payload['lb_lead'] shape (identity fields
        # only) is superseded by the LB context endpoint. Retiring
        # the lead_metadata.py re-parsing (Phase 5) closes the loop.

    # ---- Precedence resolution ---------------------------------------------
    now = datetime.now(timezone.utc)
    ctx = CanonicalConversationContext(
        conversation_id=str(conversation.id),
        resolved_at=now,
        source_versions=source_versions,
    )

    for attr in ALL_ATTRIBUTES:
        obs_list = observations_by_attr.get(attr, [])
        if not obs_list:
            ctx.coverage[attr] = {'known': False, 'source': None, 'authority': None}
            continue
        canonical, conflict, sorted_obs = resolve_precedence(attr, obs_list)
        if canonical is None:
            ctx.coverage[attr] = {'known': False, 'source': None, 'authority': None}
            continue
        ctx.attributes[attr] = canonical
        ctx.observations[attr] = sorted_obs
        winner = sorted_obs[canonical.winning_observation_index]
        ctx.coverage[attr] = {
            'known': True,
            'source': winner.source,
            'authority': winner.authority.value,
            'source_field': winner.source_field,
        }
        if conflict is not None:
            ctx.conflicts[attr] = conflict

    return ctx


# --------- helpers ------------------------------------------------------


def _linked_lb_lead_ids(conversation: Conversation) -> list[str]:
    """Deduped list of LB lead ids linked to this conversation."""
    ids: list[str] = []
    seen: set[str] = set()
    for link in EntityLink.objects.filter(
        conversation=conversation,
        target_system=TargetSystem.LEADBRIDGE,
        target_type=TargetType.LEAD,
    ):
        if link.target_id and link.target_id not in seen:
            ids.append(link.target_id)
            seen.add(link.target_id)
    return ids


def _latest_outcome_snapshot(conversation: Conversation) -> Optional[OutcomeSnapshot]:
    return (
        OutcomeSnapshot.objects
        .filter(conversation=conversation)
        .order_by('-captured_at')
        .first()
    )
