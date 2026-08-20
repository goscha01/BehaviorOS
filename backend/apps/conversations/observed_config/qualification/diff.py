"""Deterministic qualification diff (Pipeline 1D Ship B).

Joins ObservedBusinessFact (qualification, three fact_types per
field) against ConfiguredBusinessFact (fact_type=configured_question)
via canonical subject_key hash. Classifies each field into one of
the DiffCategory buckets.

Verdict rules (per user spec):

  CONFIGURED_AND_OBSERVED  → configured entry exists AND observed
                              ask_rate above `min_ask_rate` OR
                              observed volunteer_rate above
                              `min_volunteer_rate`. Reports ask/
                              answer/volunteer rates in payload.
  CONFIGURED_NOT_OBSERVED  → configured entry exists BUT observed
                              ask_rate + volunteer_rate both below
                              floor. Explicit epistemic caveat:
                              NOT an execution gap — the field may
                              be pre-populated by lead-source
                              metadata (Thumbtack/Yelp) before the
                              chat begins. v1 does not yet model
                              obtained_from_lead_source.
  OBSERVED_NOT_CONFIGURED  → observed field with meaningful support
                              has no configured entry.
  MATCH                    → subset of CONFIGURED_AND_OBSERVED where
                              ask+answer rates are strong (both
                              above `strong_capture_rate`).
  CONFLICT                 → configured=required BUT ask_rate below
                              `min_ask_rate_when_required` AND
                              volunteer_rate below floor. Still
                              carries the caveat that lead-source
                              metadata could explain it.
  VARIABLE_CONTEXT_DEPENDENT → field asked in some service_contexts
                                but not others (per-service split
                                with high variance across contexts).
                                v1: fires when the same field
                                appears with different
                                service_context values whose
                                ask_rate differs by >= 40pp.
  INSUFFICIENT_EVIDENCE    → configured entry exists but observed
                              side had too few eligible conversations
                              to say anything reliable
                              (< min_eligible_conversations).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from apps.conversations.models import (
    ConfiguredBusinessFact, ObservedBusinessFact,
    ConfiguredFactParserRun, ObservedFactExtractionRun,
)
from apps.conversations.observed_config.base import (
    DiffCategory, MergedDiffRow, canonical_subject_key,
    dimensions_are_compatible, merge_diff_rows_bucketed,
)


DEFAULT_MIN_ELIGIBLE_CONVERSATIONS = 20
DEFAULT_MIN_ASK_RATE = 0.15
DEFAULT_STRONG_CAPTURE_RATE = 0.50
DEFAULT_MIN_ASK_RATE_WHEN_REQUIRED = 0.30
DEFAULT_MIN_VOLUNTEER_RATE = 0.10
DEFAULT_VARIABLE_ASK_RATE_SPREAD_PP = 40.0


@dataclass
class QualificationDiffConfig:
    min_eligible_conversations: int = DEFAULT_MIN_ELIGIBLE_CONVERSATIONS
    min_ask_rate: float = DEFAULT_MIN_ASK_RATE
    strong_capture_rate: float = DEFAULT_STRONG_CAPTURE_RATE
    min_ask_rate_when_required: float = (
        DEFAULT_MIN_ASK_RATE_WHEN_REQUIRED
    )
    min_volunteer_rate: float = DEFAULT_MIN_VOLUNTEER_RATE
    variable_ask_rate_spread_pp: float = (
        DEFAULT_VARIABLE_ASK_RATE_SPREAD_PP
    )


def build_qualification_diff(
    *,
    extraction_run: ObservedFactExtractionRun,
    parser_run: ConfiguredFactParserRun,
    diff_config: Optional[QualificationDiffConfig] = None,
) -> dict:
    cfg = diff_config or QualificationDiffConfig()

    observed_all = list(
        ObservedBusinessFact.objects.filter(
            extraction_run=extraction_run,
            domain=ObservedBusinessFact.Domain.QUALIFICATION,
        )
    )
    configured_all = list(
        ConfiguredBusinessFact.objects.filter(
            parser_run=parser_run,
            domain=ObservedBusinessFact.Domain.QUALIFICATION,
        )
    )

    # Group observed by subject_key_hash + fact_type so we can compose
    # ask/answer/volunteer for each field.
    obs_by_hash: dict[str, dict] = {}
    for o in observed_all:
        entry = obs_by_hash.setdefault(o.subject_key_hash, {
            'subject_key': o.subject_key_json,
            'dimensions': o.subject_key_dimensions,
            'question_asked': None,
            'answer_provided': None,
            'volunteered_before_question': None,
        })
        entry[o.fact_type] = o

    cfg_by_hash = {c.subject_key_hash: c for c in configured_all}

    total_processed_hint = _guess_total_processed(observed_all)

    rows: list[MergedDiffRow] = []
    handled_hashes: set[str] = set()

    # 1) Configured + observed join on exact key hash
    for sha, cfg_row in cfg_by_hash.items():
        obs_entry = obs_by_hash.get(sha)
        rows.append(
            _row_for_configured_field(
                cfg_row=cfg_row, obs_entry=obs_entry,
                total_processed_hint=total_processed_hint,
                cfg=cfg,
            )
        )
        handled_hashes.add(sha)

    # 2) Observed fields not in configured — OBSERVED_NOT_CONFIGURED
    for sha, entry in obs_by_hash.items():
        if sha in handled_hashes:
            continue
        rows.append(
            _row_for_observed_only(
                obs_entry=entry,
                total_processed_hint=total_processed_hint,
                cfg=cfg,
            )
        )

    # 3) VARIABLE_CONTEXT_DEPENDENT sweep: same `field` appears with
    #    multiple `service_context` values whose ask_rates differ by
    #    >= threshold. Emitted as an OVERLAY row (informational, does
    #    NOT replace the per-context rows).
    variability_rows = _variability_overlay(observed_all, cfg=cfg)
    rows.extend(variability_rows)

    return merge_diff_rows_bucketed(rows)


def _guess_total_processed(observed: list[ObservedBusinessFact]) -> Optional[int]:
    """Read `eligible_conversations` off any observed row's value_json
    (they all share the same number)."""
    for o in observed:
        v = o.value_json or {}
        n = v.get('eligible_conversations')
        if n is not None:
            return int(n)
    return None


def _row_for_configured_field(
    *,
    cfg_row: ConfiguredBusinessFact,
    obs_entry: Optional[dict],
    total_processed_hint: Optional[int],
    cfg: QualificationDiffConfig,
) -> MergedDiffRow:
    cfg_val = cfg_row.value_json or {}
    required = bool(cfg_val.get('required'))
    subject_key = cfg_row.subject_key_json

    ask, answer, vol = (
        obs_entry.get('question_asked') if obs_entry else None,
        obs_entry.get('answer_provided') if obs_entry else None,
        obs_entry.get('volunteered_before_question') if obs_entry else None,
    )

    ask_rate = _rate(ask, 'ask_rate')
    volunteer_rate = _rate(vol, 'volunteer_rate')
    capture_rate = _rate(answer, 'capture_rate')
    total_processed = (
        (ask.value_json or {}).get('eligible_conversations')
        if ask else (
            (vol.value_json or {}).get('eligible_conversations')
            if vol else total_processed_hint
        )
    )

    if total_processed is not None and (
        total_processed < cfg.min_eligible_conversations
    ):
        return _mk_row(
            cfg_row=cfg_row, obs_entry=obs_entry,
            verdict=DiffCategory.INSUFFICIENT_EVIDENCE,
            rationale=(
                f'configured field but corpus has only {total_processed} '
                f'eligible conversations (< {cfg.min_eligible_conversations})'
            ),
            observed_summary=_summarize(ask, answer, vol),
        )

    if obs_entry is None or (
        ask_rate is None and volunteer_rate is None
    ):
        return _mk_row(
            cfg_row=cfg_row, obs_entry=obs_entry,
            verdict=DiffCategory.CONFIGURED_NOT_OBSERVED,
            rationale=(
                'configured field with NO ask or volunteer evidence in '
                'the corpus. Epistemic caveat: v1 does not yet '
                'distinguish "asked in chat" from "obtained from lead-'
                'source metadata (Thumbtack/Yelp)". This is not '
                'automatically an execution gap.'
            ),
            observed_summary=_summarize(ask, answer, vol),
        )

    # Strong-observed field — MATCH bucket if both ask and answer are strong.
    if (
        ask_rate is not None
        and ask_rate >= cfg.min_ask_rate
        and capture_rate is not None
        and capture_rate >= cfg.strong_capture_rate
    ):
        return _mk_row(
            cfg_row=cfg_row, obs_entry=obs_entry,
            verdict=DiffCategory.MATCH,
            rationale=(
                f'configured + observed strongly: ask_rate='
                f'{ask_rate:.0%} capture_rate={capture_rate:.0%}'
            ),
            observed_summary=_summarize(ask, answer, vol),
        )

    if required and (
        ask_rate is not None and ask_rate < cfg.min_ask_rate_when_required
    ) and (
        volunteer_rate is None
        or volunteer_rate < cfg.min_volunteer_rate
    ):
        return _mk_row(
            cfg_row=cfg_row, obs_entry=obs_entry,
            verdict=DiffCategory.CONFLICT,
            rationale=(
                f'configured REQUIRED but rarely asked/volunteered: '
                f'ask_rate={ask_rate:.0%} volunteer_rate='
                f'{volunteer_rate!r}. '
                f'Caveat: check whether Thumbtack/Yelp already provides '
                f'this field before the conversation begins.'
            ),
            observed_summary=_summarize(ask, answer, vol),
        )

    # Weak but present observation — still CONFIGURED_AND_OBSERVED bucketed
    # as INSUFFICIENT_EVIDENCE (weak observation) OR
    # OBSERVED_NOT_CONFIGURED's mirror. Use INSUFFICIENT_EVIDENCE for the
    # "some evidence, not strong enough for MATCH" case.
    return _mk_row(
        cfg_row=cfg_row, obs_entry=obs_entry,
        verdict=DiffCategory.INSUFFICIENT_EVIDENCE,
        rationale=(
            f'configured + observed but below MATCH thresholds: '
            f'ask_rate={ask_rate!r} capture_rate={capture_rate!r} '
            f'volunteer_rate={volunteer_rate!r}'
        ),
        observed_summary=_summarize(ask, answer, vol),
    )


def _row_for_observed_only(
    *,
    obs_entry: dict,
    total_processed_hint: Optional[int],
    cfg: QualificationDiffConfig,
) -> MergedDiffRow:
    ask = obs_entry.get('question_asked')
    answer = obs_entry.get('answer_provided')
    vol = obs_entry.get('volunteered_before_question')
    ask_rate = _rate(ask, 'ask_rate')
    volunteer_rate = _rate(vol, 'volunteer_rate')

    # Meaningful observed support if either ask_rate or volunteer_rate
    # clears the floor. Otherwise INSUFFICIENT_EVIDENCE.
    meaningful = (
        (ask_rate is not None and ask_rate >= cfg.min_ask_rate)
        or (
            volunteer_rate is not None
            and volunteer_rate >= cfg.min_volunteer_rate
        )
    )
    verdict = (
        DiffCategory.OBSERVED_NOT_CONFIGURED if meaningful
        else DiffCategory.INSUFFICIENT_EVIDENCE
    )
    return _mk_row(
        cfg_row=None, obs_entry=obs_entry,
        verdict=verdict,
        rationale=(
            f'observed field with no configured entry '
            f'(ask_rate={ask_rate!r} volunteer_rate={volunteer_rate!r})'
        ),
        observed_summary=_summarize(ask, answer, vol),
    )


def _variability_overlay(
    observed_all: list[ObservedBusinessFact],
    *, cfg: QualificationDiffConfig,
) -> list[MergedDiffRow]:
    """For each `field`, if the same field appears with multiple
    `service_context` values whose ask_rate spread exceeds threshold,
    emit ONE overlay row per field in VARIABLE_CONTEXT_DEPENDENT."""
    ask_by_field_context: dict[str, dict[Optional[str], float]] = (
        defaultdict(dict)
    )
    for o in observed_all:
        if o.fact_type != 'question_asked':
            continue
        field_v = (o.subject_key_json or {}).get('field')
        if not field_v:
            continue
        ctx = (o.subject_key_json or {}).get('service_context')
        ar = _rate(o, 'ask_rate')
        if ar is None:
            continue
        ask_by_field_context[field_v][ctx] = ar

    overlays: list[MergedDiffRow] = []
    for field_v, per_ctx in ask_by_field_context.items():
        if len(per_ctx) < 2:
            continue
        vals = list(per_ctx.values())
        spread_pp = (max(vals) - min(vals)) * 100.0
        if spread_pp < cfg.variable_ask_rate_spread_pp:
            continue
        overlays.append(MergedDiffRow(
            domain='qualification',
            fact_type='variability_overlay',
            subject_key_json={'field': field_v},
            observed_dimensions=['field'],
            configured_dimensions=[],
            key_dimension_relationship='n/a',
            verdict=DiffCategory.VARIABLE_CONTEXT_DEPENDENT,
            verdict_rationale=(
                f'field {field_v!r} ask_rate varies across '
                f'service_context by {spread_pp:.0f}pp: '
                + ', '.join(
                    f'{ctx or "(none)"}={ar:.0%}'
                    for ctx, ar in per_ctx.items()
                )
            ),
            observed_value={'per_service_context_ask_rate': {
                str(ctx or '(none)'): ar for ctx, ar in per_ctx.items()
            }},
            observed_support_n=sum(
                1 for _ in per_ctx
            ),
            observed_evidence_conversation_ids=[],
            observed_evidence_turn_ids=[],
            configured_value=None,
            configured_source_pointer=None,
        ))
    return overlays


def _rate(fact: Optional[ObservedBusinessFact], key: str) -> Optional[float]:
    if fact is None:
        return None
    v = fact.value_json or {}
    r = v.get(key)
    if r is None:
        return None
    try:
        return float(r)
    except (TypeError, ValueError):
        return None


def _summarize(ask, answer, vol) -> dict:
    def _f(f):
        return (f.value_json if f else None), (f.support_n if f else 0)
    ask_v, ask_n = _f(ask)
    ans_v, ans_n = _f(answer)
    vol_v, vol_n = _f(vol)
    return {
        'question_asked': {
            'support_n': ask_n,
            'value': ask_v,
            'evidence_conversation_ids': (
                list((ask.evidence_conversation_ids or []))[:5]
                if ask else []
            ),
        },
        'answer_provided': {
            'support_n': ans_n,
            'value': ans_v,
            'evidence_conversation_ids': (
                list((answer.evidence_conversation_ids or []))[:5]
                if answer else []
            ),
        },
        'volunteered_before_question': {
            'support_n': vol_n,
            'value': vol_v,
            'evidence_conversation_ids': (
                list((vol.evidence_conversation_ids or []))[:5]
                if vol else []
            ),
        },
    }


def _mk_row(
    *,
    cfg_row: Optional[ConfiguredBusinessFact],
    obs_entry: Optional[dict],
    verdict: str,
    rationale: str,
    observed_summary: dict,
) -> MergedDiffRow:
    subject_key = (
        (cfg_row and cfg_row.subject_key_json)
        or (obs_entry and obs_entry.get('subject_key'))
        or {}
    )
    obs_dims = obs_entry['dimensions'] if obs_entry else []
    cfg_dims = cfg_row.subject_key_dimensions if cfg_row else []
    rel = (
        dimensions_are_compatible(obs_dims, cfg_dims)
        if (obs_entry and cfg_row) else 'n/a'
    )
    return MergedDiffRow(
        domain='qualification',
        fact_type='field_coverage',
        subject_key_json=subject_key,
        observed_dimensions=obs_dims,
        configured_dimensions=cfg_dims,
        key_dimension_relationship=rel,
        verdict=verdict,
        verdict_rationale=rationale,
        observed_value=observed_summary,
        observed_support_n=(
            sum(
                observed_summary[k]['support_n']
                for k in observed_summary
            )
        ),
        observed_evidence_conversation_ids=(
            list({
                cid
                for k in observed_summary
                for cid in observed_summary[k]['evidence_conversation_ids']
            })
        ),
        observed_evidence_turn_ids=[],
        configured_value=(cfg_row.value_json if cfg_row else None),
        configured_source_pointer=(cfg_row.source_pointer if cfg_row else None),
    )
