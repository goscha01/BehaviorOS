"""Persist a NormalizedConversation (from an adapter's normalizer output)
into the Conversation + ConversationTurn tables.

Upsert semantics:
- Conversation is upserted on (org, source, source_conversation_id).
  Existing rows have their mutable fields (channel, ended_at, metadata)
  refreshed; started_at is preserved (it's the FIRST time we saw this
  conversation, and later re-imports shouldn't move it forward).
- Turns are upserted on (conversation, source_turn_id). An existing turn
  is NOT overwritten — if we've already stored a segment, we trust our
  earlier normalization. Turn dedupe is the whole point of stable
  source_turn_id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction

from apps.conversations.models import (
    Conversation,
    ConversationTurn,
    IngestionStatus,
)
from apps.conversations.normalization.quo import NormalizedConversation

logger = logging.getLogger(__name__)


@dataclass
class PersistResult:
    conversation: Conversation
    conversation_created: bool
    turns_created: int
    turns_already_present: int


def persist_normalized_conversation(
    normalized: NormalizedConversation,
    *,
    org,
    import_run_id: str = '',
) -> PersistResult:
    """Upsert Conversation + its Turns. Idempotent on re-import."""
    with transaction.atomic():
        conversation, created = Conversation.objects.get_or_create(
            org=org,
            source=normalized.source,
            source_conversation_id=normalized.source_conversation_id,
            defaults={
                'channel': normalized.channel,
                'customer_phone': normalized.customer_phone,
                'customer_email': normalized.customer_email,
                'started_at': normalized.started_at,
                'ended_at': normalized.ended_at,
                'metadata': dict(normalized.metadata),
                'ingestion_status': IngestionStatus.NORMALIZED,
                'import_run_id': import_run_id,
            },
        )

        if not created:
            # Refresh mutable fields but never move started_at forward
            # or clobber metadata the caller passed the first time.
            changed_fields = []
            if conversation.channel != normalized.channel:
                conversation.channel = normalized.channel
                changed_fields.append('channel')
            # Only update customer_phone if we now have a value AND we
            # didn't before (avoid overwriting a good normalization with
            # a bad one from a later record).
            if not conversation.customer_phone and normalized.customer_phone:
                conversation.customer_phone = normalized.customer_phone
                changed_fields.append('customer_phone')
            if not conversation.customer_email and normalized.customer_email:
                conversation.customer_email = normalized.customer_email
                changed_fields.append('customer_email')
            # ended_at can move FORWARD as new events arrive.
            if normalized.ended_at and (
                not conversation.ended_at
                or normalized.ended_at > conversation.ended_at
            ):
                conversation.ended_at = normalized.ended_at
                changed_fields.append('ended_at')
            # Merge metadata: existing keys survive, new keys get added.
            if normalized.metadata:
                existing = conversation.metadata or {}
                merged = {**existing, **normalized.metadata}
                if merged != existing:
                    conversation.metadata = merged
                    changed_fields.append('metadata')
            if import_run_id:
                conversation.import_run_id = import_run_id
                changed_fields.append('import_run_id')
            if changed_fields:
                changed_fields.append('updated_at')
                conversation.save(update_fields=changed_fields)

        turns_created = 0
        turns_already_present = 0
        for nt in normalized.turns:
            _, turn_created = ConversationTurn.objects.get_or_create(
                conversation=conversation,
                source_turn_id=nt.source_turn_id,
                defaults={
                    'speaker': nt.speaker,
                    'direction': nt.direction,
                    'text': nt.text,
                    'occurred_at': nt.occurred_at,
                    'confidence': nt.confidence,
                    'metadata': dict(nt.metadata),
                },
            )
            if turn_created:
                turns_created += 1
            else:
                turns_already_present += 1

    logger.debug(
        'persisted conversation source=%s ext_id=%s created=%s turns_new=%d turns_dup=%d',
        normalized.source, normalized.source_conversation_id, created,
        turns_created, turns_already_present,
    )

    return PersistResult(
        conversation=conversation,
        conversation_created=created,
        turns_created=turns_created,
        turns_already_present=turns_already_present,
    )
