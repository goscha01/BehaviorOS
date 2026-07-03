"""Evidence analysis orchestrator.

Walks the resume queue (`EvidenceInsight` rows with `analyzed_at IS NULL`),
dispatches each to the analyzer registered for its `evidence_type`, and
persists results one at a time. Per-insight commits mean a mid-loop
budget stop never loses work — the next scheduled run picks up where
this one stopped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Organization
from apps.learning.analyzers import get_analyzer_for
from apps.learning.analyzers.base import AnalysisResult
from apps.learning.analyzers.schema import AnalysisSchemaError, empty_analysis
from apps.learning.models import EvidenceInsight, LearningJob
from apps.learning.services.budget import BudgetTracker
from apps.learning.services.llm_client import LearningLLMClient, LLMProviderError

logger = logging.getLogger(__name__)


@dataclass
class AnalysisRunResult:
    analyzed: int = 0
    skipped: int = 0
    failed: int = 0
    stopped_for_budget: bool = False
    total_cost_usd: Decimal = field(default_factory=lambda: Decimal('0'))


class EvidenceAnalysisService:
    def __init__(
        self,
        org: Organization,
        job: LearningJob,
        llm_client: LearningLLMClient | None = None,
        model: str | None = None,
    ):
        self.org = org
        self.job = job
        self.llm = llm_client or LearningLLMClient()
        self.model = model or settings.LEARNING_ANALYSIS_MODEL
        self.budget = BudgetTracker(job=job)

    def analyze_unanalyzed(self, limit: int | None = None) -> AnalysisRunResult:
        """Analyze every unanalyzed insight for this org, budget permitting.

        `limit` caps the number of records considered in this run (useful
        for manual triggers / smoke tests). None = no cap; only the
        budget stops the loop.
        """
        result = AnalysisRunResult()
        queue = self._resume_queue(limit=limit)

        for insight in queue:
            if self.budget.exceeded():
                result.stopped_for_budget = True
                break

            try:
                analyzer = get_analyzer_for(insight.evidence_type)
            except LookupError as exc:
                logger.warning('Skipping insight %s: %s', insight.pk, exc)
                result.skipped += 1
                continue

            try:
                analysis = analyzer.analyze(insight, self.llm, self.model)
            except (LLMProviderError, AnalysisSchemaError) as exc:
                logger.exception(
                    'Analyzer %s failed for insight %s', analyzer.evidence_type, insight.pk
                )
                self._persist_failure(insight, analyzer.prompt_version, error=str(exc))
                result.failed += 1
                continue

            self._persist_success(insight, analysis)
            self.budget.record(analysis.cost_usd)
            result.analyzed += 1
            result.total_cost_usd += analysis.cost_usd

        if result.stopped_for_budget and self.job.status != LearningJob.Status.PARTIAL:
            LearningJob.objects.filter(pk=self.job.pk).update(
                status=LearningJob.Status.PARTIAL,
                error=f'Budget ceiling reached at ${self.budget.spent_usd}',
            )
        return result

    def _resume_queue(self, limit: int | None) -> Iterable[EvidenceInsight]:
        qs = (
            EvidenceInsight.objects.filter(org=self.org, analyzed_at__isnull=True)
            .order_by('created_at')
        )
        if limit is not None:
            qs = qs[:limit]
        return qs

    @transaction.atomic
    def _persist_success(self, insight: EvidenceInsight, analysis: AnalysisResult) -> None:
        insight.ai_summary = analysis.summary
        insight.analysis_json = analysis.analysis_json
        insight.raw_response = analysis.raw_response
        insight.analysis_model = analysis.model_used
        insight.analysis_prompt_version = analysis.prompt_version
        insight.analysis_cost_usd = analysis.cost_usd
        insight.analyzed_at = timezone.now()
        insight.save(update_fields=[
            'ai_summary',
            'analysis_json',
            'raw_response',
            'analysis_model',
            'analysis_prompt_version',
            'analysis_cost_usd',
            'analyzed_at',
            'updated_at',
        ])

    @transaction.atomic
    def _persist_failure(
        self,
        insight: EvidenceInsight,
        prompt_version: str,
        error: str,
    ) -> None:
        """Leave analyzed_at NULL so the resume queue retries next run,
        but stash the error + last raw output for debugging."""
        insight.analysis_json = {**empty_analysis(), 'error': error[:1000]}
        insight.analysis_prompt_version = prompt_version
        insight.save(update_fields=[
            'analysis_json',
            'analysis_prompt_version',
            'updated_at',
        ])
