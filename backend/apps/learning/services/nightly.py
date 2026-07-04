"""Nightly learning orchestrator — runs the full pipeline for one org.

Composes the pieces already built for Steps 2-4 into one workflow:

    LearningJob (RUNNING)
       ↓
    Ingest from every enabled source (EvidenceIngestionService)
       ↓
    Analyze the resume queue (EvidenceAnalysisService)
       ↓
    Cluster + synthesize (EvidenceClusteringService + SynthesisService)
       ↓
    Finalize (COMPLETED or PARTIAL)

Budget behavior is preserved: analysis or synthesis can hit the ceiling
mid-run and the job resumes on the next scheduled trigger. Nothing here
loops in a way that would ignore that — each stage checks the budget
and marks PARTIAL when needed.

Kept as a plain service (not a Celery task itself) so it can be called
from the beat task, a management command, or a test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime

from django.utils import timezone

from apps.accounts.models import Organization
from apps.learning.models import LearningJob
from apps.learning.services.analysis import EvidenceAnalysisService
from apps.learning.services.clustering import EvidenceClusteringService
from apps.learning.services.ingestion import EvidenceIngestionService, IngestionRun
from apps.learning.services.synthesis import SynthesisService

logger = logging.getLogger(__name__)


@dataclass
class NightlyResult:
    job_id: str
    ingestion: IngestionRun | None = None
    evidence_analyzed: int = 0
    evidence_failed: int = 0
    suggestions_created: int = 0
    stopped_for_budget: bool = False
    total_cost_usd: Decimal = field(default_factory=lambda: Decimal('0'))
    error: str = ''


def run_nightly_learning_job(
    org: Organization,
    *,
    triggered_by: str = LearningJob.TriggeredBy.SCHEDULE,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    ingestion_since: datetime | None = None,
) -> NightlyResult:
    """Create a LearningJob and run the full pipeline for one org."""
    job = LearningJob.objects.create(
        org=org,
        status=LearningJob.Status.RUNNING,
        triggered_by=triggered_by,
        started_at=timezone.now(),
        window_start=window_start,
        window_end=window_end,
    )
    result = NightlyResult(job_id=str(job.id))

    try:
        # Stage 1 — ingest.
        ingestion = EvidenceIngestionService(org=org, job=job)
        result.ingestion = ingestion.ingest_all_enabled(since=ingestion_since)
        logger.info(
            'Nightly ingestion complete for org=%s job=%s: created=%d updated=%d',
            org.pk, job.pk, result.ingestion.total_created, result.ingestion.total_updated,
        )

        # Stage 2 — analyze the resume queue (may include leftovers from
        # prior jobs whose analyzed_at is still NULL).
        analysis = EvidenceAnalysisService(org=org, job=job)
        analysis_result = analysis.analyze_unanalyzed()
        result.evidence_analyzed = analysis_result.analyzed
        result.evidence_failed = analysis_result.failed
        result.total_cost_usd += analysis_result.total_cost_usd
        if analysis_result.stopped_for_budget:
            result.stopped_for_budget = True

        # Stage 3 — cluster + synthesize. Even if analysis stopped for
        # budget, we still cluster what we have — the clustering pass
        # doesn't cost anything, only synthesis does.
        clustering = EvidenceClusteringService(org=org)
        clusters = clustering.run()

        # Synthesis only runs if we still have budget headroom.
        if not result.stopped_for_budget and clusters.potential_clusters:
            synthesis = SynthesisService(org=org, job=job)
            synth_result = synthesis.synthesize_clusters(clusters.potential_clusters)
            result.suggestions_created = synth_result.created
            result.total_cost_usd += synth_result.total_cost_usd
            if synth_result.stopped_for_budget:
                result.stopped_for_budget = True

        _finalize(job, result)
        return result

    except Exception as exc:
        logger.exception('Nightly job failed for org=%s job=%s', org.pk, job.pk)
        result.error = f'{type(exc).__name__}: {exc}'
        LearningJob.objects.filter(pk=job.pk).update(
            status=LearningJob.Status.FAILED,
            error=result.error[:2000],
            completed_at=timezone.now(),
        )
        return result


def _finalize(job: LearningJob, result: NightlyResult) -> None:
    """Mark job COMPLETED or PARTIAL. Individual services already stamp
    PARTIAL when their own budget check trips — respect that, don't
    downgrade to COMPLETED."""
    job.refresh_from_db()
    final_status = (
        LearningJob.Status.PARTIAL
        if result.stopped_for_budget or job.status == LearningJob.Status.PARTIAL
        else LearningJob.Status.COMPLETED
    )
    LearningJob.objects.filter(pk=job.pk).update(
        status=final_status,
        completed_at=timezone.now(),
        evidence_processed=result.evidence_analyzed,
    )
