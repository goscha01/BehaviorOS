"""Persistence + cache layer for canonical conversation context.

Owns the `ConversationContext` row and the cache-invalidation
fingerprint so that:

    N analyzers × 1 conversation → 1 LB fetch

For a given conversation the cache is valid iff every entry in
`source_versions` matches the one persisted. If any input version
changed (LB lead updated_at, Pricing extractor version, conversation
observation count) the row is rebuilt.

Callers should always go through `resolve_conversation_context()` —
this module is called transitively.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from django.db import transaction

from apps.conversations.context.resolver import build_context_uncached
from apps.conversations.context.types import (
    CanonicalConversationContext,
    Observation,
)
from apps.conversations.models import Conversation, ConversationContext


logger = logging.getLogger(__name__)


def get_or_build_context(
    conversation: Conversation,
    *,
    conversation_observations: Iterable[Observation],
    lb_context_client=None,
    lb_user_id: Optional[str] = None,
) -> CanonicalConversationContext:
    """Fetch cached context if source_versions still match, else rebuild.

    Cache hit criteria:
      * A ConversationContext row exists for this conversation, AND
      * source_versions we'd compute now match what's persisted.

    On rebuild, the ROW IS REPLACED (delete-then-insert inside a
    transaction) so a caller reading `conversation.canonical_context`
    right after gets the fresh one. We don't try to patch attribute
    subsets — the resolver is fast and correctness beats micro-perf.
    """
    conv_obs = list(conversation_observations)

    fresh = build_context_uncached(
        conversation,
        conversation_observations=conv_obs,
        lb_context_client=lb_context_client,
        lb_user_id=lb_user_id,
    )

    with transaction.atomic():
        # Compare against persisted fingerprint. If unchanged, reuse
        # the persisted attributes/observations exactly (they may
        # differ from `fresh` only in `resolved_at` clock skew).
        try:
            existing = ConversationContext.objects.select_for_update().get(
                conversation=conversation,
            )
        except ConversationContext.DoesNotExist:
            existing = None

        if existing is not None and existing.source_versions_json == fresh.source_versions:
            # Cache hit — reconstruct the dataclass from the persisted row
            # so callers get identical observations/conflicts across runs
            # (deterministic reproducibility invariant).
            return _from_row(existing)

        # Cache miss or first build. Persist fresh.
        if existing is not None:
            existing.delete()
        row = ConversationContext.objects.create(
            conversation=conversation,
            resolved_at=fresh.resolved_at,
            source_versions_json=fresh.source_versions,
            attributes_json={
                k: v.to_json() for k, v in fresh.attributes.items()
            },
            observations_json={
                k: [o.to_json() for o in lst]
                for k, lst in fresh.observations.items()
            },
            conflicts_json={
                k: v.to_json() for k, v in fresh.conflicts.items()
            },
            coverage_json=fresh.coverage,
        )
        return _from_row(row)


def force_rebuild(
    conversation: Conversation,
    *,
    conversation_observations: Iterable[Observation],
    lb_context_client=None,
    lb_user_id: Optional[str] = None,
) -> CanonicalConversationContext:
    """Rebuild the row regardless of source_versions. Callers use this
    after an owner-review override or a manual data correction, where
    the caller knows the cached view is stale even though the source
    fingerprint hasn't obviously moved.
    """
    with transaction.atomic():
        ConversationContext.objects.filter(conversation=conversation).delete()
        return get_or_build_context(
            conversation,
            conversation_observations=list(conversation_observations),
            lb_context_client=lb_context_client,
            lb_user_id=lb_user_id,
        )


def _from_row(row: ConversationContext) -> CanonicalConversationContext:
    """Reconstruct the dataclass from a persisted row."""
    return CanonicalConversationContext.from_json({
        'conversation_id': str(row.conversation_id),
        'resolved_at': row.resolved_at.isoformat(),
        'attributes': row.attributes_json or {},
        'observations': row.observations_json or {},
        'conflicts': row.conflicts_json or {},
        'source_versions': row.source_versions_json or {},
        'coverage': row.coverage_json or {},
    })
