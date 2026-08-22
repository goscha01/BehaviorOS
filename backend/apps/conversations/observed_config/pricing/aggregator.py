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
    canonical_subject_key, normalize_frequency, normalize_service,
)


PRICING_KEY_DIMENSIONS = (
    'service', 'service_tier', 'bedrooms', 'bathrooms',
    'square_footage_bucket', 'frequency', 'addons',
    'pricing_basis',
)

# v3: dimensions that MAY be resolved per quote but do NOT participate
# in the subject_key hash (raw sqft is fine-grained; the matcher will
# do interval containment against the configured sqft_min/sqft_max
# instead of hashing on a bucket). Preserved on value_json.dimension_samples[].
PRICING_PER_QUOTE_DIMENSIONS = ('square_footage',)

# Descriptive-only fields — retained on the fact for context but NOT
# used for the diff join. They flow into value_json.observed_attributes.
PRICING_OBSERVED_ATTRIBUTES = (
    'duration_hours', 'cleaner_count', 'total_computed_amount',
)


def aggregate_and_persist(*, run, per_conv_extractions) -> int:
    """Bucket per-conversation entries by (fact_type, subject_key),
    compute distribution stats + evidence, persist as
    ObservedBusinessFact rows. Returns the number of rows written.

    v2 support-invariant enforcement:
      - Dedup by (fact_type, subject_key_hash, conversation_id,
        quote_turn_id) so the LLM emitting the same fact twice from
        the same turn does not double-count.
      - Per-bucket, aggregate ONE amount per conversation (mean of
        that conversation's amounts for this subject_key). Prevents
        a chatty conversation with 5 restatements of "$249" from
        biasing the distribution.
      - support_n = number of DISTINCT conversations contributing.
    """
    buckets: dict[tuple[str, str], _Bucket] = {}
    conv_meta: dict[str, dict] = {}
    seen_dedup_keys: set[tuple[str, str, str, str]] = set()

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
            quote_turn_id = (
                (entry.get('evidence') or {}).get('quote_turn_id', '')
            )
            dedup_key = (
                fact_type, sha, extraction.conversation_id,
                quote_turn_id or '',
            )
            if dedup_key in seen_dedup_keys:
                continue
            seen_dedup_keys.add(dedup_key)
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

        # update_or_create makes the aggregator idempotent against
        # Celery task retries. Uniqueness is on (extraction_run,
        # domain, fact_type, subject_key_hash) — same run + same
        # subject_key = same row.
        ObservedBusinessFact.objects.update_or_create(
            extraction_run=run,
            domain=ObservedBusinessFact.Domain.PRICING,
            fact_type=b.fact_type,
            subject_key_hash=b.subject_key_hash,
            defaults={
                'org': run.org,
                'corpus': run.corpus,
                'subject_key_json': b.subject_key,
                'subject_key_dimensions': b.subject_key_dimensions,
                'value_json': value_json,
                'support_n': len(b.conversation_ids),
                'aggregate_confidence': (
                    sum(b.confidences) / len(b.confidences)
                    if b.confidences else 0.0
                ),
                'evidence_conversation_ids': (
                    list(b.conversation_ids)[:20]
                ),
                'evidence_turn_ids': list(b.turn_evidence)[:20],
                'first_seen_at': first_seen,
                'last_seen_at': last_seen,
            },
        )
        written += 1
    return written


def _normalize_subject_key(key: dict) -> dict:
    """Coerce values from the LLM into canonical types; drop nulls.

    v3: raw `square_footage` is EXCLUDED from the subject key so that
    quotes with different sqft still aggregate into the same bucket
    for stats. The raw sqft is captured on each quote sample so the
    matcher can do interval containment against configured sqft
    bounds. `square_footage_bucket` (coarse enum) remains in the
    subject key for aggregation stability across v2 → v3.
    """
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
        elif dim == 'service':
            # Canonicalize to match the configured-side normalizer so
            # `cleaning` on the observed side matches `default-service`
            # or `residential_cleaning` on the configured side.
            normalized = normalize_service(v)
            if normalized:
                out[dim] = normalized
        elif dim == 'frequency':
            # Fold `one-time`/`once`, `every-two-weeks`/`biweekly`,
            # etc. so string compat matches LB frequencyDiscounts keys.
            normalized = normalize_frequency(v)
            if normalized:
                out[dim] = normalized
        else:
            s = str(v).strip().lower()
            if s:
                out[dim] = s
    return out


def _extract_per_quote_dimensions(entry: dict) -> dict:
    """Pull the fine-grained dimensions off ONE price entry for
    matcher use. Returns only keys that the LLM resolved from the
    conversation. Unknown stays unknown (key omitted).

    Currently pulls raw square_footage (integer) and copies the
    bedrooms/bathrooms/frequency/addons already in the subject_key
    for the matcher's convenience.
    """
    out: dict = {}
    subj = entry.get('subject_key') or {}
    ctx = entry.get('resolved_context') or {}
    # Raw sqft is per-quote in v3 and NOT hashed into the subject_key.
    sqft_raw = subj.get('square_footage')
    if sqft_raw is None:
        # Fall back to resolved_context if the LLM only populated it
        # there.
        ctx_sqft = (ctx.get('square_footage') or {})
        if isinstance(ctx_sqft, dict):
            sqft_raw = ctx_sqft.get('value')
    try:
        if sqft_raw is not None:
            out['square_footage'] = int(sqft_raw)
    except (TypeError, ValueError):
        pass
    for k in ('bedrooms', 'bathrooms'):
        v = subj.get(k)
        try:
            if v is not None:
                out[k] = int(v)
        except (TypeError, ValueError):
            pass
    for k in ('service', 'service_tier', 'frequency', 'pricing_basis'):
        v = subj.get(k)
        if v not in (None, ''):
            if k == 'service':
                normalized = normalize_service(v)
                if normalized:
                    out[k] = normalized
            elif k == 'frequency':
                normalized = normalize_frequency(v)
                if normalized:
                    out[k] = normalized
            else:
                out[k] = str(v).strip().lower()
    addons = subj.get('addons')
    if isinstance(addons, list) and addons:
        out['addons'] = sorted([str(a).strip().lower() for a in addons])
    return out


