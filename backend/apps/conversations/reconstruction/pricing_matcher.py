"""Deterministic pricing matcher — CONFIG-ANCHORED (2026-08-21 v2).

Correction on 2026-08-21 (reviewer): the LB configured pricing table
is the ontology. Every observed quote is a candidate observation OF
a specific cell in that table. The matcher iterates configured
cells first, not observed subjects, and asks per cell:

  "Which observed quotes could belong here, and do they agree with
   the price this cell has configured?"

Per cell we split compatible observed samples into:

  UNIQUE_HERE   — the sample's declared dimensions rule out every
                  other cell, so this cell is the only place the
                  quote could live. This is the primary evidence
                  for the cell's verdict.

  PARTIAL_HERE  — the sample is compatible with this cell AND with
                  ≥ 1 other cell. Partial evidence: contributes to
                  INSUFFICIENT_CONTEXT_TO_COMPARE narrative but
                  never elevates a cell to MATCH or DIFFERS on its
                  own.

Cell verdicts:

  MATCH                              ≥ MIN_UNIQUE unique samples
                                     whose median is within ±tolerance
                                     of cell.amount

  DIFFERS_FROM_CONFIG                ≥ MIN_UNIQUE unique samples whose
                                     median falls outside ±tolerance

  VARIABLE_CONTEXT_DEPENDENT         ≥ MIN_UNIQUE unique samples but
                                     their IQR/median >= threshold
                                     (a single price can't capture
                                     the variance the team quotes)

  INSUFFICIENT_CONTEXT_TO_COMPARE    < MIN_UNIQUE unique samples but
                                     partial evidence exists

  CONFIGURED_NOT_OBSERVED            no compatible observed samples
                                     (neither unique nor partial)

Residual observed subjects — quotes that have NO compatible cell in
the entire configured table (e.g. observed "$15 discount_price oven
cleaning" while LB has only "$35 addon_flat oven") — are collected
into a residual bucket and surfaced as OBSERVED_NOT_CONFIGURED for
the operator's "doesn't fit your setup" review.

No LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from apps.conversations.models import (
    ConfiguredBusinessFact, ObservedBusinessFact,
    ReconstructedBusinessFact,
)


logger = logging.getLogger(__name__)


# ─── Tunables ───────────────────────────────────────────────────────

# MATCH tolerance for MATCH / DIFFERS_FROM_CONFIG on the median of
# uniquely-attributed observed samples for a cell.
PRICE_TOLERANCE_PCT = 0.10
PRICE_TOLERANCE_FLOOR = 5.0

# IQR/median threshold above which the observed distribution is
# considered too heterogeneous to compare deterministically.
VARIABLE_IQR_SHARE_THRESHOLD = 0.25

# Minimum unique observations to promote a cell out of
# INSUFFICIENT_CONTEXT_TO_COMPARE into a hard MATCH / DIFFERS /
# VARIABLE verdict.
MIN_UNIQUE_SAMPLES_FOR_VERDICT = 3


# ─── Public dataclasses ────────────────────────────────────────────

@dataclass
class ObservedSample:
    """Single per-quote observation with its declared dimensions.

    Built from ObservedBusinessFact.value_json.dimension_samples[] +
    the parent ObservedBusinessFact.subject_key_json (as fallback
    when the per-quote sample didn't restate a dim the aggregate
    subject already carries).
    """
    amount: float | None
    service: str | None
    service_tier: str | None
    bedrooms: int | None
    bathrooms: int | None
    square_footage: int | None
    frequency: str | None
    pricing_basis: str | None
    addons: list[str]
    conversation_id: str | None
    turn_id: str | None
    observed_fact_id: str
    subject_hash: str

    def stated_dimensions(self) -> set[str]:
        """Dimension keys the sample explicitly declares."""
        out = set()
        for k in ('service', 'service_tier', 'bedrooms', 'bathrooms',
                  'square_footage', 'frequency', 'pricing_basis'):
            if getattr(self, k) not in (None, ''):
                out.add(k)
        if self.addons:
            out.add('addons')
        return out


@dataclass
class CellVerdict:
    """Per-configured-cell reconstruction output."""
    cell: ConfiguredBusinessFact
    verdict: str  # ReconstructedBusinessFact.RelationshipToConfig value
    consistency: str
    rationale: str
    unique_samples: list[ObservedSample] = field(default_factory=list)
    partial_samples: list[ObservedSample] = field(default_factory=list)
    price_comparison: dict | None = None
    contributing_observed_fact_ids: list[str] = field(default_factory=list)


@dataclass
class OrphanedObservedSubject:
    """An observed pricing subject whose quotes couldn't be attributed
    to any compatible configured cell. Surfaced as
    OBSERVED_NOT_CONFIGURED for the operator to decide whether to add
    a configured rule."""
    observed_fact: ObservedBusinessFact
    reason: str


@dataclass
class MatchInputs:
    observed_facts: list[ObservedBusinessFact]
    configured_facts: list[ConfiguredBusinessFact]


# ─── Public entry point ────────────────────────────────────────────

def match_by_cell(inputs: MatchInputs) -> tuple[list[CellVerdict], list[OrphanedObservedSubject]]:
    """For each configured pricing cell, decide MATCH / DIFFERS /
    INSUFFICIENT / VARIABLE / CONFIGURED_NOT_OBSERVED. Return per-cell
    verdicts + a residual list of observed subjects that couldn't be
    attributed to any cell.
    """
    # Flatten observed facts into per-quote samples.
    samples = _flatten_samples(inputs.observed_facts)
    logger.info(
        'pricing matcher (config-anchored): %d configured cells × '
        '%d observed samples',
        len(inputs.configured_facts), len(samples),
    )

    # Pre-compute compatibility: for each sample, the list of cells it
    # could belong to. This drives both cell verdicts (per cell, the
    # samples where it appears) and orphan detection (samples that
    # appear in ZERO cell lists).
    compat_map: dict[str, list[str]] = {}  # sample_id -> [cell_id]
    for s in samples:
        sample_key = _sample_key(s)
        compat_map[sample_key] = []
    for cell in inputs.configured_facts:
        for s in samples:
            if _sample_compatible_with_cell(s, cell):
                compat_map[_sample_key(s)].append(str(cell.id))

    # Build cell verdicts.
    verdicts: list[CellVerdict] = []
    for cell in inputs.configured_facts:
        unique: list[ObservedSample] = []
        partial: list[ObservedSample] = []
        for s in samples:
            candidates = compat_map[_sample_key(s)]
            if str(cell.id) not in candidates:
                continue
            if len(candidates) == 1:
                unique.append(s)
            else:
                partial.append(s)
        verdicts.append(_verdict_for_cell(cell, unique, partial))

    # Residual: observed subjects where NO sample fit any cell.
    orphans: list[OrphanedObservedSubject] = []
    seen_observed_ids: set[str] = set()
    for f in inputs.observed_facts:
        f_samples = [s for s in samples if s.observed_fact_id == str(f.id)]
        if not f_samples:
            continue
        # A subject is orphaned when EVERY one of its samples has no
        # compatible cell. If even one sample landed somewhere, the
        # subject is represented via the cell verdicts and we don't
        # emit it again.
        has_any_placement = any(
            compat_map[_sample_key(s)] for s in f_samples
        )
        if has_any_placement:
            continue
        if str(f.id) in seen_observed_ids:
            continue
        seen_observed_ids.add(str(f.id))
        orphans.append(OrphanedObservedSubject(
            observed_fact=f,
            reason=_orphan_reason(f, inputs.configured_facts),
        ))

    return verdicts, orphans


# ─── Verdict computation for one cell ──────────────────────────────

def _verdict_for_cell(
    cell: ConfiguredBusinessFact,
    unique: list[ObservedSample],
    partial: list[ObservedSample],
) -> CellVerdict:
    RTC = ReconstructedBusinessFact.RelationshipToConfig
    Con = ReconstructedBusinessFact.Consistency
    cell_amount = _configured_amount(cell.value_json or {})

    unique_amounts = [s.amount for s in unique if s.amount is not None]
    partial_amounts = [s.amount for s in partial if s.amount is not None]
    contributing = sorted({s.observed_fact_id for s in (unique + partial)})

    if not unique and not partial:
        return CellVerdict(
            cell=cell,
            verdict=RTC.CONFIGURED_NOT_OBSERVED,
            consistency=Con.UNDETERMINED,
            rationale=(
                'no observed pricing quote is compatible with this '
                'configured cell'
            ),
            unique_samples=[], partial_samples=[],
            price_comparison=None,
            contributing_observed_fact_ids=[],
        )

    # Not enough UNIQUE evidence for a hard verdict.
    if len(unique_amounts) < MIN_UNIQUE_SAMPLES_FOR_VERDICT:
        partial_desc = ''
        if partial_amounts:
            partial_med = _median(partial_amounts)
            partial_desc = (
                f' (backed by {len(partial_amounts)} partial-evidence '
                f'quote{"s" if len(partial_amounts) != 1 else ""} '
                f'worth median ${partial_med:.2f})'
            )
        return CellVerdict(
            cell=cell,
            verdict=RTC.INSUFFICIENT_CONTEXT_TO_COMPARE,
            consistency=Con.UNDETERMINED,
            rationale=(
                f'{len(unique_amounts)} uniquely-attributed observed '
                f'quote{"s" if len(unique_amounts) != 1 else ""} '
                f'(need >= {MIN_UNIQUE_SAMPLES_FOR_VERDICT}){partial_desc}'
            ),
            unique_samples=unique, partial_samples=partial,
            price_comparison=(
                _price_comparison(unique_amounts, cell_amount)
                if unique_amounts else None
            ),
            contributing_observed_fact_ids=contributing,
        )

    # Enough unique evidence — check price alignment.
    unique_median = _median(unique_amounts)
    consistency = _consistency_from_distribution(unique_amounts)
    if cell_amount is None:
        return CellVerdict(
            cell=cell,
            verdict=RTC.INSUFFICIENT_CONTEXT_TO_COMPARE,
            consistency=consistency,
            rationale=(
                f'{len(unique_amounts)} unique observed quotes '
                f'(median ${unique_median:.2f}) but configured cell '
                f'carries no comparable amount'
            ),
            unique_samples=unique, partial_samples=partial,
            price_comparison=None,
            contributing_observed_fact_ids=contributing,
        )

    if consistency == ReconstructedBusinessFact.Consistency.CONTEXT_DEPENDENT:
        return CellVerdict(
            cell=cell,
            verdict=RTC.VARIABLE_CONTEXT_DEPENDENT,
            consistency=consistency,
            rationale=(
                f'{len(unique_amounts)} unique observed quotes span '
                f'IQR/median >= {VARIABLE_IQR_SHARE_THRESHOLD:.0%}; '
                f'single configured amount ${cell_amount:.2f} cannot '
                'capture this variance'
            ),
            unique_samples=unique, partial_samples=partial,
            price_comparison=_price_comparison(unique_amounts, cell_amount),
            contributing_observed_fact_ids=contributing,
        )

    comparison = _price_comparison(unique_amounts, cell_amount)
    if comparison['within_tolerance']:
        return CellVerdict(
            cell=cell,
            verdict=RTC.MATCH,
            consistency=consistency,
            rationale=(
                f'{len(unique_amounts)} unique observed quotes with '
                f'median ${comparison["observed_median"]:.2f} vs '
                f'configured ${comparison["configured"]:.2f} '
                f'(delta {comparison["delta_pct"]:+.1%}, within '
                f'±{PRICE_TOLERANCE_PCT:.0%})'
            ),
            unique_samples=unique, partial_samples=partial,
            price_comparison=comparison,
            contributing_observed_fact_ids=contributing,
        )
    return CellVerdict(
        cell=cell,
        verdict=RTC.DIFFERS_FROM_CONFIG,
        consistency=consistency,
        rationale=(
            f'{len(unique_amounts)} unique observed quotes with '
            f'median ${comparison["observed_median"]:.2f} vs '
            f'configured ${comparison["configured"]:.2f} '
            f'(delta {comparison["delta_pct"]:+.1%}, outside '
            f'±{PRICE_TOLERANCE_PCT:.0%})'
        ),
        unique_samples=unique, partial_samples=partial,
        price_comparison=comparison,
        contributing_observed_fact_ids=contributing,
    )


# ─── Compatibility (per-sample × per-cell) ─────────────────────────

def _sample_compatible_with_cell(
    s: ObservedSample, cell: ConfiguredBusinessFact,
) -> bool:
    """A sample is compatible with a cell when every dimension the
    sample DECLARES is consistent with the cell's dimensions.

    - Service must match (both sides always declare it; observed
      side normalizes via observed_config.base.normalize_service).
    - service_tier / bedrooms / bathrooms / frequency / pricing_basis
      must equal cell's value WHEN the sample declares them; silent
      on either side is compatible (the "unknown stays unknown"
      invariant).
    - sqft: interval containment when both sides carry it. A cell
      with no sqft bounds is compatible with any sqft. A sample
      without sqft is compatible with any bounds — but such
      universal samples become partial evidence spread across
      multiple cells and never elevate a cell to MATCH alone.
    - addons: sample.addons must be a superset of cell.addons when
      cell declares addons (i.e. cell requires the addon; sample
      may include additional addons alongside).
    """
    csubj = cell.subject_key_json or {}
    if _norm_scalar(s.service) is None:
        return False
    cell_service = _norm_scalar(csubj.get('service'))
    if cell_service is not None and cell_service != _norm_scalar(s.service):
        return False
    for dim, sample_val in (
        ('service_tier', s.service_tier),
        ('frequency', s.frequency),
        ('pricing_basis', s.pricing_basis),
    ):
        cell_val = _norm_scalar(csubj.get(dim))
        sv = _norm_scalar(sample_val)
        if cell_val is not None and sv is not None and cell_val != sv:
            return False
    for dim, sample_val in (
        ('bedrooms', s.bedrooms),
        ('bathrooms', s.bathrooms),
    ):
        cell_val = _as_int(csubj.get(dim))
        sv = _as_int(sample_val)
        if cell_val is not None and sv is not None and cell_val != sv:
            return False
    # sqft interval containment.
    cell_sqft_min = _as_int(csubj.get('sqft_min'))
    cell_sqft_max = _as_int(csubj.get('sqft_max'))
    s_sqft = _as_int(s.square_footage)
    if (
        cell_sqft_min is not None and cell_sqft_max is not None
        and s_sqft is not None
        and not (cell_sqft_min <= s_sqft <= cell_sqft_max)
    ):
        return False
    # Addons: if cell requires addons, sample must include them.
    cell_addons = _norm_list(csubj.get('addons'))
    sample_addons = _norm_list(s.addons)
    if cell_addons and not set(cell_addons).issubset(set(sample_addons)):
        return False
    return True


# ─── Sample flattening ─────────────────────────────────────────────

def _flatten_samples(
    observed_facts: Iterable[ObservedBusinessFact],
) -> list[ObservedSample]:
    """Turn observed facts into per-quote samples for the matcher.
    Each sample inherits missing dims from its parent aggregate
    subject_key so per-quote raw sqft can coexist with per-subject
    bed/bath."""
    out: list[ObservedSample] = []
    for f in observed_facts:
        subj = f.subject_key_json or {}
        parent_service = subj.get('service')
        parent_tier = subj.get('service_tier')
        parent_bed = subj.get('bedrooms')
        parent_bath = subj.get('bathrooms')
        parent_freq = subj.get('frequency')
        parent_basis = subj.get('pricing_basis')
        parent_addons = subj.get('addons') or []
        for s in (f.value_json or {}).get('dimension_samples') or []:
            out.append(ObservedSample(
                amount=_as_float(s.get('amount')),
                service=s.get('service') or parent_service,
                service_tier=s.get('service_tier') or parent_tier,
                bedrooms=_as_int(s.get('bedrooms') if s.get('bedrooms') is not None else parent_bed),
                bathrooms=_as_int(s.get('bathrooms') if s.get('bathrooms') is not None else parent_bath),
                square_footage=_as_int(s.get('square_footage')),
                frequency=s.get('frequency') or parent_freq,
                pricing_basis=s.get('pricing_basis') or parent_basis,
                addons=list(s.get('addons') or parent_addons),
                conversation_id=s.get('conversation_id'),
                turn_id=s.get('turn_id'),
                observed_fact_id=str(f.id),
                subject_hash=f.subject_key_hash,
            ))
    return out


def _sample_key(s: ObservedSample) -> str:
    """Opaque key for compat_map. Uses observed_fact_id +
    conversation_id + turn_id + amount so two identical (amount,
    dims) samples from different quotes stay distinct."""
    return (
        f'{s.observed_fact_id}::{s.conversation_id or ""}::'
        f'{s.turn_id or ""}::{s.amount}'
    )


# ─── Orphan detection ──────────────────────────────────────────────

def _orphan_reason(
    f: ObservedBusinessFact, configured: list[ConfiguredBusinessFact],
) -> str:
    """Human-readable explanation for why an observed subject has no
    compatible configured cell. Used in the OBSERVED_NOT_CONFIGURED
    rationale."""
    subj = f.subject_key_json or {}
    service = _norm_scalar(subj.get('service'))
    same_service = [
        c for c in configured
        if _norm_scalar((c.subject_key_json or {}).get('service')) == service
    ]
    if not same_service:
        return (
            f'no configured pricing rule for service={service!r}; '
            'observed subject falls entirely outside the LB pricing '
            'table'
        )
    # There ARE rules for this service — the incompatibility must be
    # on another dimension.
    basis = _norm_scalar(subj.get('pricing_basis'))
    same_basis = [
        c for c in same_service
        if _norm_scalar((c.subject_key_json or {}).get('pricing_basis')) == basis
    ]
    if not same_basis:
        return (
            f'observed pricing_basis={basis!r} has no compatible '
            f'configured rule under service={service!r}; the LB '
            'table uses other bases for this service'
        )
    return (
        f'observed context {_describe_observed_subject(subj)} '
        f'is not compatible with any of the {len(same_service)} '
        f'configured rules under service={service!r}'
    )


# ─── Number crunching ──────────────────────────────────────────────

def _price_comparison(unique_amounts: list[float], cell_amount: float) -> dict:
    median = _median(unique_amounts)
    delta = median - cell_amount
    delta_pct = delta / cell_amount if cell_amount else 0.0
    tolerance = max(cell_amount * PRICE_TOLERANCE_PCT, PRICE_TOLERANCE_FLOOR)
    return {
        'observed_median': float(median),
        'configured': float(cell_amount),
        'delta': float(delta),
        'delta_pct': float(delta_pct),
        'tolerance': float(tolerance),
        'within_tolerance': abs(delta) <= tolerance,
        'sample_n': len(unique_amounts),
    }


def _consistency_from_distribution(amounts: list[float]) -> str:
    if not amounts:
        return ReconstructedBusinessFact.Consistency.UNDETERMINED
    med = _median(amounts)
    if med <= 0 or len(amounts) < 3:
        return ReconstructedBusinessFact.Consistency.CONSISTENT
    p25, p75 = _percentile(amounts, 25), _percentile(amounts, 75)
    iqr_share = (p75 - p25) / med if med > 0 else 0.0
    if iqr_share >= VARIABLE_IQR_SHARE_THRESHOLD:
        return ReconstructedBusinessFact.Consistency.CONTEXT_DEPENDENT
    return ReconstructedBusinessFact.Consistency.CONSISTENT


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _configured_amount(value_json: dict) -> float | None:
    if not isinstance(value_json, dict):
        return None
    for k in ('amount', 'median', 'value', 'price'):
        v = _as_float(value_json.get(k))
        if v is not None:
            return v
    lo = _as_float(value_json.get('min_amount'))
    hi = _as_float(value_json.get('max_amount'))
    if lo is not None and hi is not None:
        return (lo + hi) / 2
    return None


# ─── Rendering helpers ─────────────────────────────────────────────

def _describe_observed_subject(subj: dict) -> str:
    parts = []
    for k in ('service', 'service_tier', 'bedrooms', 'bathrooms',
              'frequency', 'pricing_basis'):
        v = subj.get(k)
        if v not in (None, '', []):
            parts.append(f'{k}={v}')
    addons = subj.get('addons')
    if addons:
        parts.append(f'addons={sorted(addons)}')
    return '{' + ', '.join(parts) + '}'


# ─── Coercion helpers ──────────────────────────────────────────────

def _norm_scalar(v):
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        return s or None
    if isinstance(v, (int, float, bool)):
        return v
    return v


def _norm_list(v):
    if not isinstance(v, list):
        return []
    return sorted([str(x).strip().lower() for x in v if x is not None])


def _as_int(v):
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── Legacy alias so any importer still using match_all keeps building ──

def match_all(inputs: MatchInputs):
    """Deprecated shim. The old observed-primary API returned
    (per-observed outcomes, orphaned configured). Callers should
    migrate to match_by_cell.
    """
    raise NotImplementedError(
        'pricing_matcher.match_all() was replaced by match_by_cell() '
        'on 2026-08-21. See module docstring.'
    )
