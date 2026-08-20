"""Deterministic FAQ diff (Pipeline 1D Ship C).

Joins BUSINESS_FAQ ObservedBusinessFact rows against configured_faq
ConfiguredBusinessFact rows by canonical (topic, intent, other_topic?)
hash.

Verdict rules — kept simple; FAQ diff is more about coverage than
statistical inference:

  MATCH                   → subject_key matches AND observed ask_rate
                             above `min_ask_rate` (default 3%)
                             (i.e. configured Q&A is actually being
                             asked in real conversations)
  OBSERVED_NOT_CONFIGURED → observed BUSINESS_FAQ with meaningful
                             support (>= `min_support`, default 5)
                             has no matching configured entry
                             — a real coverage gap
  CONFIGURED_NOT_OBSERVED → configured FAQ entry with NO observed
                             questions matching it — either evergreen
                             coverage (people rarely ask about it) or
                             the entry is stale
  INSUFFICIENT_EVIDENCE   → observed exists but support below floor,
                             OR configured exists + observed has ≥1
                             but too weak for MATCH
  (No CONFLICT bucket in v1 — comparing agent answers to configured
  answers is a semantic-similarity problem we don't attempt yet;
  a future v2 could add ANSWER_DIVERGENCE.)

Emits an `operational_noise_ratio` overlay in the diff summary so the
acceptance report can answer "how much of QUESTION_FAQ is really
operational?"
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


DEFAULT_MIN_SUPPORT = 5
DEFAULT_MIN_ASK_RATE = 0.03


@dataclass
class FaqDiffConfig:
    min_support: int = DEFAULT_MIN_SUPPORT
    min_ask_rate: float = DEFAULT_MIN_ASK_RATE


def build_faq_diff(
    *,
    extraction_run: ObservedFactExtractionRun,
    parser_run: ConfiguredFactParserRun,
    diff_config: Optional[FaqDiffConfig] = None,
) -> dict:
    cfg = diff_config or FaqDiffConfig()

    observed = list(
        ObservedBusinessFact.objects.filter(
            extraction_run=extraction_run,
            domain=ObservedBusinessFact.Domain.FAQ,
            fact_type='customer_question',
        )
    )
    configured = list(
        ConfiguredBusinessFact.objects.filter(
            parser_run=parser_run,
            domain=ObservedBusinessFact.Domain.FAQ,
            fact_type='configured_faq',
        )
    )

    obs_by_hash = {o.subject_key_hash: o for o in observed}
    cfg_by_hash = {c.subject_key_hash: c for c in configured}

    rows: list[MergedDiffRow] = []
    handled_hashes: set[str] = set()

    # Exact-key joins
    for sha, cfg_row in cfg_by_hash.items():
        obs = obs_by_hash.get(sha)
        rows.append(_row_for_configured(cfg_row=cfg_row, obs=obs, cfg=cfg))
        handled_hashes.add(sha)

    # Observed-only
    for sha, obs in obs_by_hash.items():
        if sha in handled_hashes:
            continue
        rows.append(_row_for_observed_only(obs=obs, cfg=cfg))

    return merge_diff_rows_bucketed(rows)


def _row_for_configured(
    *, cfg_row: ConfiguredBusinessFact,
    obs: Optional[ObservedBusinessFact],
    cfg: FaqDiffConfig,
) -> MergedDiffRow:
    if obs is None:
        return _mk_row(
            cfg_row=cfg_row, obs=None,
            verdict=DiffCategory.CONFIGURED_NOT_OBSERVED,
            rationale=(
                f'configured FAQ has no matching customer question in '
                f'the corpus (topic={cfg_row.subject_key_json.get("topic")} '
                f'intent={cfg_row.subject_key_json.get("intent")}). '
                'Could be evergreen coverage or a stale entry.'
            ),
        )
    v = obs.value_json or {}
    ask_rate = v.get('customer_ask_rate')
    support = obs.support_n
    if support < cfg.min_support:
        return _mk_row(
            cfg_row=cfg_row, obs=obs,
            verdict=DiffCategory.INSUFFICIENT_EVIDENCE,
            rationale=(
                f'configured + observed but support n={support} '
                f'< floor {cfg.min_support}'
            ),
        )
    if ask_rate is not None and ask_rate >= cfg.min_ask_rate:
        return _mk_row(
            cfg_row=cfg_row, obs=obs,
            verdict=DiffCategory.MATCH,
            rationale=(
                f'configured FAQ AND observed customer question: '
                f'support n={support}, ask_rate={ask_rate:.1%}'
            ),
        )
    return _mk_row(
        cfg_row=cfg_row, obs=obs,
        verdict=DiffCategory.INSUFFICIENT_EVIDENCE,
        rationale=(
            f'configured + observed but ask_rate={ask_rate!r} '
            f'below MATCH floor'
        ),
    )


def _row_for_observed_only(
    *, obs: ObservedBusinessFact, cfg: FaqDiffConfig,
) -> MergedDiffRow:
    support = obs.support_n
    if support < cfg.min_support:
        return _mk_row(
            cfg_row=None, obs=obs,
            verdict=DiffCategory.INSUFFICIENT_EVIDENCE,
            rationale=(
                f'observed FAQ without configured coverage but support '
                f'n={support} below floor {cfg.min_support}'
            ),
        )
    return _mk_row(
        cfg_row=None, obs=obs,
        verdict=DiffCategory.OBSERVED_NOT_CONFIGURED,
        rationale=(
            f'meaningful observed FAQ (n={support}) with no matching '
            f'configured entry — genuine coverage gap'
        ),
    )


def _mk_row(
    *,
    cfg_row: Optional[ConfiguredBusinessFact],
    obs: Optional[ObservedBusinessFact],
    verdict: str,
    rationale: str,
) -> MergedDiffRow:
    subject_key = (
        (cfg_row and cfg_row.subject_key_json)
        or (obs and obs.subject_key_json)
        or {}
    )
    obs_dims = obs.subject_key_dimensions if obs else []
    cfg_dims = cfg_row.subject_key_dimensions if cfg_row else []
    rel = (
        dimensions_are_compatible(obs_dims, cfg_dims)
        if (obs and cfg_row) else 'n/a'
    )
    return MergedDiffRow(
        domain='faq',
        fact_type='customer_question' if obs else 'configured_faq',
        subject_key_json=subject_key,
        observed_dimensions=obs_dims,
        configured_dimensions=cfg_dims,
        key_dimension_relationship=rel,
        verdict=verdict,
        verdict_rationale=rationale,
        observed_value=obs.value_json if obs else None,
        observed_support_n=obs.support_n if obs else 0,
        observed_evidence_conversation_ids=(
            list(obs.evidence_conversation_ids)[:10] if obs else []
        ),
        observed_evidence_turn_ids=(
            list(obs.evidence_turn_ids)[:10] if obs else []
        ),
        configured_value=cfg_row.value_json if cfg_row else None,
        configured_source_pointer=(
            cfg_row.source_pointer if cfg_row else None
        ),
    )
