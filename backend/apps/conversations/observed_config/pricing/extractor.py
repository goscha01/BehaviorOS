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
from apps.conversations.context import (
    Attr,
    Authority,
    Observation,
    resolve_conversation_context,
)
from apps.conversations.context.lb_client import (
    LeadBridgeContextClient,
)
from apps.conversations.semantic.preprocessing import (
    ConversationChunk,
    load_and_normalize, render_turns_for_prompt,
)

# v3: pricing extractor takes the WHOLE conversation in one call so
# dimension mentions in any turn are visible when a price quote is
# resolved. Cap at a soft character budget to prevent runaway costs
# on the rare marathon conversation — beyond the cap we take the
# head + tail. That still preserves both the initial qualification
# (customer stating bedrooms/sqft) and the tail booking (price quote).
PRICING_WHOLE_CONVO_CHAR_BUDGET = 30_000
PRICING_TAIL_TURNS_ON_TRUNCATE = 80

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

    # v3: single whole-conversation call so the LLM can resolve
    # dimensions stated in early turns against price quotes in
    # later turns. Beyond the soft cap, take head + tail so we
    # still preserve both the qualification setup and the pricing
    # neighborhood without paying for a marathon transcript.
    total_chars = sum(len(t.text) for t in turns)
    if total_chars <= PRICING_WHOLE_CONVO_CHAR_BUDGET:
        included_turns = list(turns)
        truncated = False
    else:
        tail = PRICING_TAIL_TURNS_ON_TRUNCATE
        included_turns = list(turns[:tail]) + list(turns[-tail:])
        truncated = True
        logger.info(
            'pricing extractor: conv=%s truncated to head+tail '
            '(orig turns=%d chars=%d)',
            conv.id, len(turns), total_chars,
        )

    chunk = ConversationChunk(
        turns=included_turns, chunk_index=0, is_only_chunk=True,
    )
    rendered = render_turns_for_prompt(chunk)
    user = build_user_prompt(rendered, str(conv.id))
    r = llm_client.analyze(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user,
        model=model,
        # v3: bumped from 1200 → 2000 because resolved_context adds
        # ~4 fields × ~3 keys per price entry vs v2.
        max_tokens=2000,
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
    aggregated_prices = (
        _validate_prices(prices) if isinstance(prices, list) else []
    )
    aggregated_review = (
        _validate_review(review) if isinstance(review, list) else []
    )
    if truncated:
        for p in aggregated_prices:
            p.setdefault('_truncated', True)

    # 2026-08-22 v5: enrichment goes through the canonical context
    # resolver (apps.conversations.context). The resolver:
    #   1. Fetches semantic attributes from LB via the canonical
    #      /api/v1/learning/leads/context endpoint (LB is
    #      authoritative for Thumbtack/Yelp payload parsing —
    #      BehaviorOS never re-parses provider payloads).
    #   2. Accepts our per-price P3 resolved_context as
    #      conversation observations so both sources feed the
    #      precedence engine.
    #   3. Applies source/time/authority-aware precedence,
    #      preserves conflicts, and produces one canonical
    #      attribute set per conversation.
    #
    # We then backfill each price's subject_key from the canonical
    # attributes for any dimension the LLM did not resolve —
    # preserves the "existing LLM-resolved value wins over
    # canonical" behavior when a price is quote-specific
    # ("for 4BR that would be…" different from lead's 3BR default).
    canonical_ctx = _resolve_canonical_context(conv, aggregated_prices)
    if canonical_ctx is not None:
        for p in aggregated_prices:
            subj = p.setdefault('subject_key', {})
            for attr_name, canon_key in _PRICE_TO_CANONICAL:
                if subj.get(canon_key) in (None, ''):
                    val = canonical_ctx.get(attr_name)
                    if val is not None:
                        subj[canon_key] = val
            # Provenance flag for debug views.
            p.setdefault('_canonical_context_used', True)

    return PerConversationExtraction(
        conversation_id=str(conv.id),
        prices=aggregated_prices,
        ontology_review=aggregated_review,
        llm_input_tokens=getattr(r, 'input_tokens', 0),
        llm_output_tokens=getattr(r, 'output_tokens', 0),
        llm_cost_usd=getattr(r, 'cost_usd', Decimal('0')),
    )


# Ordered pairs of (canonical Attr name, subject_key field name).
# Kept as a constant so both the resolver call-site and any future
# consumer share the exact same enrichment surface.
_PRICE_TO_CANONICAL: tuple[tuple[str, str], ...] = (
    (Attr.BEDROOMS, 'bedrooms'),
    (Attr.BATHROOMS, 'bathrooms'),
    (Attr.SQUARE_FOOTAGE, 'square_footage'),
    (Attr.FREQUENCY, 'frequency'),
    (Attr.SERVICE, 'service'),
    (Attr.SERVICE_TIER, 'service_tier'),
)


def _resolve_canonical_context(conv, aggregated_prices: list[dict]):
    """Build the CanonicalConversationContext for this conversation.

    Feeds two source families into the resolver:
      * LB canonical lead context (via HTTP).
      * Conversation observations derived from each price's P3
        resolved_context (turn_id + source_text).

    Returns None on any unrecoverable failure so the extractor can
    still ship prices with LLM-only dimensions.
    """
    from apps.conversations.models import (
        TenantConfigSnapshot as _TCS,
    )
    try:
        snap = (
            _TCS.objects
            .filter(org=conv.org, source_system='leadbridge')
            .order_by('-created_at').first()
        )
        lb_user_id = snap.tenant_external_id if snap else None
    except Exception:  # noqa: BLE001
        lb_user_id = None

    lb_client = LeadBridgeContextClient(lb_user_id=lb_user_id) if lb_user_id else None
    if lb_client is not None and not lb_client.configured:
        lb_client = None

    conv_observations = _observations_from_prices(conv, aggregated_prices)

    try:
        return resolve_conversation_context(
            conv,
            conversation_observations=conv_observations,
            lb_context_client=lb_client,
            lb_user_id=lb_user_id,
            use_cache=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            'canonical context resolution failed for conv=%s: %s '
            '— continuing with LLM-only dimensions',
            conv.id, exc,
        )
        return None


def _observations_from_prices(conv, prices: list[dict]) -> list[Observation]:
    """Convert each price's P3 resolved_context sidecar into
    CONVERSATION_LLM observations for the precedence engine.

    P3 shape (per prompt.py):
        resolved_context: {
          "bedrooms": {"value": 3, "source_turn_id": "t...", "source_text": "..."},
          "bathrooms": {"value": 2, ...},
          "square_footage": {"value": 1800, ...},
          "frequency": {"value": "monthly", ...},
          "service": {"value": "cleaning", ...},
          "service_tier": {"value": "regular", ...},
          "addons": {"value": [...], "source_turn_ids": [...], ...}
        }

    A null / missing key means "unknown" — no observation emitted for
    that attribute.
    """
    from datetime import datetime as _dt, timezone as _tz
    conv_started = getattr(conv, 'started_at', None) or _dt.now(_tz.utc)
    obs: list[Observation] = []
    for price in prices:
        rc = price.get('resolved_context') or {}
        if not isinstance(rc, dict):
            continue
        for attr_name, canon_key in _PRICE_TO_CANONICAL:
            entry = rc.get(canon_key)
            if not isinstance(entry, dict):
                continue
            value = entry.get('value')
            if value is None or value == '':
                continue
            turn_id = entry.get('source_turn_id') or 'unknown'
            source_text = entry.get('source_text')
            obs.append(Observation(
                attribute=attr_name,
                value=value,
                source='conversation',
                source_field=f'price_resolved_context:{turn_id}',
                observed_at=conv_started,
                authority=Authority.CONVERSATION_LLM,
                text=(str(source_text)[:400] if source_text else None),
                source_version=f'pricing-extractor:{PRICING_EXTRACTOR_VERSION}',
            ))
    return obs


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


def create_or_reuse_run(
    *,
    org,
    corpus: LearningCorpus,
    model: str = 'gpt-4o-mini',
) -> tuple[ObservedFactExtractionRun, bool]:
    """Create a PENDING extraction run OR return an existing
    non-terminal run for the same (org, corpus, domain,
    extractor_version). Returns (run, created_bool).

    Trigger-endpoint side of the async contract: enqueue-time
    idempotency. If the caller retries the POST while a run is still
    queued/running, they get the SAME run back.
    """
    non_terminal = ObservedFactExtractionRun.objects.filter(
        org=org, corpus=corpus,
        domain=ObservedBusinessFact.Domain.PRICING,
        extractor_version=PRICING_EXTRACTOR_VERSION,
        status__in=[
            ObservedFactExtractionRun.Status.PENDING,
            ObservedFactExtractionRun.Status.RUNNING,
        ],
    ).order_by('-created_at').first()
    if non_terminal is not None:
        return (non_terminal, False)
    run = ObservedFactExtractionRun.objects.create(
        org=org, corpus=corpus,
        domain=ObservedBusinessFact.Domain.PRICING,
        extractor_version=PRICING_EXTRACTOR_VERSION,
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
    """Execute the extraction for an already-created run row.
    Transitions PENDING -> RUNNING -> COMPLETED (or FAILED on raise).
    Safe against Celery retries: if the run is already terminal,
    no-op returns the row unchanged.
    """
    if run.status in (
        ObservedFactExtractionRun.Status.COMPLETED,
        ObservedFactExtractionRun.Status.FAILED,
    ):
        return run
    run.status = ObservedFactExtractionRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=['status', 'started_at'])
    logger.info(
        f'pricing-extractor: run_id={run.id} started; '
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
                'pricing-extractor: conv=%s failed: %s', conv.id, exc,
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


def run_extraction(
    *,
    org,
    corpus: LearningCorpus,
    llm_client,
    model: str = 'gpt-4o-mini',
    limit: Optional[int] = None,
) -> ObservedFactExtractionRun:
    """Synchronous full extraction. Kept for the management command +
    for tests. HTTP endpoints use the async create+enqueue path via
    create_or_reuse_run + observed_pricing_extraction_task."""
    run, _ = create_or_reuse_run(org=org, corpus=corpus, model=model)
    return run_extraction_for_existing(
        run=run, llm_client=llm_client, model=model, limit=limit,
    )
