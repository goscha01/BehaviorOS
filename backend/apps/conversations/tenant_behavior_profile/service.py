"""TenantBehaviorProfile v1 assembly + approval.

Contract:

  effective_tenant_behavior
      = default_template
      + approved business_rule_overrides
      + approved custom_business_rules
      + approved communication_overrides

Approvals are additive-only. Approving a new override creates a NEW
TenantBehaviorProfile row with an incremented profile_version — never
a mutation. Consumers (LB + Callio) read the latest profile.

Discovery ≠ approval ≠ application:
  - Discovery: ReconstructedBusinessFact + CommunicationProfileDiff
    computed automatically. Not consumed by runtime.
  - Approval: this module. Owner-triggered per item. Persisted here.
  - Application: LB/Callio poll the effective profile at runtime.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Organization
from apps.conversations.communication_profile.default_profile import (
    DEFAULT_PROFILE_VERSION,
)
from apps.conversations.models import (
    CommunicationProfileDiff, CommunicationProfileRun,
    ReconstructedBusinessFact, TenantBehaviorProfile,
    UnifiedBusinessReconstructionRun,
)

logger = logging.getLogger(__name__)


BASE_TEMPLATE_VERSION = DEFAULT_PROFILE_VERSION


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_latest_profile(*, tenant_external_id: str) -> Optional[TenantBehaviorProfile]:
    return (
        TenantBehaviorProfile.objects
        .filter(tenant_external_id=tenant_external_id)
        .order_by('-profile_version').first()
    )


def get_or_bootstrap_profile(
    *, org: Organization, tenant_external_id: str,
    reconstruction_run: Optional[UnifiedBusinessReconstructionRun] = None,
    communication_profile_run: Optional[CommunicationProfileRun] = None,
) -> TenantBehaviorProfile:
    """Return the current profile for the tenant; create v1 (empty
    overrides) if none exists yet. This is the base row future approvals
    increment."""
    existing = get_latest_profile(tenant_external_id=tenant_external_id)
    if existing:
        return existing
    now = timezone.now()
    tbp = TenantBehaviorProfile.objects.create(
        org=org,
        tenant_external_id=tenant_external_id,
        base_template_version=BASE_TEMPLATE_VERSION,
        profile_version=1,
        business_rule_overrides=[],
        custom_business_rules=[],
        communication_overrides=[],
        reconstruction_run_id=(
            reconstruction_run.id if reconstruction_run else None
        ),
        communication_profile_run_id=(
            communication_profile_run.id
            if communication_profile_run else None
        ),
        generated_at=now,
        approved_at=None,
    )
    return tbp


def render_effective_profile(
    *, tenant_external_id: str,
) -> dict:
    """Runtime contract shape that LB and Callio consume.

    Shape:
      {
        "tenant_external_id": "...",
        "profile_version": 3,
        "base_template_version": "leadbridge-playbook-v1",
        "authoritative_business_owner": "leadbridge",  # explicit
        "business_rule_overrides": [...],   # LB consumes these
        "custom_business_rules": [...],     # LB consumes these
        "communication_profile": {           # Callio consumes this
            "response_style": {...},
            "pricing_communication": {...},
            "qualification_style": {...},
            "booking_style": {...},
            "objection_style": {...},
            "tone": {...},
            "diff_summary": {"different_count": N,
                             "business_specific_count": M},
        },
        "generated_at": "...",
        "approved_at": "..."
      }
    """
    tbp = get_latest_profile(tenant_external_id=tenant_external_id)
    if tbp is None:
        return {
            'tenant_external_id': tenant_external_id,
            'profile_version': 0,
            'base_template_version': BASE_TEMPLATE_VERSION,
            'authoritative_business_owner': 'leadbridge',
            'business_rule_overrides': [],
            'custom_business_rules': [],
            'communication_profile': None,
            'generated_at': None,
            'approved_at': None,
        }
    comm_profile = _compose_communication_profile(tbp)
    return {
        'tenant_external_id': tbp.tenant_external_id,
        'profile_version': tbp.profile_version,
        'base_template_version': tbp.base_template_version,
        'authoritative_business_owner': 'leadbridge',
        'business_rule_overrides': tbp.business_rule_overrides or [],
        'custom_business_rules': tbp.custom_business_rules or [],
        'communication_profile': comm_profile,
        'generated_at': tbp.generated_at.isoformat() if tbp.generated_at else None,
        'approved_at': tbp.approved_at.isoformat() if tbp.approved_at else None,
        'reconstruction_run_id': (
            str(tbp.reconstruction_run_id)
            if tbp.reconstruction_run_id else None
        ),
        'communication_profile_run_id': (
            str(tbp.communication_profile_run_id)
            if tbp.communication_profile_run_id else None
        ),
    }


def _compose_communication_profile(tbp: TenantBehaviorProfile) -> dict:
    """The communication_profile slot Callio consumes. Contains ONLY
    approved overrides — Callio blends against its own defaults."""
    approved = tbp.communication_overrides or []
    out: dict = {'overrides': approved, 'summary': {}}
    counts = {}
    for item in approved:
        dim = item.get('dimension', 'unknown')
        counts[dim] = counts.get(dim, 0) + 1
    out['summary']['approved_overrides_count'] = len(approved)
    out['summary']['approved_dimensions'] = list(counts.keys())
    return out


# ---------------------------------------------------------------------------
# Approvals — additive, one item at a time
# ---------------------------------------------------------------------------

def approve_communication_diff(
    *, tenant_external_id: str, diff_id: str,
    approved_by: str = 'owner',
    edited_payload: Optional[dict] = None,
) -> TenantBehaviorProfile:
    """Persist owner's approval of one CommunicationProfileDiff row.
    Creates a new TenantBehaviorProfile row with the added override.
    Marks the diff row's review_state.

    If `edited_payload` is provided, use it verbatim instead of the
    diff's proposed_override — represents owner-in-the-loop editing.
    """
    with transaction.atomic():
        diff = (
            CommunicationProfileDiff.objects
            .select_for_update()
            .select_related('run', 'run__org')
            .get(pk=diff_id)
        )
        run = diff.run
        if run.tenant_external_id != tenant_external_id:
            raise ValueError(
                f'diff {diff_id} belongs to tenant '
                f'{run.tenant_external_id}, not {tenant_external_id}'
            )
        payload = (edited_payload
                   if edited_payload is not None
                   else diff.proposed_override)
        current = get_latest_profile(tenant_external_id=tenant_external_id)
        base_business = list((current.business_rule_overrides
                              if current else []) or [])
        base_custom = list((current.custom_business_rules
                            if current else []) or [])
        base_comm = list((current.communication_overrides
                          if current else []) or [])

        new_item = {
            'id': str(uuid.uuid4()),
            'source': 'communication_diff',
            'source_id': str(diff.id),
            'dimension': diff.dimension,
            'category': diff.category,
            'default_value': diff.default_value,
            'observed_value': diff.observed_value,
            'support_n': diff.support_n,
            'payload': payload,
            'approved_at': timezone.now().isoformat(),
            'approved_by': approved_by,
            'was_edited': edited_payload is not None,
        }
        # Idempotency: replace previous approval of same dimension.
        base_comm = [
            i for i in base_comm
            if i.get('dimension') != diff.dimension
        ]
        base_comm.append(new_item)

        now = timezone.now()
        tbp = TenantBehaviorProfile.objects.create(
            org=run.org,
            tenant_external_id=tenant_external_id,
            base_template_version=(
                current.base_template_version
                if current else BASE_TEMPLATE_VERSION
            ),
            profile_version=(
                (current.profile_version if current else 0) + 1
            ),
            business_rule_overrides=base_business,
            custom_business_rules=base_custom,
            communication_overrides=base_comm,
            reconstruction_run_id=(
                current.reconstruction_run_id if current else None
            ),
            communication_profile_run_id=run.id,
            generated_at=(
                current.generated_at if current else now
            ),
            approved_at=now,
        )
        diff.review_state = (
            CommunicationProfileDiff.ReviewState.EDITED
            if edited_payload is not None
            else CommunicationProfileDiff.ReviewState.ACCEPTED
        )
        diff.reviewed_at = now
        if edited_payload is not None:
            diff.owner_edited_payload = edited_payload
        diff.save(update_fields=[
            'review_state', 'reviewed_at', 'owner_edited_payload',
            'updated_at',
        ])
        return tbp


def dismiss_communication_diff(*, diff_id: str) -> CommunicationProfileDiff:
    """Owner chose to keep the default. Marks the diff dismissed. No
    profile version bump (approvals are additive; dismissals are not
    an override)."""
    diff = CommunicationProfileDiff.objects.get(pk=diff_id)
    diff.review_state = CommunicationProfileDiff.ReviewState.DISMISSED
    diff.reviewed_at = timezone.now()
    diff.save(update_fields=['review_state', 'reviewed_at', 'updated_at'])
    return diff


def approve_reconstructed_fact(
    *, tenant_external_id: str, fact_id: str,
    approved_by: str = 'owner',
    edited_payload: Optional[dict] = None,
) -> TenantBehaviorProfile:
    """Persist owner's approval of one ReconstructedBusinessFact as a
    business-rule override. Creates a new TenantBehaviorProfile version.

    We accept ALL onboarding classes here — the classifier's role is to
    surface candidates in the owner UI, not to gate approval. The owner
    is the authoritative approver.
    """
    with transaction.atomic():
        fact = (
            ReconstructedBusinessFact.objects
            .select_related('reconstruction_run', 'reconstruction_run__org')
            .get(pk=fact_id)
        )
        run = fact.reconstruction_run
        if run.tenant_external_id != tenant_external_id:
            raise ValueError(
                f'fact {fact_id} belongs to tenant '
                f'{run.tenant_external_id}, not {tenant_external_id}'
            )
        current = get_latest_profile(tenant_external_id=tenant_external_id)
        base_business = list((current.business_rule_overrides
                              if current else []) or [])
        base_custom = list((current.custom_business_rules
                            if current else []) or [])
        base_comm = list((current.communication_overrides
                          if current else []) or [])

        payload = edited_payload if edited_payload is not None else {
            'domain': fact.domain,
            'canonical_subject': fact.canonical_subject_json,
            'observed_value': fact.observed_value_json,
            'configured_equivalent': fact.configured_equivalent_json,
            'support_n': fact.support_n,
            'relationship_to_config': fact.relationship_to_config,
        }
        new_item = {
            'id': str(uuid.uuid4()),
            'source': 'reconstructed_fact',
            'source_id': str(fact.id),
            'domain': fact.domain,
            'canonical_subject': fact.canonical_subject_json,
            'onboarding_class': fact.onboarding_class,
            'relationship_to_config': fact.relationship_to_config,
            'payload': payload,
            'approved_at': timezone.now().isoformat(),
            'approved_by': approved_by,
            'was_edited': edited_payload is not None,
        }
        base_business = [
            i for i in base_business if i.get('source_id') != str(fact.id)
        ]
        base_business.append(new_item)
        now = timezone.now()
        tbp = TenantBehaviorProfile.objects.create(
            org=run.org,
            tenant_external_id=tenant_external_id,
            base_template_version=(
                current.base_template_version
                if current else BASE_TEMPLATE_VERSION
            ),
            profile_version=(
                (current.profile_version if current else 0) + 1
            ),
            business_rule_overrides=base_business,
            custom_business_rules=base_custom,
            communication_overrides=base_comm,
            reconstruction_run_id=run.id,
            communication_profile_run_id=(
                current.communication_profile_run_id
                if current else None
            ),
            generated_at=(
                current.generated_at if current else now
            ),
            approved_at=now,
        )
        return tbp


def add_custom_business_rule(
    *, org: Organization, tenant_external_id: str,
    section: str, rule_text: str,
    approved_by: str = 'owner',
) -> TenantBehaviorProfile:
    """Owner-authored freeform business rule (no reconstruction source).
    Written into custom_business_rules and persisted as new profile
    version."""
    current = get_latest_profile(tenant_external_id=tenant_external_id)
    base_business = list((current.business_rule_overrides
                          if current else []) or [])
    base_custom = list((current.custom_business_rules
                        if current else []) or [])
    base_comm = list((current.communication_overrides
                      if current else []) or [])
    new_item = {
        'id': str(uuid.uuid4()),
        'source': 'custom_rule',
        'section': section,
        'rule_text': rule_text,
        'approved_at': timezone.now().isoformat(),
        'approved_by': approved_by,
    }
    base_custom.append(new_item)
    now = timezone.now()
    return TenantBehaviorProfile.objects.create(
        org=org,
        tenant_external_id=tenant_external_id,
        base_template_version=(
            current.base_template_version
            if current else BASE_TEMPLATE_VERSION
        ),
        profile_version=(
            (current.profile_version if current else 0) + 1
        ),
        business_rule_overrides=base_business,
        custom_business_rules=base_custom,
        communication_overrides=base_comm,
        reconstruction_run_id=(
            current.reconstruction_run_id if current else None
        ),
        communication_profile_run_id=(
            current.communication_profile_run_id if current else None
        ),
        generated_at=(current.generated_at if current else now),
        approved_at=now,
    )


# ---------------------------------------------------------------------------
# Owner-review payload — one endpoint's worth of shape
# ---------------------------------------------------------------------------

def build_owner_review_payload(
    *, tenant_external_id: str,
) -> dict:
    """Compose the payload the LB AI Insights UI renders under the
    "Communication" tab + the "Business rules" tab.

    Business rules: latest reconstruction's SAFE_TO_PROPOSE +
    NEEDS_OWNER_CONFIRMATION + CONTRADICTORY buckets.
    Communication: latest comm profile's DIFFERENT / BUSINESS_SPECIFIC /
    CONFLICTING diffs.

    Approved items are marked so the UI can render them as "already
    accepted" rather than fresh review cards.
    """
    business = _business_rules_payload(tenant_external_id)
    communication = _communication_payload(tenant_external_id)
    return {
        'tenant_external_id': tenant_external_id,
        'business': business,
        'communication': communication,
    }


def _business_rules_payload(tenant_external_id: str) -> dict:
    run = (
        UnifiedBusinessReconstructionRun.objects
        .filter(tenant_external_id=tenant_external_id, status='completed')
        .order_by('-created_at').first()
    )
    if run is None:
        return {'note': 'no reconstruction run yet', 'items': []}
    facts = list(
        ReconstructedBusinessFact.objects
        .filter(reconstruction_run=run)
        .exclude(onboarding_class='DO_NOT_PROPOSE')
        .order_by('-support_n')
    )
    current = get_latest_profile(tenant_external_id=tenant_external_id)
    approved_source_ids = {
        item.get('source_id')
        for item in (current.business_rule_overrides
                     if current else []) or []
        if item.get('source') == 'reconstructed_fact'
    }
    items = []
    for f in facts:
        items.append({
            'fact_id': str(f.id),
            'domain': f.domain,
            'canonical_subject': f.canonical_subject_json,
            'observed_value': f.observed_value_json,
            'configured_equivalent': f.configured_equivalent_json,
            'support_n': f.support_n,
            'relationship_to_config': f.relationship_to_config,
            'consistency': f.consistency,
            'onboarding_class': f.onboarding_class,
            'rationale': f.onboarding_rationale,
            'already_approved': str(f.id) in approved_source_ids,
        })
    return {
        'reconstruction_run_id': str(run.id),
        'items': items,
    }


def _communication_payload(tenant_external_id: str) -> dict:
    run = (
        CommunicationProfileRun.objects
        .filter(
            tenant_external_id=tenant_external_id,
            status=CommunicationProfileRun.Status.COMPLETED,
        )
        .order_by('-created_at').first()
    )
    if run is None:
        return {'note': 'no communication profile run yet', 'items': []}
    diffs = list(
        CommunicationProfileDiff.objects
        .filter(run=run)
        .exclude(category=CommunicationProfileDiff.Category.SAME_AS_DEFAULT)
        .order_by('dimension')
    )
    current = get_latest_profile(tenant_external_id=tenant_external_id)
    approved_diff_ids = {
        item.get('source_id')
        for item in (current.communication_overrides
                     if current else []) or []
        if item.get('source') == 'communication_diff'
    }
    items = []
    for d in diffs:
        items.append({
            'diff_id': str(d.id),
            'dimension': d.dimension,
            'category': d.category,
            'default': d.default_value,
            'observed': d.observed_value,
            'support_n': d.support_n,
            'confidence': d.confidence,
            'narrative': d.narrative,
            'proposed_override': d.proposed_override,
            'evidence_conversation_ids': d.evidence_conversation_ids,
            'review_state': d.review_state,
            'already_approved': str(d.id) in approved_diff_ids,
        })
    return {
        'communication_profile_run_id': str(run.id),
        'items': items,
    }
