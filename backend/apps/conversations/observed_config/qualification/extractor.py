"""Per-conversation qualification extractor (Pipeline 1D Ship B).

Same shape as pricing/extractor.py — reuses the semantic preprocessing
turn-id convention, produces PerConversationExtraction, defers
persistence to the aggregator so the extraction run wraps many
conversations in one atomic phase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from django.utils import timezone

from apps.conversations.models import (
    Conversation, LearningCorpus, LearningCorpusMember,
    ObservedBusinessFact, ObservedFactExtractionRun,
    OntologyReviewCandidate,
)
from apps.conversations.observed_config.qualification.prompt import (
    QUALIFICATION_EXTRACTOR_VERSION, QUALIFICATION_FIELDS,
    SYSTEM_PROMPT, build_user_prompt,
)
from apps.conversations.semantic.preprocessing import (
    chunk_conversation, load_and_normalize, render_turns_for_prompt,
)

logger = logging.getLogger(__name__)


VALID_FACT_TYPES = frozenset({
    'question_asked', 'answer_provided',
    'volunteered_before_question',
})
VALID_FIELDS = frozenset(QUALIFICATION_FIELDS)


@dataclass
class PerConversationExtraction:
    conversation_id: str
    events: list[dict] = field(default_factory=list)
    ontology_review: list[dict] = field(default_factory=list)
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: Decimal = Decimal('0')


def extract_from_conversation(
    conv: Conversation, *, llm_client, model: str = 'gpt-4o-mini',
) -> PerConversationExtraction:
    turns, _turn_map = load_and_normalize(conv)
    if not turns:
        return PerConversationExtraction(conversation_id=str(conv.id))
    chunks = chunk_conversation(turns)

    events_out: list[dict] = []
    review_out: list[dict] = []
    total_in = 0
    total_out = 0
    total_cost = Decimal('0')

    for chunk in chunks:
        rendered = render_turns_for_prompt(chunk)
        user = build_user_prompt(rendered, str(conv.id))
        r = llm_client.analyze(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user,
            model=model,
            max_tokens=1500,
        )
        parsed = r.parsed_json or {}
        if not isinstance(parsed, dict):
            parsed = {}
        events = parsed.get('events', []) or []
        review = parsed.get('ontology_review', []) or []
        if isinstance(events, list):
            events_out.extend(_validate_events(events))
        if isinstance(review, list):
            review_out.extend(_validate_review(review))
        total_in += getattr(r, 'input_tokens', 0)
        total_out += getattr(r, 'output_tokens', 0)
        total_cost += getattr(r, 'cost_usd', Decimal('0'))

    return PerConversationExtraction(
        conversation_id=str(conv.id),
        events=events_out,
        ontology_review=review_out,
        llm_input_tokens=total_in,
        llm_output_tokens=total_out,
        llm_cost_usd=total_cost,
    )


def _validate_events(entries: list) -> list[dict]:
    """Drop entries that don't match the qualification schema."""
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get('fact_type') not in VALID_FACT_TYPES:
            continue
        field_val = e.get('field')
        if field_val not in VALID_FIELDS:
            continue
        ev = e.get('evidence') or {}
        if not ev.get('field_turn_id') or not ev.get('evidence_text'):
            continue
        try:
            conf = float(e.get('confidence') or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < 0.5:
            continue
        e['confidence'] = conf
        # Normalize other_topic to snake_case string; drop if field != other
        if field_val == 'other':
            topic = str(e.get('other_topic') or '').strip().lower()
            if not topic:
                continue
            e['other_topic'] = topic
        else:
            e.pop('other_topic', None)
        # Normalize service_context to snake_case lower
        sc = e.get('service_context')
        if sc:
            e['service_context'] = str(sc).strip().lower()
        else:
            e['service_context'] = None
        out.append(e)
    return out


def _validate_review(entries: list) -> list[dict]:
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get('kind') != 'event_mis_classified':
            continue
        if not e.get('evidence_turn_id') or not e.get('evidence_text'):
            continue
        try:
            e['confidence'] = float(e.get('confidence') or 0.0)
        except (TypeError, ValueError):
            e['confidence'] = 0.0
        out.append(e)
    return out


def create_or_reuse_run(
    *,
    org,
    corpus: LearningCorpus,
    model: str = 'gpt-4o-mini',
) -> tuple[ObservedFactExtractionRun, bool]:
    """Enqueue-time idempotency mirror of pricing/extractor.py."""
    non_terminal = ObservedFactExtractionRun.objects.filter(
        org=org, corpus=corpus,
        domain=ObservedBusinessFact.Domain.QUALIFICATION,
        extractor_version=QUALIFICATION_EXTRACTOR_VERSION,
        status__in=[
            ObservedFactExtractionRun.Status.PENDING,
            ObservedFactExtractionRun.Status.RUNNING,
        ],
    ).order_by('-created_at').first()
    if non_terminal is not None:
        return (non_terminal, False)
    run = ObservedFactExtractionRun.objects.create(
        org=org, corpus=corpus,
        domain=ObservedBusinessFact.Domain.QUALIFICATION,
        extractor_version=QUALIFICATION_EXTRACTOR_VERSION,
        model=model,
        status=ObservedFactExtractionRun.Status.PENDING,
    )
    return (run, True)


def run_extraction_for_existing(
    *,
    run: ObservedFactExtractionRun,
    llm_client,
    model: str = 'gpt-4o-mini',
    limit: Optional[int] = None,
) -> ObservedFactExtractionRun:
    if run.status in (
        ObservedFactExtractionRun.Status.COMPLETED,
        ObservedFactExtractionRun.Status.FAILED,
    ):
        return run
    run.status = ObservedFactExtractionRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=['status', 'started_at'])
    logger.info(
        f'qualification-extractor: run_id={run.id} started; '
        f'corpus={run.corpus_id} limit={limit!r}'
    )

    member_qs = (
        LearningCorpusMember.objects
        .filter(corpus_id=run.corpus_id)
        .select_related('conversation')
    )
    if limit:
        member_qs = member_qs[:limit]

    per_conv_extractions: list[PerConversationExtraction] = []
    total_in = 0
    total_out = 0
    total_cost = Decimal('0')
    processed = 0
    reviews_persisted = 0

    for member in member_qs.iterator():
        conv = member.conversation
        try:
            extraction = extract_from_conversation(
                conv, llm_client=llm_client, model=model,
            )
        except Exception as exc:
            logger.exception(
                'qualification-extractor: conv=%s failed: %s',
                conv.id, exc,
            )
            continue
        per_conv_extractions.append(extraction)
        total_in += extraction.llm_input_tokens
        total_out += extraction.llm_output_tokens
        total_cost += extraction.llm_cost_usd
        processed += 1
        for rev in extraction.ontology_review:
            OntologyReviewCandidate.objects.create(
                org=run.org,
                extraction_run=run,
                kind=OntologyReviewCandidate.Kind.EVENT_MIS_CLASSIFIED,
                original_event_type=(
                    rev.get('original_event_type', '')
                ),
                proposed_scope=rev.get('proposed_scope', ''),
                proposed_topic=rev.get('proposed_topic', ''),
                evidence_conversation_id=str(conv.id),
                evidence_turn_id=rev.get('evidence_turn_id', ''),
                evidence_text=(rev.get('evidence_text') or '')[:1000],
                confidence=rev.get('confidence', 0.0),
            )
            reviews_persisted += 1

    from apps.conversations.observed_config.qualification.aggregator import (
        aggregate_and_persist,
    )
    facts_emitted = aggregate_and_persist(
        run=run,
        per_conv_extractions=per_conv_extractions,
        total_processed=processed,
    )

    run.status = ObservedFactExtractionRun.Status.COMPLETED
    run.completed_at = timezone.now()
    run.conversations_processed = processed
    run.facts_emitted = facts_emitted
    run.ontology_review_candidates_emitted = reviews_persisted
    run.llm_input_tokens = total_in
    run.llm_output_tokens = total_out
    run.llm_cost_usd = total_cost
    total_events = sum(len(x.events) for x in per_conv_extractions)
    run.stats_json = {
        'per_conversation_avg_events': (
            total_events / max(processed, 1)
        ),
        'total_events_before_aggregation': total_events,
    }
    run.save()
    logger.info(
        f'qualification-extractor: run_id={run.id} completed; '
        f'processed={processed} facts={facts_emitted} '
        f'reviews={reviews_persisted} cost=${total_cost}'
    )
    return run
