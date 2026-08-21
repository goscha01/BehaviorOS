"""Unified Business Reconstruction service (Pipeline 1D Hardening).

Reads the latest ObservedFactExtractionRun + ConfiguredFactParserRun
per domain for a tenant and produces ONE canonical ReconstructedBusinessFact
per business rule, with:

  - relationship_to_config (8 values, harmonized across verticals)
  - consistency (consistent / contradictory / context_dependent / undetermined)
  - quality_flags (extraction issues detected at reconstruction time)
  - onboarding_class (SAFE / NEEDS_CONFIRMATION / DO_NOT_PROPOSE)

Hardening applied at the reconstruction boundary (no re-extraction):

  - qualification: capture_rate recomputed as
      |asked_convs ∩ answered_convs| / |asked_convs|
    Volunteered stays SEPARATE.
  - FAQ: only BUSINESS_FAQ observed facts feed the diff; the 83%
    operational-noise carve is respected (already enforced upstream
    by Ship C's extractor but re-asserted here as a safeguard).
  - pricing: rows with malformed pricing_basis or missing basis are
    marked ONTOLOGY_OR_EXTRACTION_ISSUE + DO_NOT_PROPOSE. NOT
    silently dropped — they surface so the operator can see them.
  - qualification lead-source heuristic: for a known set of
    Thumbtack/Yelp-populated fields (bedrooms, bathrooms,
    square_footage, phone_number, service_type, address/location),
    a configured entry with low ask_rate + low volunteer_rate is
    marked LIKELY_LEAD_SOURCE_PROVIDED rather than CONFIGURED_NOT_OBSERVED.
    This is a heuristic — v2 will parse actual lead payloads from
    EntityLink metadata.
  - cross-vertical dedup: rules that appear in both service_scope
    and pricing (e.g. "$50 late cancellation fee") are consolidated
    into a single canonical fact with combined provenance.

Never majority-votes contradictory observed behavior — a rule that
shows agents saying 19 EXTRA_CHARGE + 7 INCLUDED + 1 EXCLUDED stays
CONTRADICTORY_OBSERVED_BEHAVIOR + NEEDS_OWNER_CONFIRMATION.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone

from apps.conversations.models import (
    ConfiguredBusinessFact, ConfiguredFactParserRun,
    ObservedBusinessFact, ObservedFactExtractionRun,
    ReconstructedBusinessFact, TenantConfigSnapshot,
    UnifiedBusinessReconstructionRun,
)

logger = logging.getLogger(__name__)


RECONSTRUCTION_VERSION = 'business-reconstruction-v1'


# Thresholds
MIN_SUPPORT_SAFE = 5
MIN_SUPPORT_INSUFFICIENT = 3
DOMINANT_MAJORITY = 0.60
VARIABLE_MINORITY_SHARE = 0.20
LEAD_SOURCE_MAX_ASK_RATE = 0.15
LEAD_SOURCE_MAX_VOLUNTEER_RATE = 0.15


# Qualification fields that Thumbtack/Yelp cleaning-lead forms
# commonly pre-populate. Heuristic — safer than assuming an
# execution gap when these fields aren't asked in chat.
LEAD_SOURCE_FIELDS = frozenset({
    'bedrooms', 'bathrooms', 'square_footage', 'phone_number',
    'service_type', 'location', 'address', 'name',
    'preferred_date',   # some platforms capture this
})


# Pricing basis values that count as VALID for reconstruction. Rows
# with a value outside this set (like 'price_range' — a fact_type,
# not a basis) get flagged ONTOLOGY_OR_EXTRACTION_ISSUE.
VALID_PRICING_BASES = frozenset({
    'flat_job', 'hourly_per_cleaner', 'hourly_team',
    'addon_flat', 'addon_hourly',
    'discount_price', 'original_price', 'unknown',
})


# Cross-vertical dedup rules — scope items whose observed facts on
# the scope side ALSO appear as pricing facts (fees). Consolidated
# under `cross_vertical` domain.
CROSS_VERTICAL_FEE_ITEMS = frozenset({
    'late_cancellation_fee', 'additional_hourly_fee',
    'satisfaction_guarantee',
})


def build_reconstruction(
    *, tenant_external_id: str,
) -> Optional[UnifiedBusinessReconstructionRun]:
    """Assemble a reconstruction for `tenant_external_id`. Returns
    None if there's no snapshot for the tenant."""
    snap = (
        TenantConfigSnapshot.objects.filter(
            source_system='leadbridge',
            tenant_external_id=tenant_external_id,
        ).order_by('-created_at').first()
    )
    if snap is None:
        return None

    inputs = _resolve_input_runs(snap)
    run = UnifiedBusinessReconstructionRun.objects.create(
        org=snap.org,
        tenant_external_id=tenant_external_id,
        snapshot=snap,
        reconstruction_version=RECONSTRUCTION_VERSION,
        status=UnifiedBusinessReconstructionRun.Status.RUNNING,
        started_at=timezone.now(),
        input_pricing_run=inputs['pricing_obs'],
        input_qualification_run=inputs['qualification_obs'],
        input_faq_run=inputs['faq_obs'],
        input_service_scope_run=inputs['service_scope_obs'],
        input_pricing_parser=inputs['pricing_cfg'],
        input_qualification_parser=inputs['qualification_cfg'],
        input_faq_parser=inputs['faq_cfg'],
        input_service_scope_parser=inputs['service_scope_cfg'],
    )
    logger.info(
        f'reconstruction: run_id={run.id} started tenant={tenant_external_id}'
    )
    try:
        facts_emitted = _reconstruct_all(run=run, inputs=inputs)
    except Exception as exc:
        run.status = UnifiedBusinessReconstructionRun.Status.FAILED
        run.error_message = str(exc)[:2000]
        run.completed_at = timezone.now()
        run.save()
        logger.exception(
            'reconstruction: run_id=%s failed: %s', run.id, exc,
        )
        raise

    run.status = UnifiedBusinessReconstructionRun.Status.COMPLETED
    run.completed_at = timezone.now()
    run.facts_emitted = facts_emitted
    run.stats_json = _summarize_stats(run)
    run.save()
    logger.info(
        f'reconstruction: run_id={run.id} completed; '
        f'facts={facts_emitted}'
    )
    return run


