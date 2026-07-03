"""Suggestion lifecycle service — validates transitions and side effects.

Transitions:

    NEW ─┬─▶ UNDER_REVIEW ─┬─▶ APPROVED ──▶ IMPLEMENTED ──▶ MEASURED ──▶ ARCHIVED
         │                 │
         │                 └─▶ REJECTED (terminal until signature TTL)
         │
         └─▶ REJECTED       (skip review)
         └─▶ APPROVED       (quick-approve)
         └─▶ ARCHIVED       (dismiss without judgement)

    WATCHLIST ─▶ NEW (auto-promotion when support grows) OR ARCHIVED
    ARCHIVED, REJECTED are terminal.

Rules exist in code (not DB CHECK constraints) so we can adjust freely
during Phase 1 without migration churn. Every transition returns the
refreshed suggestion so the view can serialize the new state.

Side effects owned here (not the view):
- REJECTED transition writes a RejectedSuggestionSignature so the
  clustering pass suppresses the same pattern for LEARNING_REJECTION_TTL_DAYS.
- APPROVED transition persists publish_targets on the suggestion so a
  future publisher (LeadBridge Playbook, Callio voice rules, etc.)
  can pick up the intent without breaking the API contract.
- IMPLEMENTED transition captures publish_receipts (opaque JSON) —
  today this is set by the human; when a real publisher lands, it
  writes here after successful publication.
- MEASURED transition writes impact_json.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.learning.models import LearningSuggestion, RejectedSuggestionSignature


class TransitionError(ValueError):
    """Raised when an attempted status transition isn't allowed."""


# From-state → set of allowed to-states.
_ALLOWED: dict[str, frozenset[str]] = {
    LearningSuggestion.Status.NEW: frozenset({
        LearningSuggestion.Status.UNDER_REVIEW,
        LearningSuggestion.Status.APPROVED,
        LearningSuggestion.Status.REJECTED,
        LearningSuggestion.Status.ARCHIVED,
    }),
    LearningSuggestion.Status.UNDER_REVIEW: frozenset({
        LearningSuggestion.Status.APPROVED,
        LearningSuggestion.Status.REJECTED,
        LearningSuggestion.Status.ARCHIVED,
        LearningSuggestion.Status.NEW,  # unclaim
    }),
    LearningSuggestion.Status.APPROVED: frozenset({
        LearningSuggestion.Status.IMPLEMENTED,
        LearningSuggestion.Status.ARCHIVED,
    }),
    LearningSuggestion.Status.IMPLEMENTED: frozenset({
        LearningSuggestion.Status.MEASURED,
        LearningSuggestion.Status.ARCHIVED,
    }),
    LearningSuggestion.Status.MEASURED: frozenset({
        LearningSuggestion.Status.ARCHIVED,
    }),
    LearningSuggestion.Status.WATCHLIST: frozenset({
        LearningSuggestion.Status.NEW,
        LearningSuggestion.Status.ARCHIVED,
    }),
    LearningSuggestion.Status.REJECTED: frozenset(),  # terminal
    LearningSuggestion.Status.ARCHIVED: frozenset(),  # terminal
}


def can_transition(suggestion: LearningSuggestion, to_status: str) -> bool:
    return to_status in _ALLOWED.get(suggestion.status, frozenset())


def _require(suggestion: LearningSuggestion, to_status: str) -> None:
    if not can_transition(suggestion, to_status):
        raise TransitionError(
            f'Cannot transition {suggestion.status!r} → {to_status!r}'
        )


def _stamp_reviewer(suggestion: LearningSuggestion, user) -> None:
    if user and user.is_authenticated:
        suggestion.reviewed_by = user
    suggestion.reviewed_at = timezone.now()


@dataclass
class TransitionResult:
    suggestion: LearningSuggestion
    side_effects: dict[str, Any]


@transaction.atomic
def start_review(suggestion: LearningSuggestion, *, user) -> TransitionResult:
    _require(suggestion, LearningSuggestion.Status.UNDER_REVIEW)
    suggestion.status = LearningSuggestion.Status.UNDER_REVIEW
    _stamp_reviewer(suggestion, user)
    suggestion.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'updated_at'])
    return TransitionResult(suggestion=suggestion, side_effects={})


