"""Idempotent evaluator for RecommendationOutcomeMeasurement rows (ROM v1 Step 5).

Callable from:
- A scheduled job (Celery beat, cron)
- A manual trigger (management command)
- Event-driven — when new conversations arrive with matching provenance

Idempotency: re-running the evaluator over the same measurement + the
same corpus MUST produce byte-identical status/counters. All inputs
are read; the frozen contract (spec, baseline, treatment hashes) is
never rewritten; only accumulated counters + verdict fields are
updated.

Provenance classification per ROM v1 invariants (see project memory):
  status=OK + full hash matches treatment  → provenance_eligible (clean)
  status=OK + full mismatch + managed match → contaminated
  status=OK + managed mismatch              → treatment_moved (excluded)
  status=OK + schema version mismatch       → provenance_schema_mismatch
  status=HASH_FAILED                        → provenance_hash_failed
  status=PENDING                            → provenance_pending
  (unstamped legacy rows have no OK/PENDING/HASH_FAILED — treated as
  provenance_pending for accounting purposes)

Status machine:
  BASELINE_FROZEN → COLLECTING → READY → {IMPROVED | NO_MATERIAL_CHANGE |
                                            WORSE | INCONCLUSIVE}
Terminal statuses are never rewritten (evaluator refuses on
finalized_at != null).

READY = min-observation thresholds (sample floor + coverage floor)
satisfied, terminal evaluation can legitimately be attempted. READY
DOES NOT mean statistically significant — that's what the terminal
statuses IMPROVED / WORSE encode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.db import transaction
from django.utils import timezone

from apps.conversations.measurement.effective_config_contract import (
    EFFECTIVE_CONFIG_SCHEMA_VERSION, ProvenanceStatus,
)
from apps.conversations.measurement.specs import (
    FrozenMeasurementSpec, get_spec,
)
from apps.conversations.measurement.stats import (
    fishers_exact_two_sided_p, newcombe_diff_ci,
)
from apps.conversations.models import (
    Conversation, ConversationSemanticEvent, OutcomeSnapshot,
    RecommendationOutcomeMeasurement,
)


logger = logging.getLogger(__name__)


EVALUATION_VERSION = 'measurement-evaluator-v1'


@dataclass
class _Counters:
    # target-signal denominator across ALL post-application matching
    # conversations regardless of provenance
    target_signal_total: int = 0

    # provenance breakdown of the target-signal conversations
    provenance_eligible: int = 0
    provenance_pending: int = 0
    provenance_hash_failed: int = 0
    provenance_schema_mismatch: int = 0
    contaminated: int = 0
    treatment_moved: int = 0

    # outcome scoring — only on the eligible/clean subset
    positive: int = 0
    negative: int = 0  # unresolved excluded from both

    def eligible_resolved_n(self) -> int:
        return self.positive + self.negative

    def coverage(self) -> Optional[float]:
        if self.target_signal_total == 0:
            return None
        return self.provenance_eligible / self.target_signal_total


def evaluate(
    measurement: RecommendationOutcomeMeasurement,
) -> RecommendationOutcomeMeasurement:
    """Re-score `measurement` in place. Returns the updated instance.

    Refuses to mutate rows that are already finalized (terminal +
    finalized_at set). Callers can safely invoke this repeatedly.
    """
    if measurement.finalized_at is not None:
        logger.info(
            f'Evaluator: measurement {measurement.id} is finalized '
            f'(status={measurement.status}); no-op.'
        )
        return measurement

    spec_data = measurement.frozen_spec_json
    frozen_spec = _rehydrate_spec_from_dict(spec_data)

    counters = _score_post_cohort(measurement, frozen_spec)
    verdict = _compute_verdict(measurement, frozen_spec, counters)

    return _persist(measurement, counters, verdict)


def _score_post_cohort(
    measurement: RecommendationOutcomeMeasurement,
    spec: FrozenMeasurementSpec,
) -> _Counters:
    """Walk post-application conversations for this tenant, classify
    each by provenance state, and score outcomes on the clean subset.
    """
    counters = _Counters()
    outcome = spec.primary_outcome
    applied_at = measurement.applied_at
    now = timezone.now()
    # Post-cohort candidate window: conversations that STARTED after
    # applied_at AND have had at least attribution_window_days to
    # accumulate outcomes. Later starts are still counted toward the
    # target_signal denominator but won't be scorable until their
    # window closes.
    window_close = now - timedelta(days=outcome.attribution_window_days)

    # Post-cohort candidates: any conversation on the tenant's org
    # (all sources) started after applied_at. Provenance
    # classification filters non-clean rows into their own buckets
    # so quo/callio/etc conversations without provenance stamps
    # count toward `provenance_pending_n` (excluded) rather than
    # being silently missed.
    candidate_qs = Conversation.objects.filter(
        org=measurement.org,
        started_at__gte=applied_at,
    ).only(
        'id', 'started_at',
        # provenance columns — added by the LB conversation-provenance
        # migration (20260822000000). LB writes them into
        # metadata['config_provenance'] on ingestion into BehaviorOS
        # (see Note in a follow-up commit that wires the LB→BehaviorOS
        # sync to forward the hashes). For now the evaluator reads
        # them from Conversation.metadata as a v1 arrangement — v2
        # will promote them to first-class columns in the BehaviorOS
        # Conversation model too.
        'metadata',
    )

    matching_ids = set(
        ConversationSemanticEvent.objects.filter(
            org=measurement.org,
            event_type=spec.cohort_entry.signal,
            conversation__in=candidate_qs,
        ).values_list('conversation_id', flat=True).distinct()
    )
    if not matching_ids:
        return counters

    positive_events = frozenset(outcome.positive_terminal_events)
    negative_events = frozenset(outcome.negative_terminal_events)

    convs = Conversation.objects.filter(
        id__in=matching_ids,
    ).only('id', 'started_at', 'metadata')

    for conv in convs:
        counters.target_signal_total += 1
        eligibility = _classify_provenance(
            conv=conv,
            treatment_full=measurement.treatment_effective_config_hash,
            treatment_managed=measurement.treatment_managed_hash,
            expected_schema=(
                measurement.effective_config_schema_version
            ),
        )
        if eligibility == 'eligible':
            counters.provenance_eligible += 1
        elif eligibility == 'pending':
            counters.provenance_pending += 1
        elif eligibility == 'hash_failed':
            counters.provenance_hash_failed += 1
        elif eligibility == 'schema_mismatch':
            counters.provenance_schema_mismatch += 1
        elif eligibility == 'contaminated':
            counters.contaminated += 1
        elif eligibility == 'treatment_moved':
            counters.treatment_moved += 1

        # Only score outcomes on the clean subset. Contamination and
        # treatment-moved go in their own buckets for transparency
        # but do not affect the primary post_rate.
        if eligibility != 'eligible':
            continue

        # Skip conversations whose attribution window hasn't closed
        # yet — outcome may not be resolved. They still count toward
        # target_signal_total + provenance_eligible so the operator
        # sees them ("N eligible but too new to score").
        if conv.started_at > window_close:
            continue

        window_end = conv.started_at + timedelta(
            days=outcome.attribution_window_days,
        )
        outcome_qs = OutcomeSnapshot.objects.filter(
            conversation_id=conv.id,
            captured_at__lte=window_end,
        ).only(
            'lb_booked', 'lb_lost', 'lb_cancelled',
            'sf_booked', 'sf_completed', 'sf_cancelled',
        )
        reached_positive = False
        reached_negative = False
        for snap in outcome_qs:
            tokens = _outcome_tokens(snap)
            if positive_events & tokens:
                reached_positive = True
                break
            if negative_events & tokens:
                reached_negative = True
        if reached_positive:
            counters.positive += 1
        elif reached_negative:
            counters.negative += 1
        # else unresolved — not counted in either arm

    return counters


def _classify_provenance(
    *,
    conv: Conversation,
    treatment_full: str,
    treatment_managed: str,
    expected_schema: str,
) -> str:
    """Return one of: eligible | pending | hash_failed | schema_mismatch |
    contaminated | treatment_moved

    Reads provenance from Conversation.metadata['config_provenance']
    for now — v1 arrangement. LB stamps the fields on its own
    Conversation table; the LB→BehaviorOS sync forwards them here
    inside the conversation metadata blob. A follow-up will promote
    these to first-class BehaviorOS columns.
    """
    meta = conv.metadata or {}
    prov = (meta.get('config_provenance') or {}) if isinstance(meta, dict) else {}
    status = prov.get('status') or ProvenanceStatus.PENDING
    if status == ProvenanceStatus.HASH_FAILED:
        return 'hash_failed'
    if status == ProvenanceStatus.PENDING:
        return 'pending'
    if status != ProvenanceStatus.OK:
        # Unknown / unexpected status — treat as pending so we don't
        # silently include it in the clean cohort.
        return 'pending'

    schema = prov.get('effective_config_schema_version', '')
    if schema and schema != expected_schema:
        return 'schema_mismatch'

    full_hash = prov.get('effective_config_hash_at_start', '')
    managed_hash = prov.get('behavior_os_managed_hash_at_start', '')
    if not full_hash or not managed_hash:
        return 'pending'

    if full_hash == treatment_full and managed_hash == treatment_managed:
        return 'eligible'
    if managed_hash != treatment_managed:
        # BehaviorOS-managed rule changed after apply → this conversation
        # experienced a different treatment
        return 'treatment_moved'
    # managed matches but full doesn't — non-BehaviorOS config drift
    return 'contaminated'


def _outcome_tokens(snap: OutcomeSnapshot) -> frozenset[str]:
    tokens: set[str] = set()
    if snap.lb_booked is True:
        tokens.add('LB_BOOKED')
    if snap.lb_lost is True:
        tokens.add('LB_LOST')
    if snap.lb_cancelled is True:
        tokens.add('LB_CANCELLED')
    if snap.sf_booked is True:
        tokens.add('SF_BOOKED')
    if snap.sf_completed is True:
        tokens.add('SF_COMPLETED')
    if snap.sf_cancelled is True:
        tokens.add('SF_CANCELLED')
    return frozenset(tokens)


# ---------- Verdict ----------


@dataclass
class _Verdict:
    status: str
    reason: str
    effect_size_pp: Optional[float]
    ci_low_pp: Optional[float]
    ci_high_pp: Optional[float]
    p_value: Optional[float]


def _compute_verdict(
    measurement: RecommendationOutcomeMeasurement,
    spec: FrozenMeasurementSpec,
    counters: _Counters,
) -> _Verdict:
    """Deterministic verdict from the frozen contract + accumulated
    counters. Never calls out; never touches LLM."""
    gates = spec.verdict_gates
    Status = RecommendationOutcomeMeasurement.Status
    now = timezone.now()
    deadline_reached = now >= measurement.measurement_deadline_at

    pre_n = measurement.pre_n
    pre_pos = measurement.pre_positive_n
    post_n = counters.eligible_resolved_n()
    post_pos = counters.positive

    # Effect + CI + p — computed even when gates fail so the UI can
    # show current-state numbers before the verdict fires. Only the
    # STATUS gate uses them terminally.
    effect_pp = None
    ci_low_pp = None
    ci_high_pp = None
    p_value = None
    if pre_n > 0 and post_n > 0:
        pre_rate = pre_pos / pre_n
        post_rate = post_pos / post_n
        effect_pp = (post_rate - pre_rate) * 100.0
        lo, hi = newcombe_diff_ci(
            post_pos, post_n, pre_pos, pre_n,
            alpha=gates.uncertainty_significance_alpha,
        )
        ci_low_pp = lo * 100.0
        ci_high_pp = hi * 100.0
        p_value = fishers_exact_two_sided_p(
            post_pos, post_n - post_pos,
            pre_pos, pre_n - pre_pos,
        )

    coverage = counters.coverage()
    coverage_ok = (
        coverage is not None
        and coverage >= gates.min_provenance_coverage
    )
    sample_ok = (
        pre_n >= gates.min_sample_per_arm
        and post_n >= gates.min_sample_per_arm
    )

    # Deadline reached — no more waiting.
    if deadline_reached:
        if not sample_ok or not coverage_ok or effect_pp is None:
            return _Verdict(
                status=Status.INCONCLUSIVE,
                reason=(
                    f'deadline_reached_thresholds_unmet: '
                    f'sample_ok={sample_ok} coverage_ok={coverage_ok} '
                    f'coverage={coverage!r}'
                ),
                effect_size_pp=effect_pp,
                ci_low_pp=ci_low_pp, ci_high_pp=ci_high_pp,
                p_value=p_value,
            )
        # Sample + coverage OK. Verdict from stats:
        return _terminal_from_stats(
            effect_pp, ci_low_pp, ci_high_pp, p_value, gates,
            deadline_reached=True,
        )

    # Not at deadline yet.
    if not coverage_ok:
        return _Verdict(
            status=Status.COLLECTING,
            reason=f'provenance_coverage_below_floor coverage={coverage!r}',
            effect_size_pp=effect_pp,
            ci_low_pp=ci_low_pp, ci_high_pp=ci_high_pp,
            p_value=p_value,
        )
    if not sample_ok:
        return _Verdict(
            status=Status.COLLECTING,
            reason=(
                f'sample_below_floor pre={pre_n} post={post_n} '
                f'floor={gates.min_sample_per_arm}'
            ),
            effect_size_pp=effect_pp,
            ci_low_pp=ci_low_pp, ci_high_pp=ci_high_pp,
            p_value=p_value,
        )

    # Sample + coverage met → attempt terminal verdict. READY is used
    # when the stats can't yet distinguish; otherwise we jump straight
    # to a terminal.
    return _terminal_from_stats(
        effect_pp, ci_low_pp, ci_high_pp, p_value, gates,
        deadline_reached=False,
    )


def _terminal_from_stats(
    effect_pp: Optional[float],
    ci_low_pp: Optional[float],
    ci_high_pp: Optional[float],
    p_value: Optional[float],
    gates,
    *,
    deadline_reached: bool,
) -> _Verdict:
    Status = RecommendationOutcomeMeasurement.Status
    if effect_pp is None or p_value is None:
        # Can't score at all — treat as INCONCLUSIVE at deadline,
        # READY otherwise (waiting for at least one resolvable outcome).
        return _Verdict(
            status=(
                Status.INCONCLUSIVE if deadline_reached else Status.READY
            ),
            reason='no_scorable_outcomes',
            effect_size_pp=effect_pp,
            ci_low_pp=ci_low_pp, ci_high_pp=ci_high_pp,
            p_value=p_value,
        )
    passes_effect = abs(effect_pp) >= gates.min_effect_size_pp
    passes_sig = p_value < gates.uncertainty_significance_alpha
    if passes_effect and passes_sig:
        status = Status.IMPROVED if effect_pp > 0 else Status.WORSE
        return _Verdict(
            status=status,
            reason=(
                f'{status}: {effect_pp:+.1f}pp p={p_value:.3f}'
            ),
            effect_size_pp=effect_pp,
            ci_low_pp=ci_low_pp, ci_high_pp=ci_high_pp,
            p_value=p_value,
        )
    # Effect + significance not both met. If CI is tight enough to
    # rule out a material effect in either direction AND deadline
    # reached → NO_MATERIAL_CHANGE. Otherwise READY (waiting) or
    # INCONCLUSIVE (deadline + wide CI).
    ci_excludes_material = (
        ci_low_pp is not None
        and ci_high_pp is not None
        and ci_low_pp > -gates.min_effect_size_pp
        and ci_high_pp < gates.min_effect_size_pp
    )
    if deadline_reached:
        if ci_excludes_material:
            return _Verdict(
                status=Status.NO_MATERIAL_CHANGE,
                reason=(
                    f'no_material_change effect={effect_pp:+.1f}pp '
                    f'CI=[{ci_low_pp:+.1f}, {ci_high_pp:+.1f}]pp '
                    f'p={p_value:.3f}'
                ),
                effect_size_pp=effect_pp,
                ci_low_pp=ci_low_pp, ci_high_pp=ci_high_pp,
                p_value=p_value,
            )
        return _Verdict(
            status=Status.INCONCLUSIVE,
            reason=(
                f'inconclusive_at_deadline effect={effect_pp:+.1f}pp '
                f'p={p_value:.3f}'
            ),
            effect_size_pp=effect_pp,
            ci_low_pp=ci_low_pp, ci_high_pp=ci_high_pp,
            p_value=p_value,
        )
    return _Verdict(
        status=Status.READY,
        reason=(
            f'ready_no_terminal_yet effect={effect_pp:+.1f}pp '
            f'p={p_value:.3f}'
        ),
        effect_size_pp=effect_pp,
        ci_low_pp=ci_low_pp, ci_high_pp=ci_high_pp,
        p_value=p_value,
    )


def _persist(
    m: RecommendationOutcomeMeasurement,
    counters: _Counters,
    verdict: _Verdict,
) -> RecommendationOutcomeMeasurement:
    now = timezone.now()
    with transaction.atomic():
        m.target_signal_conversations_n = counters.target_signal_total
        m.provenance_eligible_n = counters.provenance_eligible
        m.provenance_pending_n = counters.provenance_pending
        m.provenance_hash_failed_n = counters.provenance_hash_failed
        m.provenance_schema_mismatch_n = counters.provenance_schema_mismatch
        m.contaminated_n = counters.contaminated
        m.treatment_moved_n = counters.treatment_moved

        m.post_n = counters.eligible_resolved_n()
        m.post_positive_n = counters.positive
        m.post_rate = (
            counters.positive / m.post_n if m.post_n > 0 else None
        )

        m.effect_size_pp = verdict.effect_size_pp
        m.ci_low_pp = verdict.ci_low_pp
        m.ci_high_pp = verdict.ci_high_pp
        m.p_value = verdict.p_value

        m.status = verdict.status
        m.status_reason = verdict.reason[:255]
        m.evaluation_version = EVALUATION_VERSION
        m.last_evaluated_at = now

        if m.status in RecommendationOutcomeMeasurement.TERMINAL_STATUSES:
            m.finalized_at = now

        m.save()
    logger.info(
        f'Evaluator: rom_id={m.id} → status={m.status} '
        f'(pre={m.pre_positive_n}/{m.pre_n} '
        f'post={m.post_positive_n}/{m.post_n} '
        f'effect={m.effect_size_pp!r} p={m.p_value!r} '
        f'coverage={counters.coverage()!r})'
    )
    return m


def _rehydrate_spec_from_dict(d: dict) -> FrozenMeasurementSpec:
    """Reconstruct a FrozenMeasurementSpec from its persisted JSON.

    This does NOT go through the live spec registry — the frozen spec
    on the row IS the contract, even if the code-side spec has
    evolved. Ensures re-evaluation of historical rows uses their
    original semantics.
    """
    from apps.conversations.measurement.specs import (
        CohortEntryPredicate, ExclusionRule, PrimaryOutcomeDefinition,
        VerdictGates,
    )
    ce = d['cohort_entry']
    po = d['primary_outcome']
    ex = d['exclusions']
    vg = d['verdict_gates']
    return FrozenMeasurementSpec(
        spec_key=d['spec_key'],
        version=d['version'],
        family=d['family'],
        description=d['description'],
        cohort_entry=CohortEntryPredicate(
            kind=ce['kind'], signal=ce['signal'],
        ),
        primary_outcome=PrimaryOutcomeDefinition(
            kind=po['kind'],
            attribution_window_days=po['attribution_window_days'],
            baseline_window_days=po['baseline_window_days'],
            positive_terminal_events=tuple(po['positive_terminal_events']),
            negative_terminal_events=tuple(po['negative_terminal_events']),
        ),
        exclusions=ExclusionRule(tokens=tuple(ex['tokens'])),
        verdict_gates=VerdictGates(
            min_sample_per_arm=vg['min_sample_per_arm'],
            min_effect_size_pp=vg['min_effect_size_pp'],
            uncertainty_significance_alpha=(
                vg['uncertainty_significance_alpha']
            ),
            min_provenance_coverage=vg['min_provenance_coverage'],
            max_window_days_for_inconclusive=(
                vg['max_window_days_for_inconclusive']
            ),
        ),
    )