def _resolve_input_runs(snap) -> dict:
    """Latest completed observed extraction + configured parser run
    per domain."""
    out = {}
    for domain, key in [
        ('pricing', 'pricing'),
        ('qualification', 'qualification'),
        ('faq', 'faq'),
        ('service_scope', 'service_scope'),
    ]:
        out[f'{key}_obs'] = (
            ObservedFactExtractionRun.objects.filter(
                org=snap.org, domain=domain, status='completed',
            ).order_by('-created_at').first()
        )
        out[f'{key}_cfg'] = (
            ConfiguredFactParserRun.objects.filter(
                snapshot=snap, domain=domain, status='completed',
            ).order_by('-created_at').first()
        )
    return out


def _reconstruct_all(*, run, inputs: dict) -> int:
    """Emit ReconstructedBusinessFact rows across all domains + the
    cross_vertical consolidation. Returns total facts written."""
    written = 0
    written += _reconstruct_pricing(
        run=run, obs_run=inputs['pricing_obs'],
        cfg_run=inputs['pricing_cfg'],
    )
    written += _reconstruct_qualification(
        run=run, obs_run=inputs['qualification_obs'],
        cfg_run=inputs['qualification_cfg'],
    )
    written += _reconstruct_faq(
        run=run, obs_run=inputs['faq_obs'],
        cfg_run=inputs['faq_cfg'],
    )
    written += _reconstruct_service_scope(
        run=run, obs_run=inputs['service_scope_obs'],
        cfg_run=inputs['service_scope_cfg'],
    )
    return written


# --------------------------------------------------------------------
# Domain reconstructors
# --------------------------------------------------------------------


