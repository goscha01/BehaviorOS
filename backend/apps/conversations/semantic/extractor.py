"""Semantic event extraction service — orchestrates preprocess → LLM →
validate → dedupe → persist for one conversation, one extraction run.

Idempotent per (extraction_run, conversation) — reruns of the same
(corpus, extractor_version, ontology_version, prompt_version, model)
tuple are no-ops. To try a new prompt or ontology, create a NEW
extraction_run (its `unique_together` constraint enforces this).

Outcome IS NOT passed to the LLM. If a caller ever needs to peek at
outcomes at extraction time (e.g. active learning), do it in a
downstream pipeline stage, not here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.utils import timezone

from apps.conversations.models import (
    Conversation,
    ConversationSemanticEvent,
    EntityLink,
    LearningCorpusMember,
    SemanticExtractionRun,
    TargetSystem,
    TargetType,
)
from apps.conversations.semantic.ontology import ONTOLOGY_VERSION
from apps.conversations.semantic.preprocessing import (
    chunk_conversation, load_and_normalize, merge_extracted_events,
    render_turns_for_prompt,
)
from apps.conversations.semantic.prompt import (
    PROMPT_VERSION, SYSTEM_PROMPT, render_user_prompt,
)
from apps.conversations.semantic.validator import validate_events
from apps.learning.services.llm_client import LearningLLMClient, LLMProviderError

logger = logging.getLogger(__name__)


EXTRACTOR_VERSION = 'extractor-v1'


@dataclass
class ExtractRecordResult:
    conversation_id: str
    events_created: int = 0
    events_rejected: int = 0
    chunks: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal('0')
    skipped_reason: str = ''
    error: str = ''

    @property
    def ok(self) -> bool:
        return not self.error and not self.skipped_reason


class SemanticExtractor:
    """One instance per extraction run. Reuses the LLM client + LB link
    lookup across conversations in the run."""

    def __init__(
        self,
        run: SemanticExtractionRun,
        *,
        client: Optional[LearningLLMClient] = None,
        max_tokens: int = 4000,
    ):
        self.run = run
        self.client = client or LearningLLMClient()
        self.max_tokens = max_tokens
        # Sanity guard — the run's ontology/prompt/extractor versions
        # must match what this code currently ships. Bump the module
        # constants BEFORE running against a new version.
        if run.ontology_version != ONTOLOGY_VERSION:
            raise ValueError(
                f'run ontology_version={run.ontology_version!r} != '
                f'code ONTOLOGY_VERSION={ONTOLOGY_VERSION!r}'
            )
        if run.prompt_version != PROMPT_VERSION:
            raise ValueError(
                f'run prompt_version={run.prompt_version!r} != '
                f'code PROMPT_VERSION={PROMPT_VERSION!r}'
            )
        if run.extractor_version != EXTRACTOR_VERSION:
            raise ValueError(
                f'run extractor_version={run.extractor_version!r} != '
                f'code EXTRACTOR_VERSION={EXTRACTOR_VERSION!r}'
            )

    def extract_conversation(
        self, conversation: Conversation,
    ) -> ExtractRecordResult:
        """Extract events for ONE conversation. Idempotent: if events
        already exist for this (run, conversation) pair, skip."""
        result = ExtractRecordResult(conversation_id=str(conversation.id))

        # Idempotency guard — a previous partial run may have completed this row.
        existing = ConversationSemanticEvent.objects.filter(
            extraction_run=self.run, conversation=conversation,
        ).exists()
        if existing:
            result.skipped_reason = 'already_extracted'
            return result

        turns = load_and_normalize(conversation)
        if not turns:
            result.skipped_reason = 'no_turns_after_preprocess'
            return result

        # Absolute max index — used by validator to reject out-of-range
        # turn refs. This is the LAST NORMALIZED turn's original idx, not
        # the count; extractors output ABSOLUTE indices.
        max_turn_index = turns[-1].idx

        chunks = chunk_conversation(turns)
        result.chunks = len(chunks)

        per_chunk_events: list[list[dict]] = []
        for chunk in chunks:
            try:
                llm_result = self.client.analyze(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=render_user_prompt(render_turns_for_prompt(chunk)),
                    model=self.run.model,
                    max_tokens=self.max_tokens,
                )
            except LLMProviderError as exc:
                logger.warning(
                    'extractor: LLM failed for conv=%s chunk=%d: %s',
                    conversation.id, chunk.chunk_index, exc,
                )
                result.error = f'llm: {exc}'
                # Continue with any events already collected from prior chunks.
                continue

            result.input_tokens += llm_result.input_tokens
            result.output_tokens += llm_result.output_tokens
            result.cost_usd += llm_result.cost_usd

            validated = validate_events(
                llm_result.parsed_json, max_turn_index=max_turn_index,
            )
            result.events_rejected += len(validated.rejected)
            if not validated.any_valid:
                if validated.rejected:
                    logger.info(
                        'extractor: conv=%s chunk=%d yielded no valid events (%d rejected)',
                        conversation.id, chunk.chunk_index, len(validated.rejected),
                    )
                per_chunk_events.append([])
                continue
            per_chunk_events.append(validated.events)

        merged = merge_extracted_events(per_chunk_events)

        # Pre-resolve LB EntityLink once per conversation (used for
        # `entity_link` FK on every persisted event).
        lb_link = conversation.entity_links.filter(
            target_system=TargetSystem.LEADBRIDGE,
            target_type=TargetType.LEAD,
        ).first()

        # Persist all events for this conversation in one transaction.
        # Ordinal is assigned in merge order.
        with transaction.atomic():
            for ordinal, ev in enumerate(merged):
                ConversationSemanticEvent.objects.create(
                    org=conversation.org,
                    conversation=conversation,
                    entity_link=lb_link,
                    extraction_run=self.run,
                    ordinal=ordinal,
                    event_type=ev['event_type'],
                    actor=ev['actor'],
                    turn_start=ev['turn_start'],
                    turn_end=ev['turn_end'],
                    confidence=ev['confidence'],
                    attributes=ev['attributes'],
                    evidence_text=ev['evidence'],
                )
                result.events_created += 1

        return result


# ---------------------------------------------------------------------------
# Run orchestration (batch of conversations)
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    records_processed: int = 0
    records_failed: int = 0
    records_skipped: int = 0
    events_created: int = 0
    events_rejected: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal('0')
    per_conversation: list[ExtractRecordResult] = field(default_factory=list)


def get_or_create_run(
    corpus, *, org, model: str, provider_hint: str = '',
) -> SemanticExtractionRun:
    """Get-or-create the extraction run for the current
    (corpus × ontology × prompt × extractor × model) tuple."""
    run, _ = SemanticExtractionRun.objects.get_or_create(
        corpus=corpus,
        extractor_version=EXTRACTOR_VERSION,
        ontology_version=ONTOLOGY_VERSION,
        prompt_version=PROMPT_VERSION,
        model=model,
        defaults={
            'org': org,
            'provider': provider_hint,
            'status': SemanticExtractionRun.Status.PENDING,
        },
    )
    return run


def run_extraction(
    corpus, *, org, model: str,
    conversation_ids: Optional[list] = None,
    max_tokens: int = 4000,
    on_record: Optional[callable] = None,
) -> RunResult:
    """Run extraction over corpus members. Idempotent per (run, conversation).

    `conversation_ids` limits to a subset (used for the 30-record eval).
    `on_record` is an optional callback invoked after each conversation —
    useful for CLI progress reporting.
    """
    run = get_or_create_run(corpus, org=org, model=model)
    run.status = SemanticExtractionRun.Status.RUNNING
    if run.started_at is None:
        run.started_at = timezone.now()
    run.save(update_fields=['status', 'started_at', 'updated_at'])

    members_qs = LearningCorpusMember.objects.filter(corpus=corpus).select_related('conversation')
    if conversation_ids:
        members_qs = members_qs.filter(conversation_id__in=conversation_ids)

    extractor = SemanticExtractor(run, max_tokens=max_tokens)
    outcome = RunResult()

    for member in members_qs.iterator():
        conv = member.conversation
        try:
            rec = extractor.extract_conversation(conv)
        except Exception as exc:  # noqa: BLE001
            logger.exception('extractor: uncaught error on conv=%s', conv.id)
            rec = ExtractRecordResult(conversation_id=str(conv.id),
                                       error=f'crash: {type(exc).__name__}: {exc}')

        outcome.per_conversation.append(rec)
        if rec.skipped_reason:
            outcome.records_skipped += 1
        elif rec.error:
            outcome.records_failed += 1
        else:
            outcome.records_processed += 1

        outcome.events_created += rec.events_created
        outcome.events_rejected += rec.events_rejected
        outcome.input_tokens += rec.input_tokens
        outcome.output_tokens += rec.output_tokens
        outcome.cost_usd += rec.cost_usd

        if on_record:
            on_record(rec)

    # Finalize run.
    run.records_processed = outcome.records_processed
    run.records_failed = outcome.records_failed
    run.events_created = outcome.events_created
    run.input_tokens = outcome.input_tokens
    run.output_tokens = outcome.output_tokens
    run.cost_usd = outcome.cost_usd
    run.completed_at = timezone.now()
    if outcome.records_failed and outcome.records_processed:
        run.status = SemanticExtractionRun.Status.PARTIAL
    elif outcome.records_failed:
        run.status = SemanticExtractionRun.Status.FAILED
    else:
        run.status = SemanticExtractionRun.Status.COMPLETED
    run.save()
    return outcome
