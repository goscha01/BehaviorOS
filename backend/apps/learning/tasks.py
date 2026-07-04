"""Celery tasks for the learning engine.

Two tasks compose the nightly pipeline:

- `run_nightly_learning_job_for_org` — runs the full pipeline for one org.
- `run_nightly_learning_job_for_all_orgs` — fan-out entrypoint that beat
  schedules; queues one per-org task per active organization.

Fan-out keeps per-org failures isolated: one broken adapter for org A
doesn't stop org B's nightly run. When a real multi-tenant filter
(subscription tier, opt-in flag) becomes relevant, adjust the queryset
in `run_nightly_learning_job_for_all_orgs`.
"""

from __future__ import annotations

import logging

from celery import shared_task

from apps.accounts.models import Organization
from apps.learning.models import LearningJob
from apps.learning.services.nightly import run_nightly_learning_job

logger = logging.getLogger(__name__)


@shared_task(
    name='apps.learning.tasks.run_nightly_learning_job_for_org',
    bind=True,
    autoretry_for=(),  # Errors are captured on LearningJob; do NOT retry the whole job.
    acks_late=True,
)
def run_nightly_learning_job_for_org_task(self, org_id: str) -> dict:
    """Run one org's nightly pipeline. Returns a small summary dict for
    logs / dashboards."""
    try:
        org = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        logger.warning('Skipping missing org=%s', org_id)
        return {'org_id': org_id, 'skipped': 'org_not_found'}

    result = run_nightly_learning_job(
        org=org,
        triggered_by=LearningJob.TriggeredBy.SCHEDULE,
    )
    return {
        'org_id': str(org.pk),
        'job_id': result.job_id,
        'evidence_analyzed': result.evidence_analyzed,
        'suggestions_created': result.suggestions_created,
        'stopped_for_budget': result.stopped_for_budget,
        'error': result.error,
    }


@shared_task(name='apps.learning.tasks.run_nightly_learning_job_for_all_orgs')
def run_nightly_learning_job_for_all_orgs() -> dict:
    """Fan out one per-org task per active org.

    Beat calls this on the nightly schedule. It doesn't do work itself —
    just enqueues the per-org tasks. Beat should never block on
    long-running org loops.
    """
    org_ids = list(Organization.objects.values_list('id', flat=True))
    for org_id in org_ids:
        run_nightly_learning_job_for_org_task.delay(str(org_id))
    return {'orgs_queued': len(org_ids)}