def _reconstruct_pricing(*, run, obs_run, cfg_run) -> int:
    """Deterministic pricing matcher (P4, 2026-08-21).

    Replaces the old subject_key_hash join with a compatibility-based
    candidate search per the 2026-08-21 reviewer directive. Emits the
    four pricing-specific verdict values (MATCH, DIFFERS_FROM_CONFIG,
    INSUFFICIENT_CONTEXT_TO_COMPARE, VARIABLE_CONTEXT_DEPENDENT) plus
    OBSERVED_NOT_CONFIGURED / CONFIGURED_NOT_OBSERVED /
    INSUFFICIENT_EVIDENCE / ONTOLOGY_OR_EXTRACTION_ISSUE from the
    legacy generic set.

    Matcher logic lives in pricing_matcher.py so it is unit-testable
    without spinning up the whole reconstruction pipeline.
    """
    from apps.conversations.reconstruction.pricing_matcher import (
        MatchInputs, match_all,
    )

    if obs_run is None and cfg_run is None:
        return 0
    observed = list(
        ObservedBusinessFact.objects.filter(
            extraction_run=obs_run, domain='pricing',
        )
    ) if obs_run else []
    configured = list(
        ConfiguredBusinessFact.objects.filter(
            parser_run=cfg_run, domain='pricing',
        )
    ) if cfg_run else []

    written = 0

    # Ontology-or-extraction-issue rows short-circuit before matching:
    # a fact with a malformed / missing pricing_basis has no valid
    # subject to reason about, and we don't want to leak it into any
    # candidate lookup on the matcher side.
    matchable_observed: list[ObservedBusinessFact] = []
    for obs in observed:
        quality_flags = _pricing_quality_flags(obs)
        if 'malformed_pricing_basis' in quality_flags or 'missing_pricing_basis' in quality_flags:
            observed_value = _pricing_observed_summary(obs.value_json or {})
            _persist(
                run=run, domain='pricing',
                observed=obs, configured=None,
                observed_value=observed_value,
                relationship=ReconstructedBusinessFact.RelationshipToConfig.ONTOLOGY_OR_EXTRACTION_ISSUE,
                consistency=ReconstructedBusinessFact.Consistency.UNDETERMINED,
                quality_flags=quality_flags,
                onboarding_class=ReconstructedBusinessFact.OnboardingClass.DO_NOT_PROPOSE,
                onboarding_rationale=(
                    'pricing_basis is malformed or missing; refuse '
                    'to propose until extractor produces a valid value'
                ),
                support_n=obs.support_n or 0,
            )
            written += 1
            continue
        matchable_observed.append(obs)

    outcomes, orphaned_cfg = match_all(
        MatchInputs(observed_facts=matchable_observed,
                    configured_facts=configured),
    )

    for obs, outcome in outcomes:
        observed_value = _pricing_observed_summary(obs.value_json or {})
        # Enrich observed_value with matcher provenance so the audit
        # can render "why this verdict" without re-running the matcher.
        observed_value['matcher'] = {
            'verdict': outcome.verdict,
            'rationale': outcome.rationale,
            'candidate_configured_fact_ids': outcome.candidate_configured_fact_ids,
            'matched_configured_fact_id': outcome.matched_configured_fact_id,
            'missing_observed_dimensions': outcome.missing_observed_dimensions,
            'price_comparison': outcome.price_comparison,
        }
        matched_cfg = None
        if outcome.matched_configured_fact_id:
            for c in configured:
                if str(c.id) == outcome.matched_configured_fact_id:
                    matched_cfg = c
                    break
        onboarding_class, rationale = _pricing_onboarding_class(
            relationship=outcome.verdict,
            consistency=outcome.consistency,
            support=obs.support_n or 0,
            quality_flags=[],
        )
        _persist(
            run=run, domain='pricing',
            observed=obs, configured=matched_cfg,
            observed_value=observed_value,
            relationship=outcome.verdict,
            consistency=outcome.consistency,
            quality_flags=[],
            onboarding_class=onboarding_class,
            onboarding_rationale=rationale,
            support_n=obs.support_n or 0,
        )
        written += 1

    # Configured-only pricing facts (no observed counterpart).
    for cfg_row in orphaned_cfg:
        _persist(
            run=run, domain='pricing',
            observed=None, configured=cfg_row,
            observed_value={},
            relationship=ReconstructedBusinessFact.RelationshipToConfig.CONFIGURED_NOT_OBSERVED,
            consistency=ReconstructedBusinessFact.Consistency.UNDETERMINED,
            quality_flags=[],
            onboarding_class=ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION,
            onboarding_rationale=(
                'configured pricing entry not observed in agent '
                'conversations; may be stale or applied outside chat'
            ),
            support_n=0,
        )
        written += 1
    return written


