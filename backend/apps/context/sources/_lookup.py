"""Shared evidence-lookup helper for Context Sources.

Each source system stores customer identity in a different `source_payload`
shape (LeadBridge: `conversation_id`, `lead.name`; Callio: `call_id`,
`lead.phone_last_4`; ServiceFlow: `customer_id`, `job_id`). Rather than
teach every Source that vocabulary, we centralize the query here — any
Source that wants prior-evidence matches for the current customer/lead/
conversation gets the same lookup semantics.

Design: OR every candidate JSON path together. A row matches if ANY known
identifier from the request lines up with ANY known identity path in the
row. Order-independent, cheap, and correct for Phase 1/2 volumes.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.learning.models import EvidenceInsight


def match_evidence(
    *,
    org,
    customer_id: str = '',
    lead_id: str = '',
    conversation_id: str = '',
) -> QuerySet[EvidenceInsight]:
    """Return EvidenceInsight rows in `org` whose payload references any of
    the given identifiers.

    Returns an empty QuerySet (not None) when nothing is given — Sources
    can safely `.exists()` / iterate without a nullcheck.
    """
    if org is None:
        return EvidenceInsight.objects.none()

    filters = Q()
    matched_something = False

    if customer_id:
        filters |= (
            Q(source_payload__customer_id=customer_id)
            | Q(source_payload__lead__customer_id=customer_id)
            | Q(source_payload__customer__id=customer_id)
        )
        matched_something = True

    if conversation_id:
        filters |= (
            Q(source_payload__conversation_id=conversation_id)
            | Q(external_id=conversation_id)
        )
        matched_something = True

    if lead_id:
        filters |= (
            Q(source_payload__lead_id=lead_id)
            | Q(source_payload__lead__id=lead_id)
            | Q(external_id=lead_id)
        )
        matched_something = True

    if not matched_something:
        return EvidenceInsight.objects.none()

    return (
        EvidenceInsight.objects
        .filter(org=org)
        .filter(filters)
        .order_by('-occurred_at', '-created_at')
    )
