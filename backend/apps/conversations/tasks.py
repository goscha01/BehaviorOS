"""Celery tasks for the conversations app.

Ship A.1: async execution wrapper for Pipeline 1D extraction runs.
The trigger HTTP endpoint creates an ObservedFactExtractionRun row
in PENDING state and enqueues the corresponding task. The task
transitions the run RUNNING → COMPLETED (or FAILED) and populates
counters / facts. Callers poll the status endpoint.

Also hosts the async ingestion task used by the tenant-bootstrap
orchestration endpoint (Pipeline 1A LB-anchored). Thin wrapper over
the existing LbAnchoredIngestionService — no behavioral change.

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
    name='apps.conversations.tasks.observed_service_scope_extraction_task',
    bind=True,
    autoretry_for=(),
    acks_late=True,
)
def observed_service_scope_extraction_task(
    self, run_id: str, model: str = 'gpt-4o-mini',
    limit: Optional[int] = None,
) -> dict:
    """Async execution of the Pipeline 1D service_scope extractor (Ship D)."""
    from apps.conversations.observed_config.service_scope.extractor import (
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
            run=run, llm_client=LearningLLMClient(),
            model=model, limit=limit,
        )
    except Exception as exc:
        run.status = ObservedFactExtractionRun.Status.FAILED
        run.error_message = str(exc)[:2000]
        run.completed_at = timezone.now()
        run.save()
        logger.exception(
            'observed_service_scope_extraction_task: run %s failed: %s',
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


@shared_task(
    name='apps.conversations.tasks.ingest_lb_anchored_corpus_task',
    bind=True,
    autoretry_for=(),
    acks_late=True,
)
def ingest_lb_anchored_corpus_task(
    self,
    org_id: str,
    lb_user_id: str,
    limit: Optional[int] = None,
    statuses: Optional[list] = None,
) -> dict:
    """Async Pipeline 1A LB-anchored ingestion.

    Thin wrapper over LbAnchoredIngestionService — identical control
    flow to the `import_lb_learning_dataset` management command. Exists
    so the tenant-bootstrap HTTP endpoint can return a task_id and let
    the worker do the long-running Sigcore phone-index build + per-lead
    hydration.
    """
    import uuid
    from collections import Counter, defaultdict

    from apps.accounts.models import Organization
    from apps.conversations.adapters.quo import QuoAdapter
    from apps.conversations.normalization.phone import normalize_e164
    from apps.conversations.services.lb_anchored_ingestion import (
        LB_ANCHOR_REASON_AMBIGUOUS,
        LB_ANCHOR_REASON_INVALID_PHONE,
        LB_ANCHOR_REASON_NO_CONVERSATION,
        LB_ANCHOR_REASON_NO_PHONE,
        LbAnchoredIngestionService,
    )
    from apps.conversations.services.lb_learning_client import (
        LbLearningClient,
    )
    from apps.conversations.services.sigcore_phone_index import (
        build_sigcore_phone_index,
    )

    try:
        org = Organization.objects.get(pk=org_id)
    except Organization.DoesNotExist:
        return {'error': f'org {org_id} not found'}

    import_run_id = f'lb-dataset-{uuid.uuid4()}'
    self.update_state(state='PROGRESS', meta={'stage': 'building_phone_index'})
    index = build_sigcore_phone_index()
    self.update_state(state='PROGRESS', meta={
        'stage': 'fetching_lb_leads',
        'sigcore_index_size': index.total_conversations,
        'sigcore_unique_phones': index.phones_with_conversation,
    })

    client = LbLearningClient(lb_user_id=lb_user_id)
    leads = list(client.iter_leads(statuses=statuses, max_leads=limit))
    if not leads:
        return {
            'org_id': str(org.id),
            'lb_user_id': lb_user_id,
            'import_run_id': import_run_id,
            'error': (
                'no LB leads returned — check LEADBRIDGE_LEARNING_URL, '
                'LEADBRIDGE_LEARNING_TOKEN, and lb_user_id'
            ),
        }

    leads_by_phone: dict = defaultdict(list)
    for lead in leads:
        phone = lead.customer_phone or lead.customer_phone_substitute
        e164 = normalize_e164(phone) if phone else None
        if e164:
            leads_by_phone[e164].append(lead.lead_id)
    ambiguous_phones = {p for p, ids in leads_by_phone.items() if len(ids) > 1}

    self.update_state(state='PROGRESS', meta={
        'stage': 'ingesting_leads',
        'sigcore_index_size': index.total_conversations,
        'sigcore_unique_phones': index.phones_with_conversation,
        'lb_leads_fetched': len(leads),
        'ambiguous_phones': len(ambiguous_phones),
    })

    service = LbAnchoredIngestionService(
        org=org, phone_index=index, quo_adapter=QuoAdapter(),
        import_run_id=import_run_id, lb_user_id=lb_user_id,
    )
    counters = {
        'leads_considered': 0,
        'included_learning_records': 0,
        'no_phone': 0,
        'invalid_phone': 0,
        'no_sigcore_conversation': 0,
        'ambiguous_multi_lead': 0,
        'errors': 0,
    }
    by_outcome_status: Counter = Counter()
    by_platform: Counter = Counter()
    per_reason: Counter = Counter()

    for i, lead in enumerate(leads):
        counters['leads_considered'] += 1
        phone = lead.customer_phone or lead.customer_phone_substitute
        e164 = normalize_e164(phone) if phone else None
        is_ambig = bool(e164 and e164 in ambiguous_phones)
        try:
            result = service.ingest_lead(lead, is_ambiguous=is_ambig)
        except Exception as exc:  # noqa: BLE001
            logger.exception('lead ingestion crashed: %s', lead.lead_id)
            counters['errors'] += 1
            per_reason['CRASH'] += 1
            continue

        if result.reason == LB_ANCHOR_REASON_NO_PHONE:
            counters['no_phone'] += 1
            per_reason['NO_PHONE'] += 1
        elif result.reason == LB_ANCHOR_REASON_INVALID_PHONE:
            counters['invalid_phone'] += 1
            per_reason['INVALID_PHONE'] += 1
        elif result.reason == LB_ANCHOR_REASON_NO_CONVERSATION:
            counters['no_sigcore_conversation'] += 1
            per_reason['NO_CONVERSATION'] += 1
        elif result.reason == LB_ANCHOR_REASON_AMBIGUOUS:
            counters['ambiguous_multi_lead'] += 1
            per_reason['AMBIGUOUS_MULTI_LEAD'] += 1
        elif not result.included:
            counters['errors'] += 1
            per_reason[result.reason or 'OTHER'] += 1
        else:
            counters['included_learning_records'] += 1
            by_outcome_status[lead.outcome.status] += 1
            by_platform[lead.platform or '(none)'] += 1

        if (i + 1) % 25 == 0:
            self.update_state(state='PROGRESS', meta={
                'stage': 'ingesting_leads',
                'processed': i + 1,
                'total': len(leads),
                'included_so_far': counters['included_learning_records'],
            })

    return {
        'org_id': str(org.id),
        'lb_user_id': lb_user_id,
        'import_run_id': import_run_id,
        'sigcore_index_size': index.total_conversations,
        'sigcore_unique_phones': index.phones_with_conversation,
        'lb_leads_fetched': len(leads),
        'ambiguous_phones': len(ambiguous_phones),
        'counters': counters,
        'by_outcome_status': dict(by_outcome_status),
        'by_platform': dict(by_platform),
        'per_reason': dict(per_reason),
    }