def _reconstruct_qualification(*, run, obs_run, cfg_run) -> int:
    if obs_run is None and cfg_run is None:
        return 0
    observed = list(
        ObservedBusinessFact.objects.filter(
            extraction_run=obs_run, domain='qualification',
        )
    ) if obs_run else []
    configured = list(
        ConfiguredBusinessFact.objects.filter(
            parser_run=cfg_run, domain='qualification',
        )
    ) if cfg_run else []
    cfg_by_hash = {c.subject_key_hash: c for c in configured}
    obs_by_hash: dict[str, dict] = {}
    for o in observed:
        d = obs_by_hash.setdefault(o.subject_key_hash, {
            'subject_key': o.subject_key_json,
            'question_asked': None,
            'answer_provided': None,
            'volunteered_before_question': None,
        })
        d[o.fact_type] = o

    written = 0
    handled: set[str] = set()

    for sha, cfg_row in cfg_by_hash.items():
        entry = obs_by_hash.get(sha)
        rebuilt = _qualification_rebuild(
            cfg_row=cfg_row, obs_entry=entry,
        )
        _persist(
            run=run, domain='qualification',
            observed=(entry or {}).get('question_asked'),
            configured=cfg_row,
            observed_value=rebuilt['observed_value'],
            relationship=rebuilt['relationship'],
            consistency=rebuilt['consistency'],
            quality_flags=rebuilt['quality_flags'],
            onboarding_class=rebuilt['onboarding_class'],
            onboarding_rationale=rebuilt['rationale'],
            support_n=rebuilt['support_n'],
            extra_observed_ids=rebuilt['extra_observed_ids'],
        )
        handled.add(sha)
        written += 1

    for sha, entry in obs_by_hash.items():
        if sha in handled:
            continue
        rebuilt = _qualification_rebuild(cfg_row=None, obs_entry=entry)
        _persist(
            run=run, domain='qualification',
            observed=entry.get('question_asked'),
            configured=None,
            observed_value=rebuilt['observed_value'],
            relationship=rebuilt['relationship'],
            consistency=rebuilt['consistency'],
            quality_flags=rebuilt['quality_flags'],
            onboarding_class=rebuilt['onboarding_class'],
            onboarding_rationale=rebuilt['rationale'],
            support_n=rebuilt['support_n'],
            extra_observed_ids=rebuilt['extra_observed_ids'],
        )
        written += 1
    return written


def _qualification_rebuild(*, cfg_row, obs_entry: Optional[dict]) -> dict:
    """Recompute correct capture_rate + apply lead-source heuristic."""
    quality_flags: list = []
    ask = (obs_entry or {}).get('question_asked') if obs_entry else None
    answer = (obs_entry or {}).get('answer_provided') if obs_entry else None
    vol = (obs_entry or {}).get('volunteered_before_question') if obs_entry else None

    asked_ids = set(
        ask.evidence_conversation_ids or []
    ) if ask else set()
    answered_ids = set(
        answer.evidence_conversation_ids or []
    ) if answer else set()
    volunteered_ids = set(
        vol.evidence_conversation_ids or []
    ) if vol else set()

    # HARDENING FIX: capture_rate = |asked ∩ answered| / |asked|
    #   (never > 1.0). Old buggy formula was |answered| / |asked|.
    asked_and_answered = asked_ids & answered_ids
    corrected_capture_rate: Optional[float] = None
    if asked_ids:
        corrected_capture_rate = len(asked_and_answered) / len(asked_ids)

    ask_rate = None
    volunteer_rate = None
    total_processed = None
    if ask is not None:
        v = ask.value_json or {}
        ask_rate = v.get('ask_rate')
        total_processed = v.get('eligible_conversations')
    if vol is not None:
        vv = vol.value_json or {}
        volunteer_rate = vv.get('volunteer_rate')
        if total_processed is None:
            total_processed = vv.get('eligible_conversations')

    # Detect old capture_rate > 1 as a quality flag
    if answer is not None:
        prev = (answer.value_json or {}).get('capture_rate')
        if prev is not None and prev > 1.001:
            quality_flags.append('legacy_capture_rate_over_1_detected')

    observed_value = {
        'ask_rate': ask_rate,
        'volunteer_rate': volunteer_rate,
        'corrected_capture_rate': corrected_capture_rate,
        'asked_n': len(asked_ids),
        'answered_via_paired_ask_n': len(asked_and_answered),
        'answered_n_total': len(answered_ids),
        'volunteered_n': len(volunteered_ids),
        'eligible_conversations': total_processed,
    }

    subject_key = (
        (obs_entry and obs_entry.get('subject_key'))
        or (cfg_row and cfg_row.subject_key_json)
        or {}
    )
    field = subject_key.get('field')
    is_lead_source_field = field in LEAD_SOURCE_FIELDS
    support_n = len(asked_ids | answered_ids | volunteered_ids)

    # Relationship classification
    relationship = None
    consistency = ReconstructedBusinessFact.Consistency.UNDETERMINED

    if cfg_row is not None:
        # Configured field present.
        low_engagement = (
            (ask_rate is None or ask_rate < LEAD_SOURCE_MAX_ASK_RATE)
            and (
                volunteer_rate is None
                or volunteer_rate < LEAD_SOURCE_MAX_VOLUNTEER_RATE
            )
        )
        if is_lead_source_field and low_engagement:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.LIKELY_LEAD_SOURCE_PROVIDED
            consistency = ReconstructedBusinessFact.Consistency.UNDETERMINED
            quality_flags.append('lead_source_heuristic_applied')
        elif support_n == 0:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.CONFIGURED_NOT_OBSERVED
        elif ask_rate is not None and ask_rate >= 0.15 and (
            corrected_capture_rate or 0
        ) >= 0.50:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.CONFIRMED_BY_BEHAVIOR
            consistency = ReconstructedBusinessFact.Consistency.CONSISTENT
        elif support_n < MIN_SUPPORT_INSUFFICIENT:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.INSUFFICIENT_EVIDENCE
        else:
            # weak observation vs configured
            relationship = ReconstructedBusinessFact.RelationshipToConfig.INSUFFICIENT_EVIDENCE
    else:
        # Observed-only
        if support_n >= MIN_SUPPORT_SAFE and (
            (ask_rate or 0) >= 0.05
            or (volunteer_rate or 0) >= 0.10
        ):
            relationship = ReconstructedBusinessFact.RelationshipToConfig.OBSERVED_NOT_CONFIGURED
        else:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.INSUFFICIENT_EVIDENCE

    # Onboarding
    if relationship == ReconstructedBusinessFact.RelationshipToConfig.LIKELY_LEAD_SOURCE_PROVIDED:
        onboarding_class = ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION
        rationale = (
            'configured field with low chat ask/volunteer rates AND '
            'the field is one Thumbtack/Yelp typically pre-populates; '
            'do not treat as an execution gap until lead-source '
            'payload check confirms'
        )
    elif relationship == ReconstructedBusinessFact.RelationshipToConfig.CONFIRMED_BY_BEHAVIOR:
        onboarding_class = ReconstructedBusinessFact.OnboardingClass.SAFE_TO_PROPOSE
        rationale = 'configured + observed strongly'
    elif relationship == ReconstructedBusinessFact.RelationshipToConfig.OBSERVED_NOT_CONFIGURED:
        onboarding_class = ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION
        rationale = f'{support_n} conversations show this field, no configured entry'
    elif relationship == ReconstructedBusinessFact.RelationshipToConfig.CONFIGURED_NOT_OBSERVED:
        onboarding_class = ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION
        rationale = 'configured field with no observation at all'
    else:
        onboarding_class = ReconstructedBusinessFact.OnboardingClass.DO_NOT_PROPOSE
        rationale = 'insufficient evidence for a reliable proposal'

    extra_ids: list = []
    if answer is not None:
        extra_ids.append(str(answer.id))
    if vol is not None:
        extra_ids.append(str(vol.id))

    return {
        'observed_value': observed_value,
        'relationship': relationship,
        'consistency': consistency,
        'quality_flags': quality_flags,
        'onboarding_class': onboarding_class,
        'rationale': rationale,
        'support_n': support_n,
        'extra_observed_ids': extra_ids,
    }


