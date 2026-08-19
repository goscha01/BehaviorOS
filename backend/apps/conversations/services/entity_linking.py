"""Persist ResolutionResult batches to EntityLink rows.

Kept separate from the resolvers so that:
- Resolvers can be tested without a DB.
- Multiple resolvers can be composed and their results persisted in one
  transaction.
- The persistence rules (dedupe, upsert semantics) live in one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from django.db import transaction

from apps.conversations.models import Conversation, EntityLink
from apps.conversations.resolvers.base import ResolutionResult

logger = logging.getLogger(__name__)


@dataclass
class EntityLinkPersistResult:
    created: int = 0
    already_present: int = 0


def persist_entity_links(
    conversation: Conversation,
    results: Iterable[ResolutionResult],
) -> EntityLinkPersistResult:
    """Persist each ResolutionResult as an EntityLink row.

    Deduped on the unique constraint (conversation, target_system,
    target_type, target_id, match_method). Duplicates already in the
    DB are treated as "already_present" and NOT overwritten — this
    keeps `matched_at` from being clobbered on re-runs.
    """
    outcome = EntityLinkPersistResult()

    with transaction.atomic():
        for result in results:
            link, created = EntityLink.objects.get_or_create(
                conversation=conversation,
                target_system=result.target_system,
                target_type=result.target_type,
                target_id=result.target_id,
                match_method=result.match_method,
                defaults={
                    'confidence': result.confidence,
                    'metadata': dict(result.metadata),
                },
            )
            if created:
                outcome.created += 1
            else:
                outcome.already_present += 1

    logger.debug(
        'entity linking persisted for conv=%s: created=%d already=%d',
        conversation.id, outcome.created, outcome.already_present,
    )
    return outcome
