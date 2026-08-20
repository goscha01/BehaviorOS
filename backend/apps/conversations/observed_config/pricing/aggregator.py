"""Aggregate per-conversation pricing facts into ObservedBusinessFact
rows keyed by canonical subject_key.

Each unique subject_key gets ONE row. The `value_json` payload for
pricing preserves the full distribution behind the aggregate:

  {
    "fact_type": "quoted_price",
    "amount_stats": {
      "support_n": 17,
      "min": 169.0, "p25": 179.0, "median": 189.0,
      "p75": 199.0, "max": 249.0
    },
    "currency": "USD",
    "quotes_sample": [
      {"amount": 189.0, "conversation_id": "...", "turn_id": "t0044",
       "quote_text": "..."},
      ...  # up to 10 samples
    ]
  }

`price_range` and `discount_offered` fact_types get analogous
distributional payloads.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass

from django.utils import timezone

from apps.conversations.models import (
    Conversation, ObservedBusinessFact,
)
from apps.conversations.observed_config.base import (
    canonical_subject_key,
)


PRICING_KEY_DIMENSIONS = (
    'service', 'bedrooms', 'bathrooms',
    'square_footage_bucket', 'frequency', 'addons',
)


def aggregate_and_persist(*, run, per_conv_extractions) -> int:
    """Bucket per-conversation entries by (fact_type, subject_key),
    compute distribution stats + evidence, persist as
    ObservedBusinessFact rows. Returns the number of rows written.

    Runs inside the extraction run's transaction — callers should
    invoke this after all per-conversation LLM calls have finished.
    """
    # Bucket entries. Key = (fact_type, canonical_key_hash).
    buckets: dict[tuple[str, str], _Bucket] = {}
    conv_meta: dict[str, dict] = {}

    for extraction in per_conv_extractions:
        for entry in extraction.prices:
            fact_type = entry.get('fact_type')
            if fact_type not in ('quoted_price', 'price_range',
                                  'discount_offered'):
                continue
            subject_key = _normalize_subject_key(
                entry.get('subject_key') or {}
            )
            canon, sha, dims = canonical_subject_key(subject_key)
            key = (fact_type, sha)
            b = buckets.get(key)
            if b is None:
                b = _Bucket(
                    fact_type=fact_type,
                    subject_key=subject_key,
                    subject_key_dimensions=dims,
                    subject_key_hash=sha,
                )
                buckets[key] = b
            b.add(entry, conversation_id=extraction.conversation_id)

    # Load conversation started_at for temporal range on each bucket.
    all_conv_ids = {
        cid
        for b in buckets.values()
        for cid in b.conversation_ids
    }
    for c in Conversation.objects.filter(id__in=all_conv_ids).only(
        'id', 'started_at',
    ):
        conv_meta[str(c.id)] = {'started_at': c.started_at}

    written = 0
    for b in buckets.values():
        value_json = b.compute_value_json()
        # Temporal range from conv metadata (approximate — uses conv
        # started_at, not the specific turn timestamp).
        started_ats = [
            conv_meta.get(cid, {}).get('started_at')
            for cid in b.conversation_ids
        ]
        started_ats = [x for x in started_ats if x is not None]
        first_seen = min(started_ats) if started_ats else None
        last_seen = max(started_ats) if started_ats else None

        ObservedBusinessFact.objects.create(
            org=run.org,
            corpus=run.corpus,
            extraction_run=run,
            domain=ObservedBusinessFact.Domain.PRICING,
            fact_type=b.fact_type,
            subject_key_json=b.subject_key,
            subject_key_dimensions=b.subject_key_dimensions,
            subject_key_hash=b.subject_key_hash,
            value_json=value_json,
            support_n=len(b.conversation_ids),
            aggregate_confidence=(
                sum(b.confidences) / len(b.confidences)
                if b.confidences else 0.0
            ),
            evidence_conversation_ids=list(b.conversation_ids)[:20],
            evidence_turn_ids=list(b.turn_evidence)[:20],
            first_seen_at=first_seen,
            last_seen_at=last_seen,
        )
        written += 1
    return written


def _normalize_subject_key(key: dict) -> dict:
    """Coerce values from the LLM into canonical types; drop nulls."""
    out: dict = {}
    for dim in PRICING_KEY_DIMENSIONS:
        v = key.get(dim)
        if v is None:
            continue
        if dim in ('bedrooms', 'bathrooms'):
            try:
                out[dim] = int(v)
            except (TypeError, ValueError):
                continue
        elif dim == 'addons':
            if isinstance(v, list) and v:
                out[dim] = sorted([str(x).strip().lower() for x in v])
        else:
            s = str(v).strip().lower()
            if s:
                out[dim] = s
    return out


@dataclass
class _Bucket:
    fact_type: str
    subject_key: dict
    subject_key_dimensions: list[str]
    subject_key_hash: str
    conversation_ids: set = None
    turn_evidence: list = None
    amounts: list = None
    min_amounts: list = None
    max_amounts: list = None
    discount_pcts: list = None
    discount_amounts: list = None
    confidences: list = None
    quote_samples: list = None

    def __post_init__(self):
        self.conversation_ids = set()
        self.turn_evidence = []
        self.amounts = []
        self.min_amounts = []
        self.max_amounts = []
        self.discount_pcts = []
        self.discount_amounts = []
        self.confidences = []
        self.quote_samples = []

    def add(self, entry: dict, *, conversation_id: str) -> None:
        self.conversation_ids.add(conversation_id)
        ev = entry.get('evidence') or {}
        self.turn_evidence.append({
            'conversation_id': conversation_id,
            'quote_turn_id': ev.get('quote_turn_id', ''),
            'attribute_turn_ids': ev.get('attribute_turn_ids', {}) or {},
        })
        try:
            self.confidences.append(float(entry.get('confidence', 0.0)))
        except (TypeError, ValueError):
            pass
        v = entry.get('value') or {}
        amount = _as_float(v.get('amount'))
        min_amt = _as_float(v.get('min_amount'))
        max_amt = _as_float(v.get('max_amount'))
        discount_pct = _as_float(v.get('discount_pct'))
        discount_amt = _as_float(v.get('discount_amount'))
        if amount is not None:
            self.amounts.append(amount)
        if min_amt is not None:
            self.min_amounts.append(min_amt)
        if max_amt is not None:
            self.max_amounts.append(max_amt)
        if discount_pct is not None:
            self.discount_pcts.append(discount_pct)
        if discount_amt is not None:
            self.discount_amounts.append(discount_amt)
        # Keep a small verbatim sample for the audit report.
        if len(self.quote_samples) < 10:
            self.quote_samples.append({
                'amount': amount, 'min_amount': min_amt,
                'max_amount': max_amt,
                'discount_pct': discount_pct,
                'discount_amount': discount_amt,
                'conversation_id': conversation_id,
                'turn_id': ev.get('quote_turn_id', ''),
                'quote_text': (ev.get('quote_text') or '')[:200],
            })

    def compute_value_json(self) -> dict:
        payload: dict = {
            'fact_type': self.fact_type,
            'currency': 'USD',
            'quotes_sample': self.quote_samples,
        }
        if self.amounts:
            payload['amount_stats'] = _describe(self.amounts)
        if self.min_amounts or self.max_amounts:
            if self.min_amounts:
                payload['min_amount_stats'] = _describe(self.min_amounts)
            if self.max_amounts:
                payload['max_amount_stats'] = _describe(self.max_amounts)
        if self.discount_pcts:
            payload['discount_pct_stats'] = _describe(self.discount_pcts)
        if self.discount_amounts:
            payload['discount_amount_stats'] = _describe(self.discount_amounts)
        return payload


def _describe(values: list[float]) -> dict:
    if not values:
        return {'support_n': 0}
    n = len(values)
    sorted_values = sorted(values)
    return {
        'support_n': n,
        'min': sorted_values[0],
        'p25': _percentile(sorted_values, 25),
        'median': statistics.median(sorted_values),
        'p75': _percentile(sorted_values, 75),
        'max': sorted_values[-1],
        'mean': sum(sorted_values) / n,
    }


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return (
        sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)
    )


def _as_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
