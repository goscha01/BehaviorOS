"""Trends aggregation for the morning-brief dashboard.

Two windows drive the brief:

- "Today" — suggestions created since a floor timestamp (usually
  start-of-yesterday so a morning open covers overnight processing).
- "Recent" — a rolling window (default 30 days) for trend context.

We keep aggregations small and Python-side so the queries are fast on a
fresh Phase 1 database. When suggestion volume grows, promote heavier
rollups to a materialized view.

"Recurring issues" and "recurring opportunities" are derived from the
outcome distribution across a suggestion's supporting candidates:
- issues = mostly-negative outcomes (customers churned / lost)
- opportunities = mostly-positive outcomes (customers converted)
- consistency threshold keeps mixed-signal suggestions out of both
  columns so the brief doesn't lie about the pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db.models import Count, F, Q
from django.utils import timezone

from apps.accounts.models import Organization
from apps.learning.models import (
    CandidateRecommendation,
    LearningJob,
    LearningSuggestion,
)

# A suggestion counts as a "recurring issue" or "recurring opportunity"
# only when its supporting candidates lean this hard toward one outcome
# direction. Mixed-signal patterns stay off the brief so we don't
# mislead the reader about what customers are actually doing.
_OUTCOME_CONSISTENCY_FLOOR = 0.65

# Top-N results returned per section. Kept small so the brief stays scannable.
_SECTION_LIMIT = 5


@dataclass
class MorningBrief:
    generated_at: datetime
    window_days: int
    since: datetime
    last_job: dict | None
    new_suggestions_today: dict
    category_counts: dict[str, int]
    trends: dict


@dataclass
class TrendsResult:
    window_days: int
    since: datetime
    top_supported: list[dict] = field(default_factory=list)
    recurring_issues: list[dict] = field(default_factory=list)
    recurring_opportunities: list[dict] = field(default_factory=list)


class TrendsService:
    def __init__(self, org: Organization):
        self.org = org

    def morning_brief(self, window_days: int = 30) -> MorningBrief:
        now = timezone.now()
        # Start-of-yesterday so an early-morning open still covers the
        # overnight nightly job that ran a few hours ago.
        yesterday = (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        todays_qs = LearningSuggestion.objects.filter(
            org=self.org,
            created_at__gte=yesterday,
        ).exclude(status=LearningSuggestion.Status.WATCHLIST).order_by('-confidence', '-created_at')

        top_today = list(todays_qs[:_SECTION_LIMIT])
        todays_summary = {
            'count': todays_qs.count(),
            'since': yesterday.isoformat(),
            'top': [_suggestion_dict(s) for s in top_today],
        }

        last_job = (
            LearningJob.objects
            .filter(org=self.org)
            .exclude(status=LearningJob.Status.RUNNING)
            .order_by('-completed_at', '-created_at')
            .first()
        )

        trends = self.trends(window_days=window_days)

        return MorningBrief(
            generated_at=now,
            window_days=window_days,
            since=trends.since,
            last_job=_job_dict(last_job) if last_job else None,
            new_suggestions_today=todays_summary,
            category_counts=self._category_counts(since=trends.since),
            trends={
                'top_supported': trends.top_supported,
                'recurring_issues': trends.recurring_issues,
                'recurring_opportunities': trends.recurring_opportunities,
            },
        )

    def trends(self, window_days: int = 30) -> TrendsResult:
        since = timezone.now() - timedelta(days=window_days)
        base_qs = (
            LearningSuggestion.objects
            .filter(org=self.org, created_at__gte=since)
            .exclude(status__in=(
                LearningSuggestion.Status.WATCHLIST,
                LearningSuggestion.Status.ARCHIVED,
            ))
        )

        top_supported = list(
            base_qs.order_by('-supporting_count', '-confidence')[:_SECTION_LIMIT]
        )

        # Recurring issues / opportunities: need per-suggestion outcome
        # majority. Compute via one grouped query per direction so we
        # don't fan out to N queries.
        issue_ids = self._suggestion_ids_dominated_by(
            base_qs, direction='negative'
        )[:_SECTION_LIMIT]
        opportunity_ids = self._suggestion_ids_dominated_by(
            base_qs, direction='positive'
        )[:_SECTION_LIMIT]

        return TrendsResult(
            window_days=window_days,
            since=since,
            top_supported=[_suggestion_dict(s) for s in top_supported],
            recurring_issues=[
                _suggestion_dict(s)
                for s in base_qs.filter(id__in=issue_ids)
                .order_by('-supporting_count')
            ],
            recurring_opportunities=[
                _suggestion_dict(s)
                for s in base_qs.filter(id__in=opportunity_ids)
                .order_by('-supporting_count')
            ],
        )

    def _category_counts(self, since: datetime) -> dict[str, int]:
        rows = (
            LearningSuggestion.objects
            .filter(org=self.org, created_at__gte=since)
            .exclude(status__in=(
                LearningSuggestion.Status.WATCHLIST,
                LearningSuggestion.Status.ARCHIVED,
            ))
            .values('category')
            .annotate(n=Count('id'))
        )
        return {row['category']: row['n'] for row in rows}

    def _suggestion_ids_dominated_by(
        self, base_qs, *, direction: str
    ) -> list[str]:
        """Return suggestion IDs whose supporting candidates lean toward
        one outcome direction with consistency ≥ floor.

        Direction is 'positive' or 'negative'. We count matching vs total
        candidates per suggestion via one aggregation query, then filter
        in Python — the per-suggestion candidate counts are small.
        """
        rows = (
            CandidateRecommendation.objects
            .filter(suggestion__in=base_qs)
            .values('suggestion_id')
            .annotate(
                total=Count('id'),
                matches=Count('id', filter=Q(outcome_signal=direction)),
            )
        )
        winners = []
        for row in rows:
            if row['total'] == 0:
                continue
            ratio = row['matches'] / row['total']
            if ratio >= _OUTCOME_CONSISTENCY_FLOOR and row['matches'] >= 2:
                winners.append((row['matches'], row['suggestion_id']))
        winners.sort(reverse=True)  # highest-count first
        return [wid for _count, wid in winners]


def _suggestion_dict(s: LearningSuggestion) -> dict:
    return {
        'id': str(s.id),
        'title': s.title,
        'category': s.category,
        'status': s.status,
        'confidence': str(s.confidence),
        'supporting_count': s.supporting_count,
        'created_at': s.created_at.isoformat(),
    }


def _job_dict(j: LearningJob) -> dict:
    return {
        'id': str(j.id),
        'status': j.status,
        'started_at': j.started_at.isoformat() if j.started_at else None,
        'completed_at': j.completed_at.isoformat() if j.completed_at else None,
        'evidence_processed': j.evidence_processed,
        'suggestions_created': j.suggestions_created,
        'cost_usd': str(j.cost_usd),
    }
