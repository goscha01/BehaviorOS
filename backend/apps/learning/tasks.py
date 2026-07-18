"""Celery tasks for the learning engine.

Two families of tasks live here:

**Nightly pipeline** (long-running, one per org, fan-out from beat):
  - `run_nightly_learning_job_for_org`      — one org's full pipeline
  - `run_nightly_learning_job_for_all_orgs` — beat entrypoint, fan-out

**Promotion** (short, latency-sensitive, safe under concurrent workers):
  - `promote_evidence_events_task`       — one org, small batch
  - `promote_all_orgs_task`              — fan-out for beat

Fan-out keeps per-org failures isolated: one broken adapter for org A
doesn't stop org B's run. Same pattern for both families.

Concurrency safety on promotion: `promote_evidence_events` uses
SELECT FOR UPDATE SKIP LOCKED so multiple workers running the same task
in parallel each claim disjoint batches — no double-processing, no
starvation, no manual coordination.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

from apps.accounts.models import Organization
from apps.learning.models import LearningJob
from apps.learning.services.nightly import run_nightly_learning_job
from apps.learning.services.promotion import promote_evidence_events

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


# --- Promotion tasks ---------------------------------------------


# Default batch size for beat-driven promotion runs. Kept small during
# pilot so a runaway backlog doesn't monopolize a worker for minutes at
# a stretch. Overridable via `PROMOTION_TASK_DEFAULT_BATCH` setting.
_DEFAULT_PROMOTION_BATCH = 100


@shared_task(
    name='apps.learning.tasks.promote_evidence_events_task',
    bind=True,
    autoretry_for=(),  # Task-level retry doesn't help — per-event FAILED status handles it.
    acks_late=True,   # Redeliver on worker crash; the row locks ensure no double-processing.
)
def promote_evidence_events_task(self, org_id: str, limit: int | None = None) -> dict:
    """Promote pending EvidenceEvents for one org.

    Safe to run concurrently with other workers on the same org — the
    underlying service uses SELECT FOR UPDATE SKIP LOCKED. Beat may
    schedule this task before the previous run has completed without
    causing double-processing.
    """
    task_id = self.request.id or ''
    # Structured log line at task boundary — matched by promotion.run below
    # on the same task_id so Grafana can trace beat → queue → worker.
    logger.info(
        'promote_task.start task_id=%s org=%s limit=%s',
        task_id, org_id, limit,
    )

    try:
        org = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        logger.warning(
            'promote_task.skip task_id=%s org=%s reason=org_not_found',
            task_id, org_id,
        )
        return {'org_id': org_id, 'task_id': task_id, 'skipped': 'org_not_found'}

    effective_limit = limit if limit is not None else getattr(
        settings, 'PROMOTION_TASK_DEFAULT_BATCH', _DEFAULT_PROMOTION_BATCH,
    )
    result = promote_evidence_events(org=org, limit=effective_limit, task_id=task_id)

    logger.info(
        'promote_task.end task_id=%s org=%s scanned=%d promoted=%d skipped=%d failed=%d',
        task_id, org_id, result.scanned, result.promoted, result.skipped, result.failed,
    )
    return {
        'org_id': str(org.pk),
        'task_id': task_id,
        'scanned': result.scanned,
        'promoted': result.promoted,
        'skipped': result.skipped,
        'failed': result.failed,
        'skipped_by_reason': dict(result.skipped_by_reason),
    }


@shared_task(
    name='apps.learning.tasks.promote_all_orgs_task',
    bind=True,
)
def promote_all_orgs_task(self, limit: int | None = None) -> dict:
    """Fan out promotion tasks — one per org.

    Beat wires this on the 5-minute cadence. It only enqueues — the
    actual promotion happens in per-org tasks so one org's backlog
    doesn't block another's. Kept out of the beat's own execution
    thread for the same reason.

    Emits one log line per enqueued child with (parent_task_id, child_task_id,
    org) so Grafana can trace beat → fan-out → per-org worker in one query.
    """
    parent_task_id = self.request.id or ''
    org_ids = list(Organization.objects.values_list('id', flat=True))
    logger.info(
        'promote_fanout.start task_id=%s org_count=%d limit=%s',
        parent_task_id, len(org_ids), limit,
    )

    child_ids: list[str] = []
    for org_id in org_ids:
        r = promote_evidence_events_task.delay(str(org_id), limit)
        logger.info(
            'promote_fanout.enqueue parent_task_id=%s child_task_id=%s org=%s',
            parent_task_id, r.id, org_id,
        )
        child_ids.append(r.id)

    return {
        'parent_task_id': parent_task_id,
        'orgs_queued': len(org_ids),
        'limit_per_org': limit,
        'child_task_ids': child_ids,
    }
