"""Per-conversation FAQ extractor (Pipeline 1D Ship C).

Same shape as pricing/qualification extractors — reuses semantic
preprocessing turn IDs; produces PerConversationExtraction; defers
persistence to the aggregator.

Ship C novelty: `configuration_scope` classification splits events
into BUSINESS_FAQ (participate in diff) vs TRANSACTIONAL_OPERATION
(emit OntologyReviewCandidate instead) vs UNCLEAR (dropped).
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
from apps.conversations.observed_config.faq.prompt import (
    FAQ_BUSINESS_TOPICS, FAQ_CONFIGURATION_SCOPES,
    FAQ_EXTRACTOR_VERSION, SYSTEM_PROMPT, build_user_prompt,
)
from apps.conversations.semantic.preprocessing import (
    chunk_conversation, load_and_normalize, render_turns_for_prompt,
)

logger = logging.getLogger(__name__)


VALID_TOPICS = frozenset(FAQ_BUSINESS_TOPICS)
VALID_SCOPES = frozenset(FAQ_CONFIGURATION_SCOPES)


@dataclass
class PerConversationExtraction:
    conversation_id: str
    business_faq_events: list[dict] = field(default_factory=list)
    transactional_events: list[dict] = field(default_factory=list)
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

    business_out: list[dict] = []
    transactional_out: list[dict] = []
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
        entries = parsed.get('faq_events', []) or []
        if isinstance(entries, list):
            biz, tx = _split_and_validate(entries)
            business_out.extend(biz)
            transactional_out.extend(tx)
        total_in += getattr(r, 'input_tokens', 0)
        total_out += getattr(r, 'output_tokens', 0)
        total_cost += getattr(r, 'cost_usd', Decimal('0'))

    return PerConversationExtraction(
        conversation_id=str(conv.id),
        business_faq_events=business_out,
        transactional_events=transactional_out,
        llm_input_tokens=total_in,
        llm_output_tokens=total_out,
        llm_cost_usd=total_cost,
    )


def _split_and_validate(entries: list) -> tuple[list[dict], list[dict]]:
    """Return (business_faq_valid, transactional_valid). UNCLEAR
    entries are dropped."""
    biz = []
    tx = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        scope = e.get('configuration_scope')
        if scope not in VALID_SCOPES:
            continue
        ev = e.get('evidence') or {}
        if not ev.get('question_turn_id') or not ev.get('evidence_text'):
            continue
        try:
            conf = float(e.get('confidence') or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < 0.5:
            continue
        e['confidence'] = conf
        if scope == 'BUSINESS_FAQ':
            topic = e.get('topic')
            if topic not in VALID_TOPICS:
                continue
            intent = str(e.get('intent') or '').strip().lower()
            if not intent:
                continue
            e['intent'] = intent
            if topic == 'other':
                other_topic = str(e.get('other_topic') or '').strip().lower()
                if not other_topic:
                    continue
                e['other_topic'] = other_topic
            else:
                e.pop('other_topic', None)
            biz.append(e)
        elif scope == 'TRANSACTIONAL_OPERATION':
            tk = str(e.get('transactional_kind') or '').strip().lower()
            if not tk:
                tk = 'other_operational'
            e['transactional_kind'] = tk
            tx.append(e)
        # UNCLEAR dropped
    return biz, tx


def create_or_reuse_run(
    *,
    org,
    corpus: LearningCorpus,
    model: str = 'gpt-4o-mini',
) -> tuple[ObservedFactExtractionRun, bool]:
    non_terminal = ObservedFactExtractionRun.objects.filter(
        org=org, corpus=corpus,
        domain=ObservedBusinessFact.Domain.FAQ,
        extractor_version=FAQ_EXTRACTOR_VERSION,
        status__in=[
            ObservedFactExtractionRun.Status.PENDING,
            ObservedFactExtractionRun.Status.RUNNING,
        ],
    ).order_by('-created_at').first()
    if non_terminal is not None:
        return (non_terminal, False)
    run = ObservedFactExtractionRun.objects.create(
        org=org, corpus=corpus,
        domain=ObservedBusinessFact.Domain.FAQ,
        extractor_version=FAQ_EXTRACTOR_VERSION,
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
        f'faq-extractor: run_id={run.id} started; '
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
    transactional_persisted = 0

    for member in member_qs.iterator():
        conv = member.conversation
        try:
            extraction = extract_from_conversation(
                conv, llm_client=llm_client, model=model,
            )
        except Exception as exc:
            logger.exception(
                'faq-extractor: conv=%s failed: %s', conv.id, exc,
            )
            continue
        per_conv_extractions.append(extraction)
        total_in += extraction.llm_input_tokens
        total_out += extraction.llm_output_tokens
        total_cost += extraction.llm_cost_usd
        processed += 1

        # Persist TRANSACTIONAL_OPERATION events as
        # OntologyReviewCandidates — they were upstream classified as
        # QUESTION_FAQ but Ship C thinks they're operational. Never
        # auto-mutates the extractor; accumulates evidence for later.
        for tx in extraction.transactional_events:
            ev = tx.get('evidence') or {}
            OntologyReviewCandidate.objects.create(
                org=run.org,
                extraction_run=run,
                kind=OntologyReviewCandidate.Kind.EVENT_MIS_CLASSIFIED,
                original_event_type='QUESTION_FAQ',
                proposed_scope=(
                    f'transactional/{tx.get("transactional_kind","")}'
                )[:64],
                proposed_topic=tx.get('transactional_kind', ''),
                evidence_conversation_id=str(conv.id),
                evidence_turn_id=ev.get('question_turn_id', ''),
                evidence_text=(ev.get('evidence_text') or '')[:1000],
                confidence=tx.get('confidence', 0.0),
            )
            transactional_persisted += 1

    from apps.conversations.observed_config.faq.aggregator import (
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
    run.ontology_review_candidates_emitted = transactional_persisted
    run.llm_input_tokens = total_in
    run.llm_output_tokens = total_out
    run.llm_cost_usd = total_cost
    total_biz = sum(
        len(x.business_faq_events) for x in per_conv_extractions
    )
    total_tx = sum(
        len(x.transactional_events) for x in per_conv_extractions
    )
    run.stats_json = {
        'total_business_faq_events_raw': total_biz,
        'total_transactional_events_raw': total_tx,
        'operational_noise_ratio': (
            total_tx / (total_biz + total_tx)
            if (total_biz + total_tx) > 0 else None
        ),
    }
    run.save()
    logger.info(
        f'faq-extractor: run_id={run.id} completed; '
        f'processed={processed} biz_facts={facts_emitted} '
        f'transactional={transactional_persisted} '
        f'cost=${total_cost}'
    )
    return run
