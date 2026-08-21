"""Per-conversation service-scope extractor (Pipeline 1D Ship D).

Same shape as pricing/qualification/FAQ. Ship D novelty: fact_type
determines whether an event reaches the diff:

  agent_scope_statement   -> aggregated as ObservedBusinessFact (in diff)
  customer_scope_question -> aggregated separately (persisted, not in diff)
  performed_observed      -> aggregated separately (persisted, not in diff)

The actor-discipline gate is enforced here (drop mismatched actor/
fact_type combinations) BEFORE aggregation, so the diff can never
see a scope claim that wasn't from an agent statement.
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
)
from apps.conversations.observed_config.service_scope.prompt import (
    SCOPE_ITEMS, SCOPE_RELATIONSHIPS,
    SERVICE_SCOPE_EXTRACTOR_VERSION,
    SYSTEM_PROMPT, build_user_prompt,
)
from apps.conversations.semantic.preprocessing import (
    chunk_conversation, load_and_normalize, render_turns_for_prompt,
)

logger = logging.getLogger(__name__)


VALID_SCOPE_ITEMS = frozenset(SCOPE_ITEMS)
VALID_RELATIONSHIPS = frozenset(SCOPE_RELATIONSHIPS)
VALID_FACT_TYPES = frozenset({
    'agent_scope_statement',
    'customer_scope_question',
    'performed_observed',
})


@dataclass
class PerConversationExtraction:
    conversation_id: str
    scope_events: list[dict] = field(default_factory=list)
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
        entries = parsed.get('scope_events', []) or []
        if isinstance(entries, list):
            events_out.extend(_validate(entries))
        total_in += getattr(r, 'input_tokens', 0)
        total_out += getattr(r, 'output_tokens', 0)
        total_cost += getattr(r, 'cost_usd', Decimal('0'))

    return PerConversationExtraction(
        conversation_id=str(conv.id),
        scope_events=events_out,
        llm_input_tokens=total_in,
        llm_output_tokens=total_out,
        llm_cost_usd=total_cost,
    )


def _validate(entries: list) -> list[dict]:
    """Drop entries that violate the actor-discipline gate or the
    scope-item taxonomy."""
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        ft = e.get('fact_type')
        if ft not in VALID_FACT_TYPES:
            continue
        ev = e.get('evidence') or {}
        actor = ev.get('actor')
        if not ev.get('turn_id') or not ev.get('evidence_text'):
            continue
        # Actor discipline
        if ft == 'agent_scope_statement' and actor != 'agent':
            continue
        if ft == 'customer_scope_question' and actor != 'customer':
            continue
        # performed_observed: either actor OK
        # relationship required only for agent_scope_statement
        if ft == 'agent_scope_statement':
            rel = e.get('relationship')
            if rel not in VALID_RELATIONSHIPS:
                continue
        else:
            e.pop('relationship', None)
        scope_item = e.get('scope_item')
        if scope_item not in VALID_SCOPE_ITEMS:
            continue
        if scope_item == 'other':
            other_topic = str(e.get('other_topic') or '').strip().lower()
            if not other_topic:
                continue
            e['other_topic'] = other_topic
        else:
            e.pop('other_topic', None)
        try:
            conf = float(e.get('confidence') or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < 0.5:
            continue
        e['confidence'] = conf
        service = e.get('service')
        e['service'] = (
            str(service).strip().lower() if service else None
        )
        # Sanitize context payload
        ctx = e.get('context') or {}
        clean_ctx = {}
        for k in ('frequency', 'condition'):
            v = ctx.get(k)
            if v:
                clean_ctx[k] = str(v).strip().lower()
        e['context'] = clean_ctx
        out.append(e)
    return out


def create_or_reuse_run(
    *,
    org,
    corpus: LearningCorpus,
    model: str = 'gpt-4o-mini',
) -> tuple[ObservedFactExtractionRun, bool]:
    non_terminal = ObservedFactExtractionRun.objects.filter(
        org=org, corpus=corpus,
        domain=ObservedBusinessFact.Domain.SERVICE_SCOPE,
        extractor_version=SERVICE_SCOPE_EXTRACTOR_VERSION,
        status__in=[
            ObservedFactExtractionRun.Status.PENDING,
            ObservedFactExtractionRun.Status.RUNNING,
        ],
    ).order_by('-created_at').first()
    if non_terminal is not None:
        return (non_terminal, False)
    run = ObservedFactExtractionRun.objects.create(
        org=org, corpus=corpus,
        domain=ObservedBusinessFact.Domain.SERVICE_SCOPE,
        extractor_version=SERVICE_SCOPE_EXTRACTOR_VERSION,
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
        f'service-scope-extractor: run_id={run.id} started; '
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

    for member in member_qs.iterator():
        conv = member.conversation
        try:
            extraction = extract_from_conversation(
                conv, llm_client=llm_client, model=model,
            )
        except Exception as exc:
            logger.exception(
                'service-scope-extractor: conv=%s failed: %s',
                conv.id, exc,
            )
            continue
        per_conv_extractions.append(extraction)
        total_in += extraction.llm_input_tokens
        total_out += extraction.llm_output_tokens
        total_cost += extraction.llm_cost_usd
        processed += 1

    from apps.conversations.observed_config.service_scope.aggregator import (
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
    run.llm_input_tokens = total_in
    run.llm_output_tokens = total_out
    run.llm_cost_usd = total_cost
    by_ft = {'agent_scope_statement': 0, 'customer_scope_question': 0, 'performed_observed': 0}
    for e in per_conv_extractions:
        for entry in e.scope_events:
            ft = entry.get('fact_type')
            if ft in by_ft:
                by_ft[ft] += 1
    total_events = sum(by_ft.values())
    run.stats_json = {
        'raw_events_by_fact_type': by_ft,
        'total_raw_events': total_events,
        'agent_policy_share': (
            by_ft['agent_scope_statement'] / total_events
            if total_events > 0 else None
        ),
    }
    run.save()
    logger.info(
        f'service-scope-extractor: run_id={run.id} completed; '
        f'processed={processed} facts={facts_emitted} '
        f'events_by_ft={by_ft} cost=${total_cost}'
    )
    return run
