"""Compose LB + SF outcome fetches into an OutcomeSnapshot row.

The service is deliberately independent of the resolver. Callers pass
outcomes fetchers explicitly so tests can substitute the in-memory
backends and the production wiring stays declarative.

Rerun semantics:
- Each call to `resolve_and_persist()` creates a NEW OutcomeSnapshot
  row unless one with the exact same `captured_at` already exists
  (bounded by the unique constraint on the model). Callers typically
  want a fresh row each run — the resolver truncates `captured_at` to
  the second, so re-running within the same second is a no-op.

Merge semantics for SF entities:
- SF may return outcomes for multiple entity types (customer, opportunity,
  job, appointment). We fold them into a single OutcomeSnapshot by
  taking the strongest signal per field:
      opportunity.status → sf_opportunity_status
      opportunity.booked / job.completed / customer.recurring, etc.
- Revenue prefers customer-level rollup > opportunity revenue > job revenue,
  since a customer with 3 recurring jobs has higher revenue than any
  single job.
- If multiple entities of the same type return conflicting values, the
  most recently updated / highest-ID wins (SF's ordering is stable).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from django.utils import timezone

from apps.conversations.models import (
    Conversation,
    EntityLink,
    OutcomeSnapshot,
    TargetSystem,
    TargetType,
)
from apps.conversations.outcomes.base import (
    BaseLeadBridgeOutcomeFetcher,
    BaseServiceFlowOutcomeFetcher,
    LeadBridgeOutcome,
    ServiceFlowOutcome,
)

logger = logging.getLogger(__name__)


@dataclass
class OutcomeResolutionResult:
    snapshot: Optional[OutcomeSnapshot]
    lb_leads_queried: int = 0
    sf_entities_queried: int = 0
    lb_outcomes_returned: int = 0
    sf_outcomes_returned: int = 0
    created: bool = False


def resolve_and_persist(
    conversation: Conversation,
    *,
    lb_fetcher: Optional[BaseLeadBridgeOutcomeFetcher] = None,
    sf_fetcher: Optional[BaseServiceFlowOutcomeFetcher] = None,
) -> OutcomeResolutionResult:
    """Fetch fresh outcomes and persist as a new snapshot row."""
    links = list(conversation.entity_links.all())

    lb_ids = [
        link.target_id
        for link in links
        if link.target_system == TargetSystem.LEADBRIDGE
        and link.target_type == TargetType.LEAD
    ]
    sf_entities = [
        (link.target_type, link.target_id)
        for link in links
        if link.target_system == TargetSystem.SERVICEFLOW
    ]

    lb_outcomes: list[LeadBridgeOutcome] = []
    sf_outcomes: list[ServiceFlowOutcome] = []

    if lb_fetcher and lb_ids:
        lb_outcomes = lb_fetcher.fetch_outcomes(lb_ids)
    if sf_fetcher and sf_entities:
        sf_outcomes = sf_fetcher.fetch_outcomes(sf_entities)

    if not lb_outcomes and not sf_outcomes and not links:
        # No linked entities and no results — no snapshot to persist.
        # Return an empty result so the caller can update ingestion_status
        # to OUTCOMES_RESOLVED and continue.
        return OutcomeResolutionResult(snapshot=None)

    lb_folded = _fold_lb(lb_outcomes)
    sf_folded = _fold_sf(sf_outcomes)

    # Truncate to seconds so re-running within the same second is a no-op
    # rather than an IntegrityError.
    captured_at = timezone.now().replace(microsecond=0)

    snapshot, created = OutcomeSnapshot.objects.get_or_create(
        conversation=conversation,
        captured_at=captured_at,
        defaults={
            'lb_status': lb_folded['status'],
            'lb_engaged': lb_folded['engaged'],
            'lb_booked': lb_folded['booked'],
            'lb_lost': lb_folded['lost'],
            'lb_cancelled': lb_folded['cancelled'],
            'sf_opportunity_status': sf_folded['status'],
            'sf_booked': sf_folded['booked'],
            'sf_completed': sf_folded['completed'],
            'sf_cancelled': sf_folded['cancelled'],
            'sf_revenue_cents': sf_folded['revenue_cents'],
            'sf_recurring': sf_folded['recurring'],
            'sf_job_count': sf_folded['job_count'],
            'source_payload': {
                'lb_outcomes': [o.raw for o in lb_outcomes],
                'sf_outcomes': [o.raw for o in sf_outcomes],
            },
            'metadata': {
                'lb_ids_queried': lb_ids,
                'sf_entities_queried': [
                    {'type': t, 'id': i} for (t, i) in sf_entities
                ],
            },
        },
    )

    return OutcomeResolutionResult(
        snapshot=snapshot,
        lb_leads_queried=len(lb_ids),
        sf_entities_queried=len(sf_entities),
        lb_outcomes_returned=len(lb_outcomes),
        sf_outcomes_returned=len(sf_outcomes),
        created=created,
    )


# ---------------------------------------------------------------------------
# Field-folding helpers
# ---------------------------------------------------------------------------


def _fold_lb(outcomes: list[LeadBridgeOutcome]) -> dict:
    """When a conversation is linked to multiple LB leads (rare — usually
    one), the latest / highest-ID wins for text status; boolean flags OR
    together (any lead booked → booked is True).
    """
    if not outcomes:
        return {
            'status': '',
            'engaged': None,
            'booked': None,
            'lost': None,
            'cancelled': None,
        }

    # Deterministic order — sort by lb_lead_id to make the "which status wins"
    # decision reproducible.
    ordered = sorted(outcomes, key=lambda o: o.lb_lead_id)
    return {
        'status': ordered[-1].status,  # last after sort — arbitrary but stable
        'engaged': _or_reduce(o.engaged for o in outcomes),
        'booked': _or_reduce(o.booked for o in outcomes),
        'lost': _or_reduce(o.lost for o in outcomes),
        'cancelled': _or_reduce(o.cancelled for o in outcomes),
    }


def _fold_sf(outcomes: list[ServiceFlowOutcome]) -> dict:
    """Precedence for text fields: opportunity > job > customer.
    Booleans OR-reduce across all entities.
    Revenue: max across entities (customer rollup dominates).
    """
    def _pick_status(preferred_types: list[str]) -> str:
        for etype in preferred_types:
            for o in outcomes:
                if o.sf_entity_type == etype and o.opportunity_status:
                    return o.opportunity_status
        return ''

    if not outcomes:
        return {
            'status': '',
            'booked': None,
            'completed': None,
            'cancelled': None,
            'revenue_cents': None,
            'recurring': None,
            'job_count': None,
        }

    return {
        'status': _pick_status([
            TargetType.OPPORTUNITY, TargetType.JOB, TargetType.CUSTOMER,
        ]),
        'booked': _or_reduce(o.booked for o in outcomes),
        'completed': _or_reduce(o.completed for o in outcomes),
        'cancelled': _or_reduce(o.cancelled for o in outcomes),
        'revenue_cents': _max_optional_int(o.revenue_cents for o in outcomes),
        'recurring': _or_reduce(o.recurring for o in outcomes),
        'job_count': _max_optional_int(o.job_count for o in outcomes),
    }


def _or_reduce(values) -> Optional[bool]:
    """Return True if any value is True; False if all seen are False; None
    if every value is None (i.e. no signal from any source)."""
    seen_any = False
    for v in values:
        if v is None:
            continue
        seen_any = True
        if v:
            return True
    return False if seen_any else None


def _max_optional_int(values) -> Optional[int]:
    picks = [v for v in values if v is not None]
    return max(picks) if picks else None


# EntityLink import needed by callers wanting to iterate links; re-exported
# for convenience.
__all__ = [
    'resolve_and_persist',
    'OutcomeResolutionResult',
    'EntityLink',
]