@dataclass
class _Bucket:
    fact_type: str
    subject_key: dict
    subject_key_dimensions: list[str]
    subject_key_hash: str
    conversation_ids: set = None
    turn_evidence: list = None
    min_amounts: list = None
    max_amounts: list = None
    discount_pcts: list = None
    discount_amounts: list = None
    confidences: list = None
    quote_samples: list = None
    # v2: per-conversation amount accumulator. Each conversation
    # contributes ONE representative amount (mean of its raw
    # extractions for this bucket) to the top-level distribution.
    _conv_amounts: dict = None
    # v2: observed_attributes (descriptive-only). Never in subject_key.
    observed_attributes: dict = None
    # v3: per-quote fine-grained dimensions (raw sqft + resolved
    # context) for the deterministic matcher. Not in the subject_key
    # (aggregation stability), retained on value_json so the matcher
    # can evaluate compatibility with configured pricing rules.
    dimension_samples: list = None

    def __post_init__(self):
        self.conversation_ids = set()
        self.turn_evidence = []
        self.min_amounts = []
        self.max_amounts = []
        self.discount_pcts = []
        self.discount_amounts = []
        self.confidences = []
        self.quote_samples = []
        self._conv_amounts = {}
        self.observed_attributes = {}
        self.dimension_samples = []

    @property
    def amounts(self) -> list[float]:
        """Distribution input: ONE amount per contributing conversation
        (mean of that conversation's raw amounts for this bucket)."""
        out = []
        for _cid, vals in self._conv_amounts.items():
            if vals:
                out.append(sum(vals) / len(vals))
        return out

    def add(self, entry: dict, *, conversation_id: str) -> None:
        first_seen = conversation_id not in self.conversation_ids
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

        # Per-conversation aggregation: if this conversation has
        # already contributed an amount to this bucket, average the
        # new amount in rather than adding as an independent
        # observation. Enforces the invariant that support_n =
        # distinct conversations, not extracted price events.
        if amount is not None:
            existing = self._conv_amounts.get(conversation_id)
            if existing is None:
                self._conv_amounts[conversation_id] = [amount]
            else:
                existing.append(amount)
        if min_amt is not None:
            self.min_amounts.append(min_amt)
        if max_amt is not None:
            self.max_amounts.append(max_amt)
        if discount_pct is not None:
            self.discount_pcts.append(discount_pct)
        if discount_amt is not None:
            self.discount_amounts.append(discount_amt)

        # Capture observed_attributes (v2) — descriptive context only.
        oa = entry.get('observed_attributes') or {}
        for k in ('duration_hours', 'cleaner_count',
                   'total_computed_amount'):
            val = oa.get(k)
            if val is not None:
                try:
                    self.observed_attributes.setdefault(k, []).append(
                        float(val)
                    )
                except (TypeError, ValueError):
                    pass

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
                'pricing_basis': (
                    (entry.get('subject_key') or {}).get('pricing_basis')
                ),
            })

        # v3: per-quote fine-grained dimensions for the matcher.
        # Every quote gets a sample, even beyond the 10-sample verbatim
        # cap, so the matcher has full evidence for interval
        # containment against configured sqft bounds.
        per_quote_dims = _extract_per_quote_dimensions(entry)
        if amount is not None or per_quote_dims:
            per_quote_dims['amount'] = amount
            per_quote_dims['conversation_id'] = conversation_id
            per_quote_dims['turn_id'] = ev.get('quote_turn_id', '')
            self.dimension_samples.append(per_quote_dims)

    def compute_value_json(self) -> dict:
        payload: dict = {
            'fact_type': self.fact_type,
            'currency': 'USD',
            'quotes_sample': self.quote_samples,
            # v3: matcher input — every quote's raw dimensions.
            # `square_footage` is the load-bearing signal that
            # decouples observed from LB's band boundaries.
            'dimension_samples': self.dimension_samples,
        }
        amounts = self.amounts
        if amounts:
            payload['amount_stats'] = _describe(amounts)
            payload['amount_stats']['per_conversation_aggregation'] = True
        if self.min_amounts or self.max_amounts:
            if self.min_amounts:
                payload['min_amount_stats'] = _describe(self.min_amounts)
            if self.max_amounts:
                payload['max_amount_stats'] = _describe(self.max_amounts)
        if self.discount_pcts:
            payload['discount_pct_stats'] = _describe(self.discount_pcts)
        if self.discount_amounts:
            payload['discount_amount_stats'] = _describe(self.discount_amounts)
        if self.observed_attributes:
            payload['observed_attributes'] = {
                k: _describe(vals)
                for k, vals in self.observed_attributes.items() if vals
            }
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
