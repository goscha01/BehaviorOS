"""Deterministic pricing matcher (Pipeline 1D, 2026-08-21 refresh).

Replaces the old subject_key_hash lookup in _reconstruct_pricing with
a compatibility-based candidate search:

  1. For each observed pricing fact, find compatible configured
     candidates — same `service`, same `pricing_basis` (when both
     sides declare one), and no dimension the observed side EXPLICITLY
     states that contradicts a value the configured side pins.
  2. Apply the observed side's known dimensions to narrow the
     candidate set.
       - Interval containment for sqft:
           observed.square_footage ∈ [cfg.sqft_min, cfg.sqft_max]
         An observed sqft with no LB-configured bounds is compatible
         with any bounds-less configured rule for the same
         bed/bath/tier.
       - Exact match for bedrooms / bathrooms / service_tier /
         frequency / addons when the observed side states them.
  3. Verdict per the reviewer directive:
       - One rule resolves        → compare price → MATCH / DIFFERS_FROM_CONFIG
       - Multiple candidates left because observed lacks dims
                                  → INSUFFICIENT_CONTEXT_TO_COMPARE
                                    (with the list of missing dims)
       - No compatible candidate  → OBSERVED_NOT_CONFIGURED
       - Observed distribution is materially heterogeneous
                                  → VARIABLE_CONTEXT_DEPENDENT
  4. Configured facts never claimed by any observed fact
       → CONFIGURED_NOT_OBSERVED

Never uses an LLM for the verdict. Price tolerance for MATCH is
±10 % (mirrors the LB pricing engine's round-to-$5 semantics + the
old reconstructor's tolerance so the schema change alone does not
shift MATCH/DIFFERS boundaries).

The matcher consumes ObservedBusinessFact and ConfiguredBusinessFact
rows via the shapes produced by P2 (deterministic config parser) and
P3 (observed extractor v3). It gracefully accepts v2-shaped
observed facts too — sqft is simply treated as missing when the fact
only carries the legacy square_footage_bucket instead of raw sqft.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from apps.conversations.models import (
    ConfiguredBusinessFact, ObservedBusinessFact,
    ReconstructedBusinessFact,
)


logger = logging.getLogger(__name__)


# ─── Tunables ───────────────────────────────────────────────────────

# MATCH tolerance for MATCH/DIFFERS_FROM_CONFIG on a single-candidate
# comparison. ±10% of the configured amount, floored at $5 to allow
# LB's own round-to-$5 recurring discount output to still MATCH.
PRICE_TOLERANCE_PCT = 0.10
PRICE_TOLERANCE_FLOOR = 5.0

# IQR/median threshold above which the observed distribution is
# considered too heterogeneous to compare deterministically.
VARIABLE_IQR_SHARE_THRESHOLD = 0.25

# Minimum support (distinct conversations) to emit a verdict at all;
# below this we emit INSUFFICIENT_EVIDENCE (legacy generic value —
# not a pricing-specific verdict).
MIN_SUPPORT_FOR_VERDICT = 3


# ─── Public types ───────────────────────────────────────────────────

@dataclass
class MatchOutcome:
    """Result of matching one observed fact against the configured set."""
    verdict: str  # ReconstructedBusinessFact.RelationshipToConfig value
    rationale: str
    consistency: str  # ReconstructedBusinessFact.Consistency value
    candidate_configured_fact_ids: list[str]
    matched_configured_fact_id: str | None
    missing_observed_dimensions: list[str]
    price_comparison: dict | None  # {'observed_median':..,'configured':..,'delta_pct':..}


@dataclass
class MatchInputs:
    observed_facts: list[ObservedBusinessFact]
    configured_facts: list[ConfiguredBusinessFact]


# ─── Entry points ───────────────────────────────────────────────────

def match_all(inputs: MatchInputs) -> tuple[list[tuple[ObservedBusinessFact, MatchOutcome]],
                                             list[ConfiguredBusinessFact]]:
    """Run the matcher across every observed fact. Returns:
      - list of (observed_fact, MatchOutcome) pairs
      - list of configured facts that were never claimed by any
        observed fact (→ CONFIGURED_NOT_OBSERVED)
    """
    observed_by_id: dict[str, ObservedBusinessFact] = {
        str(o.id): o for o in inputs.observed_facts
    }
    outcomes: list[tuple[ObservedBusinessFact, MatchOutcome]] = []
    claimed_cfg_ids: set[str] = set()

    for obs in observed_by_id.values():
        outcome = match_one(obs, inputs.configured_facts)
        outcomes.append((obs, outcome))
        for cid in outcome.candidate_configured_fact_ids:
            claimed_cfg_ids.add(cid)

    orphaned = [
        c for c in inputs.configured_facts
        if str(c.id) not in claimed_cfg_ids
    ]
    return outcomes, orphaned


def match_one(
    obs: ObservedBusinessFact,
    configured_facts: Iterable[ConfiguredBusinessFact],
) -> MatchOutcome:
    """Compute a MatchOutcome for one observed pricing fact."""
    obs_subj = obs.subject_key_json or {}
    obs_value = obs.value_json or {}
    support_n = obs.support_n or 0

    # Short-circuit: too little evidence to bother matching.
    if support_n < MIN_SUPPORT_FOR_VERDICT:
        return MatchOutcome(
            verdict=ReconstructedBusinessFact.RelationshipToConfig.INSUFFICIENT_EVIDENCE,
            rationale=(
                f'support_n={support_n} < {MIN_SUPPORT_FOR_VERDICT}; '
                'not enough evidence to emit a pricing verdict'
            ),
            consistency=ReconstructedBusinessFact.Consistency.UNDETERMINED,
            candidate_configured_fact_ids=[],
            matched_configured_fact_id=None,
            missing_observed_dimensions=[],
            price_comparison=None,
        )

    # Step 1: find compatible candidates.
    plausible = _plausible_candidates(obs_subj, configured_facts)

    # Step 2: apply per-quote dimension samples for finer selection.
    # An observed fact aggregates many quotes; each quote may have
    # raw sqft that lets us pick a specific configured row.
    dim_samples = obs_value.get('dimension_samples') or []
    if plausible and dim_samples:
        narrowed = _narrow_by_samples(plausible, dim_samples)
    else:
        narrowed = plausible

    # Step 3: verdict.
    consistency = _consistency_from_iqr(obs_value, support_n)

    if not plausible:
        # No compatible configured rule under ANY subset of observed
        # dims — this is a real "not in the config" case.
        return MatchOutcome(
            verdict=ReconstructedBusinessFact.RelationshipToConfig.OBSERVED_NOT_CONFIGURED,
            rationale=(
                'no configured pricing rule is compatible with the '
                f'observed context: {_describe_observed_context(obs_subj)}'
            ),
            consistency=consistency,
            candidate_configured_fact_ids=[],
            matched_configured_fact_id=None,
            missing_observed_dimensions=[],
            price_comparison=None,
        )

    if len(narrowed) == 1:
        cfg = narrowed[0]
        comparison = _compare_prices(obs_value, cfg)
        if comparison is None:
            # No usable observed median → treat as insufficient
            # context (we have a candidate but nothing to compare).
            return MatchOutcome(
                verdict=ReconstructedBusinessFact.RelationshipToConfig.INSUFFICIENT_CONTEXT_TO_COMPARE,
                rationale=(
                    f'one compatible configured rule (cfg={cfg.id}) '
                    'but observed price distribution has no usable '
                    'median to compare against'
                ),
                consistency=consistency,
                candidate_configured_fact_ids=[str(cfg.id)],
                matched_configured_fact_id=None,
                missing_observed_dimensions=['price_distribution'],
                price_comparison=None,
            )
        if consistency == ReconstructedBusinessFact.Consistency.CONTEXT_DEPENDENT:
            return MatchOutcome(
                verdict=ReconstructedBusinessFact.RelationshipToConfig.VARIABLE_CONTEXT_DEPENDENT,
                rationale=(
                    f'observed distribution IQR/median >= '
                    f'{VARIABLE_IQR_SHARE_THRESHOLD:.0%}; a single '
                    'configured rule cannot capture this variance'
                ),
                consistency=consistency,
                candidate_configured_fact_ids=[str(cfg.id)],
                matched_configured_fact_id=None,
                missing_observed_dimensions=[],
                price_comparison=comparison,
            )
        if comparison['within_tolerance']:
            return MatchOutcome(
                verdict=ReconstructedBusinessFact.RelationshipToConfig.MATCH,
                rationale=(
                    f'observed median ${comparison["observed_median"]:.2f} '
                    f'vs configured ${comparison["configured"]:.2f} '
                    f'(delta {comparison["delta_pct"]:+.1%}, within ±{PRICE_TOLERANCE_PCT:.0%})'
                ),
                consistency=consistency,
                candidate_configured_fact_ids=[str(cfg.id)],
                matched_configured_fact_id=str(cfg.id),
                missing_observed_dimensions=[],
                price_comparison=comparison,
            )
        return MatchOutcome(
            verdict=ReconstructedBusinessFact.RelationshipToConfig.DIFFERS_FROM_CONFIG,
            rationale=(
                f'observed median ${comparison["observed_median"]:.2f} '
                f'vs configured ${comparison["configured"]:.2f} '
                f'(delta {comparison["delta_pct"]:+.1%}, outside ±{PRICE_TOLERANCE_PCT:.0%})'
            ),
            consistency=consistency,
            candidate_configured_fact_ids=[str(cfg.id)],
            matched_configured_fact_id=str(cfg.id),
            missing_observed_dimensions=[],
            price_comparison=comparison,
        )

    # More than one plausible configured candidate — observed
    # context lacks the discriminators to choose. Report which
    # dimensions vary across the candidates.
    missing = _dimensions_varying_across_candidates(narrowed, obs_subj)
    return MatchOutcome(
        verdict=ReconstructedBusinessFact.RelationshipToConfig.INSUFFICIENT_CONTEXT_TO_COMPARE,
        rationale=(
            f'{len(narrowed)} configured rules are plausible for the '
            f'observed context {_describe_observed_context(obs_subj)}; '
            f'missing observed dimensions to choose among them: '
            f'{", ".join(missing) if missing else "(unable to determine)"}'
        ),
        consistency=consistency,
        candidate_configured_fact_ids=[str(c.id) for c in narrowed],
        matched_configured_fact_id=None,
        missing_observed_dimensions=missing,
        price_comparison=None,
    )


# ─── Compatibility ─────────────────────────────────────────────────

# Dimensions on which observed vs configured MUST agree when both
# sides declare them. Any of these being explicitly incompatible
# eliminates a candidate.
COMPAT_DIMENSIONS = (
    'service', 'service_tier', 'bedrooms', 'bathrooms',
    'frequency', 'pricing_basis',
)


def _plausible_candidates(
    obs_subj: dict, configured: Iterable[ConfiguredBusinessFact],
) -> list[ConfiguredBusinessFact]:
    """Filter configured facts to those compatible with the observed
    subject. Compatibility rules:

    - Service: MUST match when both are declared. When observed has
      no service, no candidate is plausible (a matcher without any
      shared discriminator would explode).
    - service_tier, bedrooms, bathrooms, frequency, pricing_basis:
      when observed declares → must equal configured (when
      configured declares). Silent on either side is compatible.
    - sqft interval: handled in _narrow_by_samples (per-quote).
    - addons: when observed lists addons, configured must list a
      compatible superset OR the same set. When observed has no
      addons declared, addon-only configured rules (pricing_basis=
      addon_flat/addon_hourly) are excluded unless the observed
      pricing_basis is also addon_*.
    """
    obs_service = _norm_scalar(obs_subj.get('service'))
    if obs_service is None:
        return []
    out: list[ConfiguredBusinessFact] = []
    for cfg in configured:
        c_subj = cfg.subject_key_json or {}
        cfg_service = _norm_scalar(c_subj.get('service'))
        if cfg_service is not None and cfg_service != obs_service:
            continue
        # Compatibility check for each compat dimension.
        skip = False
        for dim in COMPAT_DIMENSIONS:
            o = _norm_scalar(obs_subj.get(dim))
            c = _norm_scalar(c_subj.get(dim))
            if o is None or c is None:
                continue
            if o != c:
                skip = True
                break
        if skip:
            continue
        # Addon compatibility.
        obs_addons = _norm_list(obs_subj.get('addons'))
        cfg_addons = _norm_list(c_subj.get('addons'))
        if obs_addons and cfg_addons:
            if not set(cfg_addons).issubset(set(obs_addons)):
                continue
        elif not obs_addons and cfg_addons:
            # Observed didn't declare addons but configured is an
            # addon-only rule — only compatible if observed
            # pricing_basis is addon-flavored too.
            obs_basis = _norm_scalar(obs_subj.get('pricing_basis'))
            if obs_basis is None or not obs_basis.startswith('addon_'):
                continue
        out.append(cfg)
    return out


def _narrow_by_samples(
    candidates: list[ConfiguredBusinessFact],
    dim_samples: list[dict],
) -> list[ConfiguredBusinessFact]:
    """Given per-quote dimension samples (each with raw sqft +
    resolved bedrooms/bathrooms), narrow the candidate set by
    interval containment on sqft and equality on the coarse dims.

    A candidate is KEPT if AT LEAST ONE sample is compatible with it
    (i.e. its sqft ∈ [sqft_min, sqft_max] when both sides declare
    them, and its bedrooms / bathrooms equal the candidate's).

    If none of the samples carry any narrowing dimension (e.g. the
    observed extractor could not resolve sqft for any quote), the
    candidate set is returned unchanged and the caller resolves via
    INSUFFICIENT_CONTEXT_TO_COMPARE.
    """
    if not candidates or not dim_samples:
        return candidates
    any_narrowing_signal = any(
        (s.get('square_footage') is not None
         or s.get('bedrooms') is not None
         or s.get('bathrooms') is not None)
        for s in dim_samples
    )
    if not any_narrowing_signal:
        return candidates
    kept: list[ConfiguredBusinessFact] = []
    for cfg in candidates:
        c_subj = cfg.subject_key_json or {}
        c_sqft_min = _as_int(c_subj.get('sqft_min'))
        c_sqft_max = _as_int(c_subj.get('sqft_max'))
        c_bed = _as_int(c_subj.get('bedrooms'))
        c_bath = _as_int(c_subj.get('bathrooms'))
        for s in dim_samples:
            s_sqft = _as_int(s.get('square_footage'))
            s_bed = _as_int(s.get('bedrooms'))
            s_bath = _as_int(s.get('bathrooms'))
            if c_bed is not None and s_bed is not None and c_bed != s_bed:
                continue
            if c_bath is not None and s_bath is not None and c_bath != s_bath:
                continue
            if c_sqft_min is not None and c_sqft_max is not None and s_sqft is not None:
                if not (c_sqft_min <= s_sqft <= c_sqft_max):
                    continue
            kept.append(cfg)
            break
    return kept


def _dimensions_varying_across_candidates(
    candidates: list[ConfiguredBusinessFact], obs_subj: dict,
) -> list[str]:
    """Return the dimension names that (a) are not declared on the
    observed side and (b) vary across the compatible configured
    candidates — the observer needs to state one of them to
    disambiguate."""
    reportable = (
        'service_tier', 'bedrooms', 'bathrooms',
        'sqft_min', 'sqft_max', 'frequency', 'addons',
    )
    missing: list[str] = []
    for dim in reportable:
        if obs_subj.get(dim) not in (None, [], ''):
            continue
        values = set()
        for c in candidates:
            v = (c.subject_key_json or {}).get(dim)
            if v is None:
                continue
            if isinstance(v, list):
                v = tuple(v)
            values.add(v)
        if len(values) > 1:
            # collapse sqft_min/sqft_max into one reported dim
            if dim in ('sqft_min', 'sqft_max'):
                if 'square_footage' not in missing:
                    missing.append('square_footage')
            else:
                missing.append(dim)
    return missing


# ─── Price comparison ──────────────────────────────────────────────

def _compare_prices(
    obs_value: dict, cfg: ConfiguredBusinessFact,
) -> dict | None:
    """Compare the observed aggregate median against the configured
    amount. Returns None when we cannot compute a comparison (missing
    median or missing configured amount)."""
    stats = obs_value.get('amount_stats') or {}
    median = _as_float(stats.get('median'))
    cfg_amount = _configured_amount(cfg.value_json or {})
    if median is None or cfg_amount is None:
        return None
    delta = median - cfg_amount
    delta_pct = delta / cfg_amount if cfg_amount else 0.0
    tolerance = max(cfg_amount * PRICE_TOLERANCE_PCT, PRICE_TOLERANCE_FLOOR)
    return {
        'observed_median': float(median),
        'configured': float(cfg_amount),
        'delta': float(delta),
        'delta_pct': float(delta_pct),
        'tolerance': float(tolerance),
        'within_tolerance': abs(delta) <= tolerance,
    }


def _configured_amount(value_json: dict) -> float | None:
    """Extract a comparable configured amount from a
    ConfiguredBusinessFact.value_json produced by either the
    deterministic parser (P2) or the legacy LLM parser."""
    if not isinstance(value_json, dict):
        return None
    # Deterministic parser (P2) stores post-discount amount here.
    for k in ('amount', 'median', 'value', 'price'):
        v = _as_float(value_json.get(k))
        if v is not None:
            return v
    # LLM parser sometimes emits min_amount + max_amount only.
    lo = _as_float(value_json.get('min_amount'))
    hi = _as_float(value_json.get('max_amount'))
    if lo is not None and hi is not None:
        return (lo + hi) / 2
    return None


def _consistency_from_iqr(obs_value: dict, support_n: int) -> str:
    stats = obs_value.get('amount_stats') or {}
    median = _as_float(stats.get('median'))
    p25 = _as_float(stats.get('p25'))
    p75 = _as_float(stats.get('p75'))
    if median is None or p25 is None or p75 is None or median <= 0:
        return (
            ReconstructedBusinessFact.Consistency.CONSISTENT
            if support_n >= MIN_SUPPORT_FOR_VERDICT
            else ReconstructedBusinessFact.Consistency.UNDETERMINED
        )
    iqr_share = (p75 - p25) / median
    if iqr_share >= VARIABLE_IQR_SHARE_THRESHOLD and support_n >= MIN_SUPPORT_FOR_VERDICT:
        return ReconstructedBusinessFact.Consistency.CONTEXT_DEPENDENT
    return (
        ReconstructedBusinessFact.Consistency.CONSISTENT
        if support_n >= MIN_SUPPORT_FOR_VERDICT
        else ReconstructedBusinessFact.Consistency.UNDETERMINED
    )


def _describe_observed_context(obs_subj: dict) -> str:
    """Compact string for rationale messages."""
    parts = []
    for k in ('service', 'service_tier', 'bedrooms', 'bathrooms',
              'frequency', 'pricing_basis'):
        v = obs_subj.get(k)
        if v not in (None, '', []):
            parts.append(f'{k}={v}')
    addons = obs_subj.get('addons')
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