def _reconstruct_faq(*, run, obs_run, cfg_run) -> int:
    if obs_run is None and cfg_run is None:
        return 0
    # Observed side: ONLY customer_question fact_type (Ship C already
    # filtered out TRANSACTIONAL_OPERATION events).
    observed = list(
        ObservedBusinessFact.objects.filter(
            extraction_run=obs_run, domain='faq',
            fact_type='customer_question',
        )
    ) if obs_run else []
    configured = list(
        ConfiguredBusinessFact.objects.filter(
            parser_run=cfg_run, domain='faq',
            fact_type='configured_faq',
        )
    ) if cfg_run else []
    obs_by_hash = {o.subject_key_hash: o for o in observed}
    cfg_by_hash = {c.subject_key_hash: c for c in configured}
    written = 0
    handled: set[str] = set()

    for sha, cfg_row in cfg_by_hash.items():
        obs = obs_by_hash.get(sha)
        support = obs.support_n if obs else 0
        observed_value = obs.value_json if obs else {}
        if obs is None:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.CONFIGURED_NOT_OBSERVED
            consistency = ReconstructedBusinessFact.Consistency.UNDETERMINED
            oc = ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION
            rat = (
                'configured FAQ with no customer question in corpus '
                '(evergreen coverage OR stale entry)'
            )
        elif support >= MIN_SUPPORT_SAFE:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.CONFIRMED_BY_BEHAVIOR
            consistency = ReconstructedBusinessFact.Consistency.CONSISTENT
            oc = ReconstructedBusinessFact.OnboardingClass.SAFE_TO_PROPOSE
            rat = f'configured FAQ + {support} customer questions in corpus'
        else:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.INSUFFICIENT_EVIDENCE
            consistency = ReconstructedBusinessFact.Consistency.UNDETERMINED
            oc = ReconstructedBusinessFact.OnboardingClass.DO_NOT_PROPOSE
            rat = f'configured FAQ but only {support} customer questions'
        _persist(
            run=run, domain='faq',
            observed=obs, configured=cfg_row,
            observed_value=observed_value,
            relationship=relationship, consistency=consistency,
            quality_flags=[],
            onboarding_class=oc, onboarding_rationale=rat,
            support_n=support,
        )
        handled.add(sha)
        written += 1

    for sha, obs in obs_by_hash.items():
        if sha in handled:
            continue
        support = obs.support_n
        if support >= MIN_SUPPORT_SAFE:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.OBSERVED_NOT_CONFIGURED
            consistency = ReconstructedBusinessFact.Consistency.CONSISTENT
            oc = ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION
            rat = (
                f'{support} conversations ask about {obs.subject_key_json}; '
                'no configured FAQ answer — real coverage gap'
            )
        else:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.INSUFFICIENT_EVIDENCE
            consistency = ReconstructedBusinessFact.Consistency.UNDETERMINED
            oc = ReconstructedBusinessFact.OnboardingClass.DO_NOT_PROPOSE
            rat = (
                f'observed FAQ topic with only {support} support; '
                'below onboarding floor'
            )
        _persist(
            run=run, domain='faq',
            observed=obs, configured=None,
            observed_value=obs.value_json,
            relationship=relationship, consistency=consistency,
            quality_flags=[],
            onboarding_class=oc, onboarding_rationale=rat,
            support_n=support,
        )
        written += 1
    return written


