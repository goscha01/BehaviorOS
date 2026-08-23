"""Persistence + cache layer for canonical conversation context.

Owns the `ConversationContext` row + a bounded TTL so:

    N analyzers × 1 conversation → 1 LB fetch (bounded by TTL)

Cache-hit contract
------------------
For a given conversation the persisted row is served WITHOUT hitting
LB when BOTH:
  * A ConversationContext row exists for this conversation, AND
  * The row's `resolved_at` is within `max_age_seconds` of now.

If either condition fails, we fall through to `build_context_uncached`
which DOES call LB, then compares `source_versions` with the
persisted row's fingerprint. Matching fingerprint → keep the old
row (rebump `resolved_at`); mismatch → replace it.

Freshness contract
------------------
Detecting a stale row without LB is impossible without either
webhooks or If-Modified-Since support on LB's side (neither
exists today). The TTL cap trades off freshness for cost:

  * Default `max_age_seconds = 15 * 60` (15 min) — a lead updated
    now becomes visible within 15 min without any explicit
    invalidation.
  * Callers that require freshness (reconstruction runs,
    owner-review actions) pass `max_age_seconds=0` to force a
    rebuild.
  * Callers running against a frozen corpus (backfills, reruns)
    can pass a larger TTL to amortize the LB call across every
    analyzer.

Persisted rows are ALSO invalidated deterministically by
`source_versions` mismatch — the TTL only decides whether we bother
to CHECK. Once we fetch LB and see a new mapping_version / lead
updated_at, the row is replaced immediately regardless of TTL.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from django.db import transaction

from apps.conversations.context.resolver import build_context_uncached
from apps.conversations.context.types import (
    CanonicalConversationContext,
    Observation,
)
from apps.conversations.models import Conversation, ConversationContext


logger = logging.getLogger(__name__)


DEFAULT_MAX_AGE_SECONDS = 15 * 60  # 15 min TTL for "trust cache, skip LB"


def get_or_build_context(
    conversation: Conversation,
    *,
    conversation_observations: Iterable[Observation],
    lb_context_client=None,
    lb_user_id: Optional[str] = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> CanonicalConversationContext:
    """Fetch cached context if fresh + fingerprint matches, else rebuild.

    Skip-LB fast path (TTL-bounded):
      * Row exists AND resolved_at is within `max_age_seconds` of now
        → return persisted row without calling LB.

    Slow path (fingerprint check):
      * Row missing OR older than TTL → run `build_context_uncached`
        (fetches LB), compare `source_versions` to persisted:
          - Matches → refresh `resolved_at` on existing row, return.
          - Differs → replace row.

    Pass `max_age_seconds=0` to force a rebuild regardless of freshness
    (used by reconstruction runs that must reflect the current LB state).
    """
    conv_obs = list(conversation_observations)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=max_age_seconds)

    # Skip-LB fast path — only if TTL > 0 and row exists+fresh.
    if max_age_seconds > 0:
        try:
            fresh_enough = ConversationContext.objects.get(
                conversation=conversation,
            )
        except ConversationContext.DoesNotExist:
            fresh_enough = None
        if (
            fresh_enough is not None
            and fresh_enough.resolved_at >= cutoff
        ):
            return _from_row(fresh_enough)

    # Slow path — must call LB to know current LB.updated_at.
    fresh = build_context_uncached(
        conversation,
        conversation_observations=conv_obs,
        lb_context_client=lb_context_client,
        lb_user_id=lb_user_id,
    )

    with transaction.atomic():
        try:
            existing = ConversationContext.objects.select_for_update().get(
                conversation=conversation,
            )
        except ConversationContext.DoesNotExist:
            existing = None

        if existing is not None and existing.source_versions_json == fresh.source_versions:
            # Fingerprint unchanged — just bump resolved_at so the TTL
            # short-circuit works next time.
            existing.resolved_at = fresh.resolved_at
            existing.save(update_fields=['resolved_at', 'updated_at'])
            return _from_row(existing)

        # Cache miss or fingerprint drift. Persist fresh.
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
