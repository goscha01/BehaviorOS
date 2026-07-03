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
from apps.learning.models import CandidateRecommendation, EvidenceInsight, LearningJob
from apps.learning.services.budget import BudgetTracker
from apps.learning.services.fingerprint import fingerprint as fingerprint_text
from apps.learning.services.fingerprint import tokenize
from apps.learning.services.llm_client import LearningLLMClient, LLMProviderError

# Outcome-status → CandidateRecommendation.OutcomeSignal mapping. Populated
# from the source system's own outcome vocabulary — never inferred.
_POSITIVE_OUTCOMES = frozenset({
    'booked', 'won', 'completed', 'recurring', 'accepted', 'confirmed',
})
_NEGATIVE_OUTCOMES = frozenset({
    'cancelled', 'canceled', 'lost', 'no_show', 'declined', 'rejected',
    'churned',
})

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
        self._extract_candidates(insight, analysis.analysis_json)

    def _extract_candidates(self, insight: EvidenceInsight, analysis_json: dict) -> None:
        """Flatten candidate_playbook_rules + candidate_faq into
        CandidateRecommendation rows so clustering has something to group.

        Delete-then-create: on a re-analysis (prompt version bump, manual
        reset), stale candidates are wiped before new ones are written.
        Clustered candidates lose their suggestion linkage — the next
        clustering run reattaches or re-clusters them.
        """
        CandidateRecommendation.objects.filter(evidence=insight).delete()

        category = str(analysis_json.get('category', 'other')).lower()
        parent_confidence = _to_decimal(analysis_json.get('confidence', 0))
        outcome_signal = _classify_outcome(insight.outcome)

        rows: list[CandidateRecommendation] = []
        for rule in analysis_json.get('candidate_playbook_rules', []) or []:
            if not isinstance(rule, dict):
                continue
            title = str(rule.get('title', '')).strip()
            description = str(rule.get('description', '')).strip()
            if not title and not description:
                continue
            rule_conf = _to_decimal(rule.get('confidence', parent_confidence))
            rows.append(self._build_candidate(
                insight=insight,
                kind=CandidateRecommendation.Kind.PLAYBOOK_RULE,
                category=category,
                title=title,
                description=description,
                llm_confidence=rule_conf,
                outcome_signal=outcome_signal,
            ))

        for faq in analysis_json.get('candidate_faq', []) or []:
            if not isinstance(faq, dict):
                continue
            question = str(faq.get('question', '')).strip()
            answer = str(faq.get('answer', '')).strip()
            if not question and not answer:
                continue
            rows.append(self._build_candidate(
                insight=insight,
                kind=CandidateRecommendation.Kind.FAQ,
                category=category,
                title=question,
                description=answer,
                llm_confidence=parent_confidence,
                outcome_signal=outcome_signal,
            ))

        if rows:
            CandidateRecommendation.objects.bulk_create(rows)

    def _build_candidate(
        self,
        *,
        insight: EvidenceInsight,
        kind: str,
        category: str,
        title: str,
        description: str,
        llm_confidence: Decimal,
        outcome_signal: str,
    ) -> CandidateRecommendation:
        fp_text = f'{title}\n{description}'
        tokens = tokenize(fp_text)
        return CandidateRecommendation(
            org=self.org,
            evidence=insight,
            job=self.job,
            kind=kind,
            category=category,  # schema validator already clamps to valid enum
            title=title[:400],
            description=description,
            llm_confidence=llm_confidence,
            outcome_signal=outcome_signal,
            fingerprint=fingerprint_text(fp_text),
            tokens=tokens,
        )

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


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal('0')


def _classify_outcome(outcome_status: str) -> str:
    lowered = (outcome_status or '').lower()
    if lowered in _POSITIVE_OUTCOMES:
        return CandidateRecommendation.OutcomeSignal.POSITIVE
    if lowered in _NEGATIVE_OUTCOMES:
        return CandidateRecommendation.OutcomeSignal.NEGATIVE
    return CandidateRecommendation.OutcomeSignal.NEUTRAL