def _reconstruct_service_scope(*, run, obs_run, cfg_run) -> int:
    if obs_run is None and cfg_run is None:
        return 0
    # Observed side: ONLY agent_scope_statement.
    observed = list(
        ObservedBusinessFact.objects.filter(
            extraction_run=obs_run, domain='service_scope',
            fact_type='agent_scope_statement',
        )
    ) if obs_run else []
    configured = list(
        ConfiguredBusinessFact.objects.filter(
            parser_run=cfg_run, domain='service_scope',
            fact_type='configured_scope',
        )
    ) if cfg_run else []
    obs_by_hash = {o.subject_key_hash: o for o in observed}
    cfg_by_hash = {c.subject_key_hash: c for c in configured}
    written = 0
    handled: set[str] = set()

    for sha, cfg_row in cfg_by_hash.items():
        obs = obs_by_hash.get(sha)
        support = obs.support_n if obs else 0
        if obs is None:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.CONFIGURED_NOT_OBSERVED
            consistency = ReconstructedBusinessFact.Consistency.UNDETERMINED
            oc = ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION
            rat = (
                f'configured scope ({cfg_row.value_json.get("relationship")}) '
                'has no agent statements in the corpus'
            )
            observed_value = {}
        else:
            observed_value = obs.value_json or {}
            dist = observed_value.get('relationship_distribution') or {}
            total = sum(dist.values()) if dist else 0
            dominant = None
            if total > 0:
                dominant_key, dominant_count = max(
                    dist.items(), key=lambda kv: kv[1],
                )
                if dominant_count / total >= DOMINANT_MAJORITY:
                    dominant = dominant_key
            materially_variable = (
                sum(1 for v in dist.values() if v / max(total, 1) >= VARIABLE_MINORITY_SHARE) >= 2
            ) if total > 0 else False
            cfg_rel = (cfg_row.value_json or {}).get('relationship')
            if materially_variable:
                relationship = ReconstructedBusinessFact.RelationshipToConfig.CONTRADICTORY_OBSERVED_BEHAVIOR
                consistency = ReconstructedBusinessFact.Consistency.CONTRADICTORY
                oc = ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION
                rat = (
                    f'agents contradict each other: {dist}. '
                    f'Configured says {cfg_rel}. Do NOT auto-propose; '
                    'business owner must reconcile.'
                )
            elif support < MIN_SUPPORT_INSUFFICIENT:
                relationship = ReconstructedBusinessFact.RelationshipToConfig.INSUFFICIENT_EVIDENCE
                consistency = ReconstructedBusinessFact.Consistency.UNDETERMINED
                oc = ReconstructedBusinessFact.OnboardingClass.DO_NOT_PROPOSE
                rat = f'configured + observed but support {support} < floor'
            elif dominant == cfg_rel:
                relationship = ReconstructedBusinessFact.RelationshipToConfig.CONFIRMED_BY_BEHAVIOR
                consistency = ReconstructedBusinessFact.Consistency.CONSISTENT
                oc = ReconstructedBusinessFact.OnboardingClass.SAFE_TO_PROPOSE
                rat = (
                    f'configured={cfg_rel} matches dominant '
                    f'observed ({dist})'
                )
            elif dominant is not None and dominant != cfg_rel:
                relationship = ReconstructedBusinessFact.RelationshipToConfig.CONTRADICTORY_OBSERVED_BEHAVIOR
                consistency = ReconstructedBusinessFact.Consistency.CONTRADICTORY
                oc = ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION
                rat = (
                    f'configured={cfg_rel} but agents dominantly say '
                    f'{dominant} ({dist})'
                )
            else:
                relationship = ReconstructedBusinessFact.RelationshipToConfig.INSUFFICIENT_EVIDENCE
                consistency = ReconstructedBusinessFact.Consistency.UNDETERMINED
                oc = ReconstructedBusinessFact.OnboardingClass.DO_NOT_PROPOSE
                rat = (
                    f'observed distribution {dist} has no dominant '
                    f'majority; configured says {cfg_rel}'
                )
        _persist(
            run=run, domain='service_scope',
            observed=obs, configured=cfg_row,
            observed_value=observed_value,
            relationship=relationship, consistency=consistency,
            quality_flags=[],
            onboarding_class=oc, onboarding_rationale=rat,
            support_n=support,
        )
        handled.add(sha)
        written += 1

    for sha, obs in obs_by_hash.items():
        if sha in handled:
            continue
        support = obs.support_n
        observed_value = obs.value_json or {}
        dist = observed_value.get('relationship_distribution') or {}
        total = sum(dist.values()) if dist else 0
        materially_variable = (
            sum(1 for v in dist.values() if v / max(total, 1) >= VARIABLE_MINORITY_SHARE) >= 2
        ) if total > 0 else False
        if materially_variable:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.CONTRADICTORY_OBSERVED_BEHAVIOR
            consistency = ReconstructedBusinessFact.Consistency.CONTRADICTORY
            oc = ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION
            rat = (
                f'agents contradict each other on '
                f'{obs.subject_key_json}: {dist}. Not configured. '
                'Owner reconciliation required.'
            )
        elif support >= MIN_SUPPORT_SAFE:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.OBSERVED_NOT_CONFIGURED
            consistency = ReconstructedBusinessFact.Consistency.CONSISTENT
            oc = ReconstructedBusinessFact.OnboardingClass.SAFE_TO_PROPOSE
            rat = (
                f'{support} consistent agent statements about '
                f'{obs.subject_key_json}: {dist} — undocumented policy'
            )
        else:
            relationship = ReconstructedBusinessFact.RelationshipToConfig.INSUFFICIENT_EVIDENCE
            consistency = ReconstructedBusinessFact.Consistency.UNDETERMINED
            oc = ReconstructedBusinessFact.OnboardingClass.DO_NOT_PROPOSE
            rat = f'observed scope with only {support} support'
        _persist(
            run=run, domain='service_scope',
            observed=obs, configured=None,
            observed_value=observed_value,
            relationship=relationship, consistency=consistency,
            quality_flags=[],
            onboarding_class=oc, onboarding_rationale=rat,
            support_n=support,
        )
        written += 1
    return written


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------