@transaction.atomic
def approve(
    suggestion: LearningSuggestion,
    *,
    user,
    note: str = '',
    publish_to: list[str] | None = None,
) -> TransitionResult:
    _require(suggestion, LearningSuggestion.Status.APPROVED)
    suggestion.status = LearningSuggestion.Status.APPROVED
    if publish_to is not None:
        suggestion.publish_targets = list(publish_to)
    if note:
        suggestion.review_note = note
    _stamp_reviewer(suggestion, user)
    suggestion.save(update_fields=[
        'status', 'publish_targets', 'review_note',
        'reviewed_by', 'reviewed_at', 'updated_at',
    ])
    return TransitionResult(
        suggestion=suggestion,
        side_effects={'publish_targets': suggestion.publish_targets},
    )


@transaction.atomic
def reject(
    suggestion: LearningSuggestion,
    *,
    user,
    reason: str,
) -> TransitionResult:
    """Reject a suggestion — reason is required. Writes a
    RejectedSuggestionSignature so the clustering pass won't
    re-surface the same pattern until the TTL expires.
    """
    if not reason or not reason.strip():
        raise TransitionError('Rejection reason is required.')
    _require(suggestion, LearningSuggestion.Status.REJECTED)

    suggestion.status = LearningSuggestion.Status.REJECTED
    suggestion.review_note = reason.strip()
    _stamp_reviewer(suggestion, user)
    suggestion.save(update_fields=[
        'status', 'review_note', 'reviewed_by', 'reviewed_at', 'updated_at',
    ])

    ttl_days = int(getattr(settings, 'LEARNING_REJECTION_TTL_DAYS', 90))
    tokens = (suggestion.fingerprint or '').split()
    signature, _created = RejectedSuggestionSignature.objects.update_or_create(
        org=suggestion.org,
        category=suggestion.category,
        signature=suggestion.fingerprint,
        defaults={
            'tokens': tokens,
            'rejected_suggestion': suggestion,
            'rejection_reason': reason.strip(),
            'rejected_by': user if user and user.is_authenticated else None,
            'expires_at': timezone.now() + timedelta(days=ttl_days),
        },
    )
    return TransitionResult(
        suggestion=suggestion,
        side_effects={'rejection_signature_id': str(signature.id)},
    )


@transaction.atomic
def mark_implemented(
    suggestion: LearningSuggestion,
    *,
    user,
    publish_receipts: dict[str, Any] | None = None,
) -> TransitionResult:
    """Move APPROVED → IMPLEMENTED. `publish_receipts` is opaque today
    (human-entered summary of what was published); when a real
    publisher ships, it'll write structured receipts here after
    successful writes to LeadBridge / Callio / etc."""
    _require(suggestion, LearningSuggestion.Status.IMPLEMENTED)
    suggestion.status = LearningSuggestion.Status.IMPLEMENTED
    if publish_receipts:
        # Store receipts inside synthesis_json so we don't need a schema
        # change every time we add a publisher.
        aj = dict(suggestion.synthesis_json or {})
        aj['publish_receipts'] = publish_receipts
        suggestion.synthesis_json = aj
    _stamp_reviewer(suggestion, user)
    suggestion.save(update_fields=[
        'status', 'synthesis_json', 'reviewed_by', 'reviewed_at', 'updated_at',
    ])
    return TransitionResult(suggestion=suggestion, side_effects={})


@transaction.atomic
def mark_measured(
    suggestion: LearningSuggestion,
    *,
    user,
    impact: dict[str, Any],
) -> TransitionResult:
    """IMPLEMENTED → MEASURED, with the before/after metrics captured."""
    _require(suggestion, LearningSuggestion.Status.MEASURED)
    if not isinstance(impact, dict) or not impact:
        raise TransitionError('impact must be a non-empty object.')
    suggestion.status = LearningSuggestion.Status.MEASURED
    suggestion.impact_json = impact
    _stamp_reviewer(suggestion, user)
    suggestion.save(update_fields=[
        'status', 'impact_json', 'reviewed_by', 'reviewed_at', 'updated_at',
    ])
    return TransitionResult(suggestion=suggestion, side_effects={})


@transaction.atomic
def archive(suggestion: LearningSuggestion, *, user) -> TransitionResult:
    _require(suggestion, LearningSuggestion.Status.ARCHIVED)
    suggestion.status = LearningSuggestion.Status.ARCHIVED
    _stamp_reviewer(suggestion, user)
    suggestion.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'updated_at'])
    return TransitionResult(suggestion=suggestion, side_effects={})
