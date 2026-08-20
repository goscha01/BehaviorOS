"""MeasurementCreationService — freezes the experimental contract at
apply-time and persists a RecommendationOutcomeMeasurement row.

Called by the consumer-facing API endpoint when LeadBridge reports
that a recommendation was successfully applied. At that point:

  1. Resolve the MeasurementSpec deterministically from the recommendation
     (rec_class + target_signal). Raise NoMeasurementSpec if the rec is
     outside every spec's applicability envelope — the operator sees
     "no measurement available for this recommendation."
  2. Freeze the spec (instantiate its target-signal sentinel from the
     recommendation) and serialize via FrozenMeasurementSpec.to_dict().
  3. Compute the FROZEN baseline cohort — conversations from the
     tenant, started_at in [applied_at - baseline_window_days,
     applied_at), whose semantic events include the target signal.
     Score each conversation's outcome deterministically from its
     OutcomeSnapshots within attribution_window_days of started_at.
  4. Persist the RecommendationOutcomeMeasurement with:
       status=BASELINE_FROZEN
       measurement_started_at=applied_at
       measurement_deadline_at=applied_at + max_window_days_for_inconclusive
       pre_cohort_conversation_ids = frozen list (never recomputed)
       pre_n / pre_positive_n / pre_rate = frozen counters
       treatment_effective_config_hash + treatment_managed_hash + version
         = as reported by LB
       post_* counters = 0 (evaluator will populate)

Idempotent per lb_recommendation_application_id — a duplicate POST
returns the existing row rather than creating a second one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from django.db import transaction
from django.utils import timezone

from apps.conversations.measurement.effective_config_contract import (
    EFFECTIVE_CONFIG_SCHEMA_VERSION,
)
from apps.conversations.measurement.specs import (
    FrozenMeasurementSpec, MeasurementSpec, NoMeasurementSpec,
    resolve_spec_for_recommendation,
)
from apps.conversations.models import (
    BehaviorRecommendation, Conversation, ConversationSemanticEvent,
    OutcomeSnapshot, RecommendationOutcomeMeasurement,
)


logger = logging.getLogger(__name__)


CREATION_VERSION = 'measurement-creation-v1'


class MeasurementCreationError(Exception):
    """Raised when a measurement cannot be created — the caller should
    surface as 422 (deterministic ineligibility) or 500 (unexpected)."""


@dataclass(frozen=True)
class LbApplyContext:
    """Everything LB provides at apply-completion time. All hashes are
    hex sha256 strings (64 chars). schema_version must match the
    contract module's EFFECTIVE_CONFIG_SCHEMA_VERSION or the evaluator
    will refuse to cross-compare."""
    lb_recommendation_application_id: str
    applied_at: datetime
    pre_effective_config_hash: str
    treatment_effective_config_hash: str
    treatment_managed_hash: str
    effective_config_schema_version: str


def create_measurement(
    rec: BehaviorRecommendation,
    ctx: LbApplyContext,
) -> RecommendationOutcomeMeasurement:
    """Freeze the experimental contract and persist a measurement row.

    Deterministic + idempotent. Never touches an LLM. Never calls out
    over the network.
    """
    # Idempotency — a duplicate apply for the same LB application id
    # returns the existing row unchanged. Prevents a webhook retry from
    # silently overwriting a frozen baseline.
    existing = RecommendationOutcomeMeasurement.objects.filter(
        lb_recommendation_application_id=(
            ctx.lb_recommendation_application_id
        ),
    ).first()
    if existing is not None:
        logger.info(
            'MeasurementCreation: existing row returned '
            f'for lb_app={ctx.lb_recommendation_application_id} '
            f'(rom_id={existing.id})'
        )
        return existing

    # Contract-version guard — the row records the incoming version
    # verbatim, but if it doesn't match the current module, log a
    # loud warning. Evaluator will exclude schema-mismatched post
    # conversations (per ROM v1 provenance invariants).
    if ctx.effective_config_schema_version != EFFECTIVE_CONFIG_SCHEMA_VERSION:
        logger.warning(
            'MeasurementCreation: LB reported schema_version='
            f'{ctx.effective_config_schema_version!r}, BehaviorOS '
            f'expects {EFFECTIVE_CONFIG_SCHEMA_VERSION!r}. Persisting '
            'as-is; evaluator will apply schema-mismatch cohort '
            'exclusion until versions align.'
        )

    # Deterministic spec resolution + freeze.
    try:
        spec = resolve_spec_for_recommendation(rec)
    except NoMeasurementSpec as e:
        raise MeasurementCreationError(
            f'No MeasurementSpec applies to this recommendation: {e}'
        ) from e
    frozen = spec.freeze_for_recommendation(rec)
    target_signal = frozen.cohort_entry.signal

    # Derive tenant scope from the recommendation's run.
    if not rec.run.config_snapshot:
        raise MeasurementCreationError(
            f'Recommendation {rec.recommendation_id} has no '
            'config_snapshot on its run — cannot scope baseline cohort '
            'to a tenant.'
        )
    tenant_external_id = rec.run.config_snapshot.tenant_external_id
    org = rec.run.org

    # Baseline cohort computation — FROZEN once at creation.
    baseline_cohort_ids, baseline_pos, baseline_neg = _compute_baseline_cohort(
        org=org,
        tenant_external_id=tenant_external_id,
        target_signal=target_signal,
        applied_at=ctx.applied_at,
        spec=frozen,
    )
    pre_n = baseline_pos + baseline_neg
    pre_rate = (baseline_pos / pre_n) if pre_n > 0 else None

    deadline = ctx.applied_at + timedelta(
        days=frozen.verdict_gates.max_window_days_for_inconclusive,
    )

    with transaction.atomic():
        row = RecommendationOutcomeMeasurement.objects.create(
            org=org,
            recommendation=rec,
            lb_recommendation_application_id=(
                ctx.lb_recommendation_application_id
            ),
            tenant_external_id=tenant_external_id,

            measurement_spec_key=frozen.spec_key,
            measurement_spec_version=frozen.version,
            frozen_spec_json=frozen.to_dict(),

            applied_at=ctx.applied_at,
            subject_state=rec.subject_state,
            subject_signals=list(rec.subject_signals),
            target_signal=target_signal,

            pre_effective_config_hash=ctx.pre_effective_config_hash,
            treatment_effective_config_hash=(
                ctx.treatment_effective_config_hash
            ),
            treatment_managed_hash=ctx.treatment_managed_hash,
            effective_config_schema_version=(
                ctx.effective_config_schema_version
            ),

            pre_cohort_conversation_ids=[
                str(cid) for cid in baseline_cohort_ids
            ],
            pre_cohort_frozen_at=timezone.now(),
            pre_n=pre_n,
            pre_positive_n=baseline_pos,
            pre_rate=pre_rate,

            status=RecommendationOutcomeMeasurement.Status.BASELINE_FROZEN,
            status_reason='baseline_frozen_at_apply',
            evaluation_version=CREATION_VERSION,

            measurement_started_at=ctx.applied_at,
            measurement_deadline_at=deadline,
        )

    logger.info(
        f'MeasurementCreation: created rom_id={row.id} '
        f'for rec={rec.recommendation_id} '
        f'lb_app={ctx.lb_recommendation_application_id} '
        f'pre_n={pre_n} pre_rate={pre_rate!r}'
    )
    return row


def _compute_baseline_cohort(
    *,
    org,
    tenant_external_id: str,
    target_signal: str,
    applied_at: datetime,
    spec: FrozenMeasurementSpec,
) -> tuple[list, int, int]:
    """Find pre-application conversations matching cohort_entry and
    score their outcomes.

    Returns (conversation_ids, positive_n, negative_n).

    Cohort membership:
      - Conversation.org == org (tenant scoping — v1 approximation;
        assumes org is 1:1 with an LB tenant for now; v2 should tighten
        via EntityLink → LB lead ownership check)
      - source == 'leadbridge'
      - started_at in [applied_at - baseline_window_days, applied_at)
      - has at least one ConversationSemanticEvent with
        event_type == target_signal

    Outcome scoring (per FrozenMeasurementSpec.primary_outcome):
      - Look at OutcomeSnapshots for this conversation captured within
        attribution_window_days of started_at
      - Positive if ANY snapshot has a positive_terminal_event True
      - Negative if ANY snapshot has a negative_terminal_event True
        AND no positive was reached
      - Unresolved otherwise (not counted in either arm)
    """
    outcome = spec.primary_outcome
    window_start = applied_at - timedelta(days=outcome.baseline_window_days)

    # Candidate conversations in the baseline window.
    candidate_qs = Conversation.objects.filter(
        org=org,
        source='leadbridge',
        started_at__gte=window_start,
        started_at__lt=applied_at,
    ).only('id', 'started_at')

    # Which of those have at least one target_signal event? Do it as a
    # single semantic-event query to avoid an N+1.
    matching_ids = set(
        ConversationSemanticEvent.objects.filter(
            org=org,
            event_type=target_signal,
            conversation__in=candidate_qs,
        ).values_list('conversation_id', flat=True).distinct()
    )
    if not matching_ids:
        return ([], 0, 0)

    positive_n = 0
    negative_n = 0
    cohort_ids: list = []
    positive_events = frozenset(outcome.positive_terminal_events)
    negative_events = frozenset(outcome.negative_terminal_events)

    # Load conversations + all outcome snapshots in the attribution
    # window for scoring. For v1 the corpus is small enough that a
    # loop is fine; a later optimization can push scoring into SQL.
    convs = Conversation.objects.filter(id__in=matching_ids).only(
        'id', 'started_at',
    )
    for conv in convs:
        cohort_ids.append(conv.id)
        window_end = conv.started_at + timedelta(
            days=outcome.attribution_window_days,
        )
        outcome_qs = OutcomeSnapshot.objects.filter(
            conversation_id=conv.id,
            captured_at__lte=window_end,
        )
        # Terminals derived from the snapshot's boolean columns via
        # the canonical outcome tokens.
        reached_positive = False
        reached_negative = False
        for snap in outcome_qs.only(
            'lb_booked', 'lb_lost', 'lb_cancelled',
            'sf_booked', 'sf_completed', 'sf_cancelled',
        ):
            tokens = _extract_outcome_tokens(snap)
            if positive_events & tokens:
                reached_positive = True
            if negative_events & tokens:
                reached_negative = True
            if reached_positive:
                break
        if reached_positive:
            positive_n += 1
        elif reached_negative:
            negative_n += 1
        # else: unresolved — not counted in either arm

    return (cohort_ids, positive_n, negative_n)


def _extract_outcome_tokens(snap: OutcomeSnapshot) -> frozenset[str]:
    """Turn a single OutcomeSnapshot's boolean columns into canonical
    outcome tokens (per effective_config_contract.OutcomeTerminal).

    Only True values contribute — None ("unknown") is not a terminal.
    """
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