def _pricing_quality_flags(obs: ObservedBusinessFact) -> list[str]:
    flags: list[str] = []
    basis = (obs.subject_key_json or {}).get('pricing_basis')
    if basis is None:
        flags.append('missing_pricing_basis')
    elif basis not in VALID_PRICING_BASES:
        flags.append('malformed_pricing_basis')
    return flags


def _pricing_observed_summary(payload: dict) -> dict:
    """Compact pricing observed_value that goes on the reconstructed row."""
    stats = payload.get('amount_stats') or {}
    return {
        'currency': payload.get('currency', 'USD'),
        'amount_stats': stats,
        'observed_attributes': payload.get('observed_attributes') or {},
        'sample_quotes': (payload.get('quotes_sample') or [])[:5],
    }


def _configured_amount(value_json: dict) -> Optional[float]:
    v = value_json or {}
    a = v.get('amount')
    if a is not None:
        try:
            return float(a)
        except (TypeError, ValueError):
            return None
    mn = v.get('min_amount')
    mx = v.get('max_amount')
    if mn is not None and mx is not None:
        try:
            return (float(mn) + float(mx)) / 2.0
        except (TypeError, ValueError):
            return None
    return None


def _pricing_onboarding_class(
    *, relationship, consistency, support: int,
    quality_flags: list[str],
) -> tuple:
    if quality_flags:
        return (
            ReconstructedBusinessFact.OnboardingClass.DO_NOT_PROPOSE,
            f'quality_flags present: {quality_flags}',
        )
    if support < MIN_SUPPORT_INSUFFICIENT:
        return (
            ReconstructedBusinessFact.OnboardingClass.DO_NOT_PROPOSE,
            f'support {support} below floor {MIN_SUPPORT_INSUFFICIENT}',
        )
    # New pricing verdicts (P1 dual-write) + legacy generic names.
    RTC = ReconstructedBusinessFact.RelationshipToConfig
    if relationship in (RTC.MATCH, RTC.CONFIRMED_BY_BEHAVIOR):
        return (
            ReconstructedBusinessFact.OnboardingClass.SAFE_TO_PROPOSE,
            f'observed distribution confirms configured amount (support {support})',
        )
    if relationship == RTC.INSUFFICIENT_CONTEXT_TO_COMPARE:
        return (
            ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION,
            'observed conversations lack the dimensions needed to '
            'match a specific configured pricing rule',
        )
    if relationship == RTC.VARIABLE_CONTEXT_DEPENDENT:
        return (
            ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION,
            'observed price distribution is materially heterogeneous; '
            'a single configured rule cannot capture the variance',
        )
    if consistency == ReconstructedBusinessFact.Consistency.CONTEXT_DEPENDENT:
        return (
            ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION,
            'observed pricing distribution is materially context-dependent',
        )
    if support >= MIN_SUPPORT_SAFE and relationship == RTC.OBSERVED_NOT_CONFIGURED:
        return (
            ReconstructedBusinessFact.OnboardingClass.SAFE_TO_PROPOSE,
            f'consistent observed rule with {support} conversations; '
            'no compatible configured entry',
        )
    if relationship == RTC.DIFFERS_FROM_CONFIG:
        return (
            ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION,
            'observed pricing sits outside the compatible configured '
            'rule\'s tolerance — owner should reconcile',
        )
    return (
        ReconstructedBusinessFact.OnboardingClass.NEEDS_OWNER_CONFIRMATION,
        'observed evidence does not confirm configured amount',
    )


