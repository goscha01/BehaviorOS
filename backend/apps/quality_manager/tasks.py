"""Celery task for running Quality Manager.

Idempotent: reusing the same (org, reconstruction_run, qm_version)
returns the existing run without re-executing dimensions.
"""

from __future__ import annotations

import logging
from typing import Optional

from celery import shared_task


logger = logging.getLogger(__name__)


@shared_task(
    name='apps.quality_manager.tasks.run_quality_manager_task',
    bind=True,
    autoretry_for=(),
    acks_late=True,
)
def run_quality_manager_task(
    self,
    reconstruction_run_id: str,
    qm_version: str = 'qm-v1',
    dimensions: Optional[list[str]] = None,
) -> dict:
    from apps.conversations.models import (
        UnifiedBusinessReconstructionRun as _URun,
    )
    from apps.quality_manager.engine import (
        create_or_reuse_run, run_quality_manager,
    )

    try:
        recon = _URun.objects.get(pk=reconstruction_run_id)
    except _URun.DoesNotExist:
        return {'error': f'reconstruction_run {reconstruction_run_id} not found'}

    run, created = create_or_reuse_run(
        recon, qm_version=qm_version, dimensions=dimensions,
    )
    if run.status == 'completed':
        return {
            'run_id': str(run.id),
            'status': 'completed',
            'created': False,
            'note': 'existing completed run reused',
        }

    logger.info(
        'quality-manager: run_id=%s reconstruction_run=%s qm_version=%s '
        'dimensions=%s started (created=%s)',
        run.id, reconstruction_run_id, qm_version,
        run.dimensions_enabled_json, created,
    )
    result = run_quality_manager(run)
    logger.info(
        'quality-manager: run_id=%s completed status=%s conversations=%s',
        result.id, result.status, result.conversations_evaluated,
    )
    return {
        'run_id': str(result.id),
        'status': result.status,
        'created': created,
        'conversations_evaluated': result.conversations_evaluated,
        'stats': result.stats_json,
    }
