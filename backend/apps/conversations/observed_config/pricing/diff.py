"""Deterministic pricing diff (Pipeline 1D Ship A).

Joins ObservedBusinessFact (pricing) rows against ConfiguredBusinessFact
(pricing) rows via canonical subject_key hash, then classifies each
join into one of the 6 DiffCategory buckets.

Never LLM-driven. Verdict rules:

  Key-dimension gate FIRST:
    * exact_match dims                 -> MATCH / CONFLICT / VARIABLE
    * observed dims are subset of cfg  -> INSUFFICIENT_EVIDENCE
                                            (PARTIAL_KEY_COMPATIBLE)
    * cfg dims are subset of observed  -> INSUFFICIENT_EVIDENCE
                                            (PARTIAL_KEY_COMPATIBLE)
    * disjoint dims                    -> not merged; observed goes to
                                            OBSERVED_NOT_CONFIGURED,
                                            configured to
                                            CONFIGURED_NOT_OBSERVED
    * overlapping                      -> INSUFFICIENT_EVIDENCE

  Value comparison (only when dims exact_match AND support >= floor):
    * observed distribution IQR overlaps configured amount AND
      abs(observed_median - configured_amount) <= drift_pp * config_amount
                                       -> MATCH
    * IQR/median clearly excludes configured amount
                                       -> CONFLICT
    * high dispersion (IQR / median >= variability_threshold)
                                       -> VARIABLE_CONTEXT_DEPENDENT

  Below support floor:
    -> INSUFFICIENT_EVIDENCE
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from apps.conversations.models import (
    ConfiguredBusinessFact, ObservedBusinessFact,
    ConfiguredFactParserRun, ObservedFactExtractionRun,
)
from apps.conversations.observed_config.base import (
    DiffCategory, MergedDiffRow, dimensions_are_compatible,
    merge_diff_rows_bucketed,
)


# Verdict thresholds — v1 defaults. Conservative because we'd rather
# lean INSUFFICIENT than declare a spurious CONFLICT.
DEFAULT_MIN_SUPPORT_FOR_VERDICT = 3
DEFAULT_MATCH_DRIFT_PCT = 0.10   # median within 10% of configured amount
DEFAULT_HIGH_VARIABILITY_IQR_OVER_MEDIAN = 0.25


@dataclass
class PricingDiffConfig:
    min_support_for_verdict: int = DEFAULT_MIN_SUPPORT_FOR_VERDICT
    match_drift_pct: float = DEFAULT_MATCH_DRIFT_PCT
    variability_iqr_over_median: float = (
        DEFAULT_HIGH_VARIABILITY_IQR_OVER_MEDIAN
    )


def build_pricing_diff(
    *,
    extraction_run: ObservedFactExtractionRun,
    parser_run: ConfiguredFactParserRun,
    diff_config: Optional[PricingDiffConfig] = None,
) -> dict:
    """Join + classify. Returns the bucketed dict shape the audit
    endpoint renders — see MergedDiffRow / merge_diff_rows_bucketed.
    """
    cfg = diff_config or PricingDiffConfig()

    observed = list(
        ObservedBusinessFact.objects.filter(
            extraction_run=extraction_run,
            domain=ObservedBusinessFact.Domain.PRICING,
        )
    )
    configured = list(
        ConfiguredBusinessFact.objects.filter(
            parser_run=parser_run,
            domain=ObservedBusinessFact.Domain.PRICING,
        )
    )

    # Group both sides by (fact_type, subject_key_hash) for exact-key
    # joins; also keep them indexed by fact_type for cross-key
    # comparability checks.
    obs_by_key: dict[tuple[str, str], ObservedBusinessFact] = {
        (o.fact_type, o.subject_key_hash): o for o in observed
    }
    cfg_by_key: dict[tuple[str, str], ConfiguredBusinessFact] = {
        (c.fact_type, c.subject_key_hash): c for c in configured
    }
    obs_by_fact_type = {}
    for o in observed:
        obs_by_fact_type.setdefault(o.fact_type, []).append(o)
    cfg_by_fact_type = {}
    for c in configured:
        cfg_by_fact_type.setdefault(c.fact_type, []).append(c)

    rows: list[MergedDiffRow] = []
    matched_obs_ids: set = set()
    matched_cfg_ids: set = set()

    # ---- Exact-key joins first ----
    for (fact_type, sha), obs in obs_by_key.items():
        cfg_hit = cfg_by_key.get((fact_type, sha))
        if cfg_hit is None:
            continue
        rows.append(
            _compare_exact_key(obs, cfg_hit, cfg=cfg),
        )
        matched_obs_ids.add(obs.id)
        matched_cfg_ids.add(cfg_hit.id)

    # ---- Partial-key compatibility for unmatched observations ----
    for obs in observed:
        if obs.id in matched_obs_ids:
            continue
        candidates = _candidate_configured_matches(
            obs, cfg_by_fact_type,
        )
        if not candidates:
            rows.append(_observed_orphan(obs))
            continue
        # Any candidate — emit as INSUFFICIENT_EVIDENCE with candidates
        # attached in the rationale. Keeps the audit honest: we saw
        # something the config MIGHT cover, but the observed key
        # lacks the dimensions needed to pick which configured row
        # applies.
        rows.append(
            _observed_partial_key(obs, candidates),
        )
        # Do NOT mark cfg candidates as consumed — they still get their
        # own CONFIGURED_NOT_OBSERVED row below if no observation
        # unambiguously matched them.

    # ---- Configured entries with no matched observation ----
    for cfg_row in configured:
        if cfg_row.id in matched_cfg_ids:
            continue
        # Was this cfg_row implicated by ANY observed partial-key row?
        # If so, we've already surfaced it in the INSUFFICIENT_EVIDENCE
        # bucket via candidates — don't double-count as
        # CONFIGURED_NOT_OBSERVED. Track this by scanning rows.
        implicated = any(
            _cfg_row_in_candidates(cfg_row, r)
            for r in rows
            if r.verdict == DiffCategory.INSUFFICIENT_EVIDENCE
        )
        if implicated:
            continue
        rows.append(_configured_orphan(cfg_row))

    return merge_diff_rows_bucketed(rows)


# ------------------------------------------------------------------
# Per-row comparators
# ------------------------------------------------------------------


def _compare_exact_key(
    obs: ObservedBusinessFact,
    cfg_row: ConfiguredBusinessFact,
    *, cfg: PricingDiffConfig,
) -> MergedDiffRow:
    """Both sides share the same canonical subject_key. Decide MATCH /
    CONFLICT / VARIABLE_CONTEXT_DEPENDENT / INSUFFICIENT_EVIDENCE."""
    cfg_amount = _configured_amount(cfg_row)
    stats = (obs.value_json or {}).get('amount_stats') or {}
    support = stats.get('support_n') or 0
    median = stats.get('median')
    p25 = stats.get('p25')
    p75 = stats.get('p75')

    if support < cfg.min_support_for_verdict:
        return _row(
            obs=obs, cfg=cfg_row, verdict=DiffCategory.INSUFFICIENT_EVIDENCE,
            rationale=(
                f'exact-key match but observed support n={support} '
                f'< floor {cfg.min_support_for_verdict}'
            ),
        )
    if cfg_amount is None or median is None:
        return _row(
            obs=obs, cfg=cfg_row, verdict=DiffCategory.INSUFFICIENT_EVIDENCE,
            rationale=(
                'exact-key match but configured amount or observed '
                'median is null'
            ),
        )

    # Variability gate first — a wildly-varying observed distribution
    # against a single configured amount is VARIABLE, not MATCH/CONFLICT.
    iqr = None
    if p25 is not None and p75 is not None:
        iqr = p75 - p25
    if (iqr is not None and median > 0
            and iqr / median >= cfg.variability_iqr_over_median):
        return _row(
            obs=obs, cfg=cfg_row,
            verdict=DiffCategory.VARIABLE_CONTEXT_DEPENDENT,
            rationale=(
                f'observed IQR=[{p25:g},{p75:g}] over median={median:g} '
                f'exceeds variability threshold '
                f'{cfg.variability_iqr_over_median:.0%}; configured '
                f'single amount ${cfg_amount:g} cannot capture the '
                f'observed distribution'
            ),
        )

    drift = abs(median - cfg_amount) / cfg_amount if cfg_amount > 0 else 0.0
    within_iqr = (
        p25 is not None and p75 is not None
        and p25 <= cfg_amount <= p75
    )
    if within_iqr and drift <= cfg.match_drift_pct:
        return _row(
            obs=obs, cfg=cfg_row, verdict=DiffCategory.MATCH,
            rationale=(
                f'configured ${cfg_amount:g} within observed IQR '
                f'[{p25:g},{p75:g}] and median-drift={drift:.1%} '
                f'<= {cfg.match_drift_pct:.0%}'
            ),
        )
    return _row(
        obs=obs, cfg=cfg_row, verdict=DiffCategory.CONFLICT,
        rationale=(
            f'configured ${cfg_amount:g} outside observed IQR '
            f'[{p25 or "n/a"},{p75 or "n/a"}] or median-drift='
            f'{drift:.1%} > {cfg.match_drift_pct:.0%} '
            f'(observed median=${median:g}, support n={support})'
        ),
    )


def _observed_orphan(obs: ObservedBusinessFact) -> MergedDiffRow:
    return _row(
        obs=obs, cfg=None,
        verdict=DiffCategory.OBSERVED_NOT_CONFIGURED,
        rationale=(
            f'observed pricing fact with subject_key='
            f'{obs.subject_key_json} has no configured entry with '
            f'compatible dimensions'
        ),
    )


def _observed_partial_key(
    obs: ObservedBusinessFact,
    candidates: list[ConfiguredBusinessFact],
) -> MergedDiffRow:
    rat = (
        f'observed key dims={obs.subject_key_dimensions} is a subset '
        f'of {len(candidates)} configured entries with more '
        f'specific dimensions; cannot determine MATCH/CONFLICT '
        f'without additional attribute evidence. Candidate configured '
        f'entries: '
        + '; '.join(
            f'{c.subject_key_json}={_summarize_cfg_value(c.value_json)}'
            for c in candidates[:5]
        )
    )
    # Attach candidates on the row (via configured_source_pointer for
    # v1 — the merged shape will grow a dedicated field later).
    row = _row(
        obs=obs, cfg=None,
        verdict=DiffCategory.INSUFFICIENT_EVIDENCE,
        rationale=rat,
    )
    row.configured_source_pointer = {
        'partial_key_candidates': [
            {
                'configured_fact_id': str(c.id),
                'subject_key': c.subject_key_json,
                'subject_key_dimensions': c.subject_key_dimensions,
                'value': c.value_json,
                'source_pointer': c.source_pointer,
            }
            for c in candidates
        ],
    }
    return row


def _configured_orphan(cfg_row: ConfiguredBusinessFact) -> MergedDiffRow:
    return _row(
        obs=None, cfg=cfg_row,
        verdict=DiffCategory.CONFIGURED_NOT_OBSERVED,
        rationale=(
            f'configured pricing entry {cfg_row.subject_key_json} '
            f'value={_summarize_cfg_value(cfg_row.value_json)} has no '
            f'observed evidence in the corpus'
        ),
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _candidate_configured_matches(
    obs: ObservedBusinessFact,
    cfg_by_fact_type: dict,
) -> list[ConfiguredBusinessFact]:
    """Configured rows of the same fact_type where the observed key is
    a subset of the configured key. These are partial-key candidates —
    not enough to declare MATCH/CONFLICT but useful context for the
    INSUFFICIENT_EVIDENCE rationale."""
    obs_dims = set(obs.subject_key_dimensions)
    obs_key = obs.subject_key_json
    candidates: list[ConfiguredBusinessFact] = []
    for cfg_row in cfg_by_fact_type.get(obs.fact_type, []):
        cfg_dims = set(cfg_row.subject_key_dimensions)
        rel = dimensions_are_compatible(
            list(obs_dims), list(cfg_dims),
        )
        if rel not in ('observed_subset', 'exact_match'):
            continue
        # Observed dims must AGREE on shared dimensions.
        agrees = all(
            obs_key.get(d) == cfg_row.subject_key_json.get(d)
            for d in obs_dims
        )
        if not agrees:
            continue
        # exact_match already handled by _compare_exact_key. Skip.
        if rel == 'exact_match':
            continue
        candidates.append(cfg_row)
    return candidates


def _cfg_row_in_candidates(
    cfg_row: ConfiguredBusinessFact, row: MergedDiffRow,
) -> bool:
    ptr = row.configured_source_pointer or {}
    ids = {
        c.get('configured_fact_id')
        for c in ptr.get('partial_key_candidates', [])
    }
    return str(cfg_row.id) in ids


def _configured_amount(cfg_row: ConfiguredBusinessFact) -> Optional[float]:
    v = cfg_row.value_json or {}
    a = v.get('amount')
    if a is None:
        # For price_range facts, use midpoint. Diff on ranges is a v2
        # refinement.
        mn = v.get('min_amount')
        mx = v.get('max_amount')
        if mn is not None and mx is not None:
            try:
                return (float(mn) + float(mx)) / 2.0
            except (TypeError, ValueError):
                return None
        return None
    try:
        return float(a)
    except (TypeError, ValueError):
        return None


def _summarize_cfg_value(value_json: dict) -> str:
    v = value_json or {}
    if v.get('amount') is not None:
        return f'${v["amount"]:g}'
    if v.get('min_amount') is not None and v.get('max_amount') is not None:
        return f'${v["min_amount"]:g}-${v["max_amount"]:g}'
    if v.get('discount_pct') is not None:
        return f'{v["discount_pct"]:g}% off'
    return str(v)


def _row(
    *,
    obs: Optional[ObservedBusinessFact],
    cfg: Optional[ConfiguredBusinessFact],
    verdict: str,
    rationale: str,
) -> MergedDiffRow:
    subject_key = (
        (obs and obs.subject_key_json)
        or (cfg and cfg.subject_key_json)
        or {}
    )
    obs_dims = obs.subject_key_dimensions if obs else []
    cfg_dims = cfg.subject_key_dimensions if cfg else []
    rel = dimensions_are_compatible(obs_dims, cfg_dims) if (obs and cfg) else 'n/a'
    return MergedDiffRow(
        domain='pricing',
        fact_type=(obs and obs.fact_type) or (cfg and cfg.fact_type) or '',
        subject_key_json=subject_key,
        observed_dimensions=obs_dims,
        configured_dimensions=cfg_dims,
        key_dimension_relationship=rel,
        verdict=verdict,
        verdict_rationale=rationale,
        observed_value=(obs.value_json if obs else None),
        observed_support_n=(obs.support_n if obs else 0),
        observed_evidence_conversation_ids=(
            list(obs.evidence_conversation_ids)[:10]
            if obs else []
        ),
        observed_evidence_turn_ids=(
            list(obs.evidence_turn_ids)[:10] if obs else []
        ),
        configured_value=(cfg.value_json if cfg else None),
        configured_source_pointer=(cfg.source_pointer if cfg else None),
    )