def _persist(
    *,
    run,
    domain: str,
    observed: Optional[ObservedBusinessFact],
    configured: Optional[ConfiguredBusinessFact],
    observed_value: dict,
    relationship: str,
    consistency: str,
    quality_flags: list,
    onboarding_class: str,
    onboarding_rationale: str,
    support_n: int,
    extra_observed_ids: Optional[list] = None,
) -> None:
    subject_key = (
        (observed and observed.subject_key_json)
        or (configured and configured.subject_key_json)
        or {}
    )
    subject_hash = hashlib.sha256(
        json.dumps(subject_key, sort_keys=True).encode('utf-8')
    ).hexdigest()
    observed_ids = []
    if observed is not None:
        observed_ids.append(str(observed.id))
    if extra_observed_ids:
        observed_ids.extend(extra_observed_ids)
    configured_ids = [str(configured.id)] if configured else []
    ev_conv_ids: list = []
    if observed is not None:
        ev_conv_ids = list(observed.evidence_conversation_ids or [])[:20]
    ev_turn_ids: list = []
    if observed is not None:
        ev_turn_ids = list(observed.evidence_turn_ids or [])[:20]
    ReconstructedBusinessFact.objects.create(
        reconstruction_run=run,
        domain=domain,
        canonical_subject_json=subject_key,
        canonical_subject_hash=subject_hash,
        observed_value_json=observed_value or {},
        configured_equivalent_json=(
            configured.value_json if configured else {}
        ),
        support_n=support_n,
        aggregate_confidence=(
            observed.aggregate_confidence if observed else 0.0
        ),
        consistency=consistency,
        relationship_to_config=relationship,
        quality_flags=quality_flags,
        onboarding_class=onboarding_class,
        onboarding_rationale=onboarding_rationale,
        evidence_conversation_ids=ev_conv_ids,
        evidence_turn_ids=ev_turn_ids,
        source_observed_fact_ids=observed_ids,
        source_configured_fact_ids=configured_ids,
    )


def _summarize_stats(run: UnifiedBusinessReconstructionRun) -> dict:
    from django.db.models import Count
    facts = ReconstructedBusinessFact.objects.filter(
        reconstruction_run=run,
    )
    by_domain = dict(
        facts.values('domain').annotate(n=Count('id')).values_list('domain', 'n')
    )
    by_relationship = dict(
        facts.values('relationship_to_config')
        .annotate(n=Count('id'))
        .values_list('relationship_to_config', 'n')
    )
    by_onboarding = dict(
        facts.values('onboarding_class')
        .annotate(n=Count('id'))
        .values_list('onboarding_class', 'n')
    )
    return {
        'by_domain': by_domain,
        'by_relationship_to_config': by_relationship,
        'by_onboarding_class': by_onboarding,
        'total_facts': facts.count(),
    }
