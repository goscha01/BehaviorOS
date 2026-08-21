"""Deterministic service-scope diff (Pipeline 1D Ship D).

Only `agent_scope_statement` observed facts participate in the diff.
`customer_scope_question` and `performed_observed` are persisted for
transparency but never influence the verdict.

Join key: canonical (service, scope_item, other_topic?). Relationship
is INSIDE value_json so the same subject_key row can carry a
distribution when agents give conflicting scope statements.

Verdict rules:

  MATCH                   — configured relationship matches the
                             dominant observed relationship (>50%
                             of contributing conversations) AND
                             support meets min_support
  CONFLICT                — configured relationship AND dominant
                             observed differ; both have material
                             support
  VARIABLE_CONTEXT_DEPENDENT
                          — observed side shows >=2 relationships
                             with material support (no single
                             dominant relationship)
  OBSERVED_NOT_CONFIGURED — observed agent_scope_statement with
                             meaningful support, no configured entry
  CONFIGURED_NOT_OBSERVED — configured scope with no agent statements
                             (customer_scope_question / performed_observed
                             DO NOT count as observation of the
                             scope policy)
  INSUFFICIENT_EVIDENCE   — support below floor
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


DEFAULT_MIN_SUPPORT = 3
DEFAULT_DOMINANT_MAJORITY = 0.60
DEFAULT_VARIABLE_MIN_MINORITY_SHARE = 0.20


@dataclass
class ServiceScopeDiffConfig:
    min_support: int = DEFAULT_MIN_SUPPORT
    dominant_majority: float = DEFAULT_DOMINANT_MAJORITY
    variable_min_minority_share: float = (
        DEFAULT_VARIABLE_MIN_MINORITY_SHARE
    )


def build_service_scope_diff(
    *,
    extraction_run: ObservedFactExtractionRun,
    parser_run: ConfiguredFactParserRun,
    diff_config: Optional[ServiceScopeDiffConfig] = None,
) -> dict:
    cfg = diff_config or ServiceScopeDiffConfig()

    # ONLY agent_scope_statement observed facts feed the diff.
    observed = list(
        ObservedBusinessFact.objects.filter(
            extraction_run=extraction_run,
            domain=ObservedBusinessFact.Domain.SERVICE_SCOPE,
            fact_type='agent_scope_statement',
        )
    )
    configured = list(
        ConfiguredBusinessFact.objects.filter(
            parser_run=parser_run,
            domain=ObservedBusinessFact.Domain.SERVICE_SCOPE,
            fact_type='configured_scope',
        )
    )

    obs_by_hash = {o.subject_key_hash: o for o in observed}
    cfg_by_hash = {c.subject_key_hash: c for c in configured}

    rows: list[MergedDiffRow] = []
    handled: set[str] = set()

    for sha, cfg_row in cfg_by_hash.items():
        obs = obs_by_hash.get(sha)
        rows.append(_row_for_configured(cfg_row=cfg_row, obs=obs, cfg=cfg))
        handled.add(sha)

    for sha, obs in obs_by_hash.items():
        if sha in handled:
            continue
        rows.append(_row_for_observed_only(obs=obs, cfg=cfg))

    return merge_diff_rows_bucketed(rows)


def _dominant_relationship(
    obs_value: dict, *, cfg: ServiceScopeDiffConfig,
) -> tuple[Optional[str], dict]:
    """Returns (dominant_relationship_or_None, distribution_dict)."""
    dist = (obs_value or {}).get('relationship_distribution') or {}
    if not dist:
        return (None, {})
    total = sum(dist.values())
    if total == 0:
        return (None, dist)
    dominant, count = max(dist.items(), key=lambda kv: kv[1])
    share = count / total
    if share >= cfg.dominant_majority:
        return (dominant, dist)
    return (None, dist)


def _has_variable_distribution(
    dist: dict, *, cfg: ServiceScopeDiffConfig,
) -> bool:
    total = sum(dist.values()) if dist else 0
    if total == 0:
        return False
    material = sum(
        1 for v in dist.values()
        if (v / total) >= cfg.variable_min_minority_share
    )
    return material >= 2


def _row_for_configured(
    *, cfg_row: ConfiguredBusinessFact,
    obs: Optional[ObservedBusinessFact],
    cfg: ServiceScopeDiffConfig,
) -> MergedDiffRow:
    cfg_rel = (cfg_row.value_json or {}).get('relationship')
    if obs is None:
        return _mk_row(
            cfg_row=cfg_row, obs=None,
            verdict=DiffCategory.CONFIGURED_NOT_OBSERVED,
            rationale=(
                f'configured scope ({cfg_rel} for '
                f'{cfg_row.subject_key_json}) has no matching '
                f'agent_scope_statement in the corpus. '
                'Note: customer_scope_question / performed_observed '
                'facts do NOT count as observation of scope policy.'
            ),
        )
    support = obs.support_n
    if support < cfg.min_support:
        return _mk_row(
            cfg_row=cfg_row, obs=obs,
            verdict=DiffCategory.INSUFFICIENT_EVIDENCE,
            rationale=(
                f'configured + observed agent statement but support '
                f'n={support} < floor {cfg.min_support}'
            ),
        )
    dominant, dist = _dominant_relationship(obs.value_json, cfg=cfg)
    if _has_variable_distribution(dist, cfg=cfg):
        return _mk_row(
            cfg_row=cfg_row, obs=obs,
            verdict=DiffCategory.VARIABLE_CONTEXT_DEPENDENT,
            rationale=(
                f'observed agent statements vary across '
                f'relationships {dist} (no single dominant); '
                f'configured says {cfg_rel}. Business behavior is '
                f'context-dependent — check observed context '
                f'distribution.'
            ),
        )
    if dominant is None:
        return _mk_row(
            cfg_row=cfg_row, obs=obs,
            verdict=DiffCategory.INSUFFICIENT_EVIDENCE,
            rationale=(
                f'observed distribution {dist} has no dominant '
                f'relationship at {cfg.dominant_majority:.0%} '
                f'majority; configured says {cfg_rel}'
            ),
        )
    if dominant == cfg_rel:
        return _mk_row(
            cfg_row=cfg_row, obs=obs,
            verdict=DiffCategory.MATCH,
            rationale=(
                f'configured={cfg_rel} matches dominant observed '
                f'({dominant}, {dist})'
            ),
        )
    return _mk_row(
        cfg_row=cfg_row, obs=obs,
        verdict=DiffCategory.CONFLICT,
        rationale=(
            f'configured={cfg_rel} but agents say {dominant} '
            f'({dist}) — real conflict between configured policy '
            f'and agent execution'
        ),
    )


def _row_for_observed_only(
    *, obs: ObservedBusinessFact,
    cfg: ServiceScopeDiffConfig,
) -> MergedDiffRow:
    support = obs.support_n
    if support < cfg.min_support:
        return _mk_row(
            cfg_row=None, obs=obs,
            verdict=DiffCategory.INSUFFICIENT_EVIDENCE,
            rationale=(
                f'observed agent scope statement without configured '
                f'entry, but support n={support} below floor '
                f'{cfg.min_support}'
            ),
        )
    dominant, dist = _dominant_relationship(obs.value_json, cfg=cfg)
    if _has_variable_distribution(dist, cfg=cfg):
        return _mk_row(
            cfg_row=None, obs=obs,
            verdict=DiffCategory.VARIABLE_CONTEXT_DEPENDENT,
            rationale=(
                f'observed scope agents vary across relationships '
                f'{dist}; no configured entry'
            ),
        )
    return _mk_row(
        cfg_row=None, obs=obs,
        verdict=DiffCategory.OBSERVED_NOT_CONFIGURED,
        rationale=(
            f'agents state scope {obs.subject_key_json} = '
            f'{dominant or dist} (n={support}) but there is no '
            f'configured entry — undocumented policy'
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
        domain='service_scope',
        fact_type='scope_policy_join',
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
