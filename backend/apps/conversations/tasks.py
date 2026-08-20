"""Celery tasks for the conversations app.

Ship A.1: async execution wrapper for Pipeline 1D extraction runs.
The trigger HTTP endpoint creates an ObservedFactExtractionRun row
in PENDING state and enqueues the corresponding task. The task
transitions the run RUNNING → COMPLETED (or FAILED) and populates
counters / facts. Callers poll the status endpoint.

Idempotency:
  - The trigger endpoint refuses to create a second PENDING/RUNNING
    run for the same (org, corpus, domain, extractor_version) — the
    caller reuses the existing run's id.
  - The task itself is safe against Celery retries: it checks the
    run's status on entry; a COMPLETED run is a no-op.
  - The aggregator uses update_or_create so a partial run that got
    retried does not create duplicate ObservedBusinessFact rows on
    the same (extraction_run, domain, fact_type, subject_key_hash).
"""

from __future__ import annotations

import logging
from typing import Optional

from celery import shared_task
from django.utils import timezone

from apps.conversations.models import (
    ObservedBusinessFact, ObservedFactExtractionRun,
)

logger = logging.getLogger(__name__)


@shared_task(
    name='apps.conversations.tasks.observed_pricing_extraction_task',
    bind=True,
    autoretry_for=(),
    acks_late=True,
)
def observed_pricing_extraction_task(
    self, run_id: str, model: str = 'gpt-4o-mini',
    limit: Optional[int] = None,
) -> dict:
    """Async execution of the Pipeline 1D pricing extractor.

    The trigger endpoint has already created an
    ObservedFactExtractionRun in PENDING state. This task loads the
    run, executes the extractor, and updates the run to
    COMPLETED / FAILED. Never creates a second run row.
    """
    from apps.conversations.observed_config.pricing.extractor import (
        run_extraction_for_existing,
    )
    from apps.learning.services.llm_client import LearningLLMClient

    try:
        run = ObservedFactExtractionRun.objects.select_related(
            'org', 'corpus',
        ).get(pk=run_id)
    except ObservedFactExtractionRun.DoesNotExist:
        logger.warning(
            'observed_pricing_extraction_task: run %s not found', run_id,
        )
        return {'run_id': run_id, 'skipped': 'run_not_found'}

    if run.status in (
        ObservedFactExtractionRun.Status.COMPLETED,
        ObservedFactExtractionRun.Status.FAILED,
    ):
        logger.info(
            'observed_pricing_extraction_task: run %s already %s; '
            'no-op', run.id, run.status,
        )
        return {
            'run_id': str(run.id), 'status': run.status,
            'skipped': 'already_terminal',
        }

    try:
        run_extraction_for_existing(
            run=run,
            llm_client=LearningLLMClient(),
            model=model,
            limit=limit,
        )
    except Exception as exc:
        run.status = ObservedFactExtractionRun.Status.FAILED
        run.error_message = str(exc)[:2000]
        run.completed_at = timezone.now()
        run.save()
        logger.exception(
            'observed_pricing_extraction_task: run %s failed: %s',
            run.id, exc,
        )
        raise

    return {
        'run_id': str(run.id),
        'status': run.status,
        'conversations_processed': run.conversations_processed,
        'facts_emitted': run.facts_emitted,
        'llm_cost_usd': str(run.llm_cost_usd),
    }


@shared_task(
    name='apps.conversations.tasks.observed_faq_extraction_task',
    bind=True,
    autoretry_for=(),
    acks_late=True,
)
def observed_faq_extraction_task(
    self, run_id: str, model: str = 'gpt-4o-mini',
    limit: Optional[int] = None,
) -> dict:
    """Async execution of the Pipeline 1D FAQ extractor (Ship C)."""
    from apps.conversations.observed_config.faq.extractor import (
        run_extraction_for_existing,
    )
    from apps.learning.services.llm_client import LearningLLMClient
    try:
        run = ObservedFactExtractionRun.objects.select_related(
            'org', 'corpus',
        ).get(pk=run_id)
    except ObservedFactExtractionRun.DoesNotExist:
        return {'run_id': run_id, 'skipped': 'run_not_found'}
    if run.status in (
        ObservedFactExtractionRun.Status.COMPLETED,
        ObservedFactExtractionRun.Status.FAILED,
    ):
        return {
            'run_id': str(run.id), 'status': run.status,
            'skipped': 'already_terminal',
        }
    try:
        run_extraction_for_existing(
            run=run,
            llm_client=LearningLLMClient(),
            model=model,
            limit=limit,
        )
    except Exception as exc:
        run.status = ObservedFactExtractionRun.Status.FAILED
        run.error_message = str(exc)[:2000]
        run.completed_at = timezone.now()
        run.save()
        logger.exception(
            'observed_faq_extraction_task: run %s failed: %s',
            run.id, exc,
        )
        raise
    return {
        'run_id': str(run.id),
        'status': run.status,
        'conversations_processed': run.conversations_processed,
        'facts_emitted': run.facts_emitted,
        'llm_cost_usd': str(run.llm_cost_usd),
    }


@shared_task(
    name='apps.conversations.tasks.observed_qualification_extraction_task',
    bind=True,
    autoretry_for=(),
    acks_late=True,
)
def observed_qualification_extraction_task(
    self, run_id: str, model: str = 'gpt-4o-mini',
    limit: Optional[int] = None,
) -> dict:
    """Async execution of the Pipeline 1D qualification extractor
    (Ship B). Same shape as observed_pricing_extraction_task."""
    from apps.conversations.observed_config.qualification.extractor import (
        run_extraction_for_existing,
    )
    from apps.learning.services.llm_client import LearningLLMClient
    try:
        run = ObservedFactExtractionRun.objects.select_related(
            'org', 'corpus',
        ).get(pk=run_id)
    except ObservedFactExtractionRun.DoesNotExist:
        logger.warning(
            'observed_qualification_extraction_task: run %s not found',
            run_id,
        )
        return {'run_id': run_id, 'skipped': 'run_not_found'}
    if run.status in (
        ObservedFactExtractionRun.Status.COMPLETED,
        ObservedFactExtractionRun.Status.FAILED,
    ):
        return {
            'run_id': str(run.id), 'status': run.status,
            'skipped': 'already_terminal',
        }
    try:
        run_extraction_for_existing(
            run=run,
            llm_client=LearningLLMClient(),
            model=model,
            limit=limit,
        )
    except Exception as exc:
        run.status = ObservedFactExtractionRun.Status.FAILED
        run.error_message = str(exc)[:2000]
        run.completed_at = timezone.now()
        run.save()
        logger.exception(
            'observed_qualification_extraction_task: run %s failed: %s',
            run.id, exc,
        )
        raise
    return {
        'run_id': str(run.id),
        'status': run.status,
        'conversations_processed': run.conversations_processed,
        'facts_emitted': run.facts_emitted,
        'llm_cost_usd': str(run.llm_cost_usd),
    }
