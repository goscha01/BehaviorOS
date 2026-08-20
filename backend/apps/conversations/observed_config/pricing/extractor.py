"""Per-conversation pricing extractor (Pipeline 1D).

Reads conversation turns via the existing semantic preprocessing (same
turn-id convention), calls the LLM with the pricing extractor prompt,
validates + persists results into ObservedBusinessFact rows keyed by
canonical subject_key. Aggregation across the corpus is a separate
pass (aggregator.py).

Idempotent per (org, corpus, extractor_version). A re-run creates a
new ObservedFactExtractionRun and NEW facts; older runs are preserved
so the audit can pick a specific run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.utils import timezone

from apps.conversations.measurement.effective_config_contract import (
    canonical_json_bytes, sha256_hex,
)
from apps.conversations.models import (
    Conversation, LearningCorpus, LearningCorpusMember,
    ObservedBusinessFact, ObservedFactExtractionRun,
    OntologyReviewCandidate,
)
from apps.conversations.observed_config.base import (
    canonical_subject_key,
)
from apps.conversations.observed_config.pricing.prompt import (
    PRICING_EXTRACTOR_VERSION, SYSTEM_PROMPT, build_user_prompt,
)
from apps.conversations.semantic.preprocessing import (
    chunk_conversation, load_and_normalize, render_turns_for_prompt,
)

logger = logging.getLogger(__name__)


PRICING_FACT_TYPES = frozenset({
    'quoted_price', 'price_range', 'discount_offered',
})


@dataclass
class PerConversationExtraction:
    conversation_id: str
    prices: list[dict] = field(default_factory=list)
    ontology_review: list[dict] = field(default_factory=list)
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: Decimal = Decimal('0')


def extract_from_conversation(
    conv: Conversation, *, llm_client, model: str = 'gpt-4o-mini',
) -> PerConversationExtraction:
    """Run the pricing extractor on a single conversation. Returns the
    parsed LLM response with per-conversation cost / token counts.

    Does NOT persist. Persistence is the caller's responsibility so
    the extraction run can wrap many conversations in a single txn.
    """
    turns, _turn_map = load_and_normalize(conv)
    if not turns:
        return PerConversationExtraction(
            conversation_id=str(conv.id),
        )
    chunks = chunk_conversation(turns)

    aggregated_prices: list[dict] = []
    aggregated_review: list[dict] = []
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
            max_tokens=1200,
        )
        parsed = r.parsed_json or {}
        if not isinstance(parsed, dict):
            logger.warning(
                'pricing extractor: non-dict response for conv=%s',
                conv.id,
            )
            parsed = {}
        prices = parsed.get('prices', []) or []
        review = parsed.get('ontology_review', []) or []
        if isinstance(prices, list):
            aggregated_prices.extend(_validate_prices(prices))
        if isinstance(review, list):
            aggregated_review.extend(_validate_review(review))
        total_in += getattr(r, 'input_tokens', 0)
        total_out += getattr(r, 'output_tokens', 0)
        total_cost += getattr(r, 'cost_usd', Decimal('0'))

    return PerConversationExtraction(
        conversation_id=str(conv.id),
        prices=aggregated_prices,
        ontology_review=aggregated_review,
        llm_input_tokens=total_in,
        llm_output_tokens=total_out,
        llm_cost_usd=total_cost,
    )


def _validate_prices(entries: list) -> list[dict]:
    """Filter entries that don't match the pricing schema. Drops
    fabrications: entries missing a quote_turn_id or a value payload."""
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get('fact_type') not in PRICING_FACT_TYPES:
            continue
        ev = e.get('evidence') or {}
        if not ev.get('quote_turn_id'):
            continue
        if not ev.get('quote_text'):
            continue
        conf = e.get('confidence')
        try:
            conf = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            conf = 0.0
        if conf < 0.5:
            continue
        v = e.get('value') or {}
        has_amount = (
            v.get('amount') is not None
            or v.get('min_amount') is not None
            or v.get('max_amount') is not None
        )
        if e.get('fact_type') in ('quoted_price', 'price_range') and not has_amount:
            continue
        e['confidence'] = conf
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


def run_extraction(
    *,
    org,
    corpus: LearningCorpus,
    llm_client,
    model: str = 'gpt-4o-mini',
    limit: Optional[int] = None,
) -> ObservedFactExtractionRun:
    """Full extraction run over a corpus. Persists an
    ObservedFactExtractionRun + per-conversation
    ObservedBusinessFact rows (aggregated later) + any
    OntologyReviewCandidate rows."""
    run = ObservedFactExtractionRun.objects.create(
        org=org,
        corpus=corpus,
        domain=ObservedBusinessFact.Domain.PRICING,
        extractor_version=PRICING_EXTRACTOR_VERSION,
        model=model,
        status=ObservedFactExtractionRun.Status.RUNNING,
        started_at=timezone.now(),
    )
    logger.info(
        f'pricing-extractor: run_id={run.id} started; '
        f'corpus={corpus.id} limit={limit!r}'
    )

    member_qs = (
        LearningCorpusMember.objects
        .filter(corpus=corpus)
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
                'pricing-extractor: conv=%s failed: %s', conv.id, exc,
            )
            continue
        per_conv_extractions.append(extraction)
        total_in += extraction.llm_input_tokens
        total_out += extraction.llm_output_tokens
        total_cost += extraction.llm_cost_usd
        processed += 1
        # Persist ontology-review candidates immediately (small enough).
        for rev in extraction.ontology_review:
            OntologyReviewCandidate.objects.create(
                org=org,
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

    # Aggregate all per-conversation prices into ObservedBusinessFact
    # rows keyed by canonical subject_key.
    from apps.conversations.observed_config.pricing.aggregator import (
        aggregate_and_persist,
    )
    facts_emitted = aggregate_and_persist(
        run=run, per_conv_extractions=per_conv_extractions,
    )

    run.status = ObservedFactExtractionRun.Status.COMPLETED
    run.completed_at = timezone.now()
    run.conversations_processed = processed
    run.facts_emitted = facts_emitted
    run.ontology_review_candidates_emitted = reviews_persisted
    run.llm_input_tokens = total_in
    run.llm_output_tokens = total_out
    run.llm_cost_usd = total_cost
    run.stats_json = {
        'per_conversation_avg_prices': (
            sum(len(x.prices) for x in per_conv_extractions)
            / max(processed, 1)
        ),
    }
    run.save()
    logger.info(
        f'pricing-extractor: run_id={run.id} completed; '
        f'processed={processed} facts={facts_emitted} '
        f'reviews={reviews_persisted} cost=${total_cost}'
    )
    return run
