"""Cluster synthesis — turn a CandidateCluster into a LearningSuggestion.

One synthesizer call per new cluster. Uses the LEARNING_SYNTHESIS_MODEL
(default Opus 4.7 alias) because this step needs judgment across many
inputs — the cluster prompt asks the model to consolidate signals from
many candidates into ONE business improvement, not summarize any one
conversation.

Confidence is not the LLM's number alone. We store the LLM component,
the support-count component, and the outcome-consistency component
separately so the dashboard can explain the score.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from django.conf import settings
from django.db import transaction
from django.db.models import F

from apps.accounts.models import Organization
from apps.learning.analyzers.prompts import SYNTHESIS_PROMPT_VERSION, SYNTHESIS_SYSTEM_PROMPT
from apps.learning.models import (
    CandidateRecommendation,
    LearningJob,
    LearningSuggestion,
)
from apps.learning.services.budget import BudgetTracker
from apps.learning.services.clustering import CandidateCluster
from apps.learning.services.llm_client import LearningLLMClient, LLMProviderError

logger = logging.getLogger(__name__)

_MAX_REPRESENTATIVE = 3


@dataclass
class SynthesisResult:
    created: int = 0
    stopped_for_budget: bool = False
    total_cost_usd: Decimal = field(default_factory=lambda: Decimal('0'))
    suggestion_ids: list[str] = field(default_factory=list)


class SynthesisService:
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
        self.model = model or settings.LEARNING_SYNTHESIS_MODEL
        self.budget = BudgetTracker(job=job)
        self.min_supporting = int(
            getattr(settings, 'LEARNING_MIN_SUPPORTING_EVIDENCE', 3)
        )

    def synthesize_clusters(self, clusters: Iterable[CandidateCluster]) -> SynthesisResult:
        result = SynthesisResult()
        for cluster in clusters:
            if self.budget.exceeded():
                result.stopped_for_budget = True
                break
            try:
                suggestion = self._synthesize_one(cluster)
            except LLMProviderError as exc:
                logger.exception('Synthesis failed for cluster (%d candidates): %s',
                                 len(cluster.candidates), exc)
                continue
            if suggestion is not None:
                result.created += 1
                result.suggestion_ids.append(str(suggestion.id))
                result.total_cost_usd += Decimal(suggestion.synthesis_cost_usd)
        if result.stopped_for_budget and self.job.status != LearningJob.Status.PARTIAL:
            LearningJob.objects.filter(pk=self.job.pk).update(
                status=LearningJob.Status.PARTIAL,
                error=f'Synthesis budget ceiling reached at ${self.budget.spent_usd}',
            )
        return result

    def _synthesize_one(self, cluster: CandidateCluster) -> LearningSuggestion | None:
        system_prompt = SYNTHESIS_SYSTEM_PROMPT
        user_prompt = self._build_user_prompt(cluster)

        llm_result = self.llm.analyze(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=self.model,
        )
        synthesis = _normalize_synthesis(llm_result.parsed_json)
        confidence_breakdown = _confidence_breakdown(cluster, synthesis, self.min_supporting)
        representative = _representative_examples(cluster.candidates)

        suggestion = self._persist(
            cluster=cluster,
            synthesis=synthesis,
            confidence_breakdown=confidence_breakdown,
            representative=representative,
            synthesis_cost=llm_result.cost_usd,
            raw_response=llm_result.raw_response,
        )
        self.budget.record(llm_result.cost_usd)
        LearningJob.objects.filter(pk=self.job.pk).update(
            suggestions_created=F('suggestions_created') + 1
        )
        return suggestion

    def _build_user_prompt(self, cluster: CandidateCluster) -> str:
        outcome_counts = _outcome_distribution(cluster.candidates)
        outcome_line = ', '.join(f'{k}={v}' for k, v in outcome_counts.items())
        candidate_lines = []
        for cand in cluster.candidates[:20]:  # cap prompt size at 20 exemplars
            candidate_lines.append(
                f'- [{cand.kind}] {cand.title.strip()}\n'
                f'  {cand.description.strip()}\n'
                f'  (source={cand.evidence.source_system}, '
                f'outcome_signal={cand.outcome_signal}, '
                f'llm_confidence={cand.llm_confidence})'
            )
        joined = '\n'.join(candidate_lines) if candidate_lines else '(no candidates)'
        return (
            f'Category: {cluster.category}\n'
            f'Cluster size: {len(cluster.candidates)} candidates\n'
            f'Outcome distribution: {outcome_line or "n/a"}\n'
            f'\n--- Candidate recommendations ---\n{joined}\n'
        )

    @transaction.atomic
    def _persist(
        self,
        *,
        cluster: CandidateCluster,
        synthesis: dict,
        confidence_breakdown: dict,
        representative: dict,
        synthesis_cost: Decimal,
        raw_response: str,
    ) -> LearningSuggestion:
        support_count = len(cluster.candidates)
        status = (
            LearningSuggestion.Status.NEW
            if support_count >= self.min_supporting
            else LearningSuggestion.Status.WATCHLIST
        )
        suggestion = LearningSuggestion.objects.create(
            org=self.org,
            job=self.job,
            category=cluster.category,
            title=str(synthesis.get('title', ''))[:200],
            description=str(synthesis.get('description', '')),
            fingerprint=cluster.fingerprint,
            confidence=Decimal(str(confidence_breakdown['final'])).quantize(Decimal('0.01')),
            confidence_breakdown=confidence_breakdown,
            representative_examples=representative,
            supporting_count=support_count,
            synthesis_json={
                'why_this_matters': str(synthesis.get('why_this_matters', '')),
                'supporting_evidence_summary': str(synthesis.get('supporting_evidence_summary', '')),
                'suggested_playbook_change': str(synthesis.get('suggested_playbook_change', '')),
                'suggested_faq_addition': str(synthesis.get('suggested_faq_addition', '')),
                'raw_response': raw_response[:8000],
            },
            synthesis_model=self.model,
            synthesis_prompt_version=SYNTHESIS_PROMPT_VERSION,
            synthesis_cost_usd=synthesis_cost,
            status=status,
        )
        now = suggestion.created_at
        CandidateRecommendation.objects.filter(
            id__in=[c.id for c in cluster.candidates]
        ).update(suggestion=suggestion, clustered_at=now, updated_at=now)
        return suggestion


def _normalize_synthesis(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            'title': '(synthesis failed — non-dict output)',
            'description': '',
            'confidence': 0.0,
            'why_this_matters': '',
            'supporting_evidence_summary': '',
            'suggested_playbook_change': '',
            'suggested_faq_addition': '',
        }
    try:
        confidence = float(payload.get('confidence', 0))
    except (TypeError, ValueError):
        confidence = 0.0
    payload['confidence'] = max(0.0, min(1.0, confidence))
    return payload


def _confidence_breakdown(
    cluster: CandidateCluster,
    synthesis: dict,
    min_supporting: int,
) -> dict[str, float]:
    """Multiplicative confidence: llm × sqrt(support) × outcome_consistency.

    Each component is stored separately so the dashboard can render:
        "Confidence 87% — LLM 0.91 × supporting 24 × outcome consistent"
    """
    llm_component = _avg_confidence(cluster.candidates)
    # sqrt scaling: 1 candidate → 0.577, 3 → 1.0, 5 → 1.29 (capped at 1.0)
    support_component = min(1.0, math.sqrt(len(cluster.candidates) / max(1, min_supporting)))
    outcome_component = _outcome_consistency(cluster.candidates)
    synth_confidence = float(synthesis.get('confidence', llm_component))
    final = max(0.0, min(1.0, synth_confidence * support_component * outcome_component))
    return {
        'llm': round(llm_component, 3),
        'llm_synthesis': round(synth_confidence, 3),
        'support': round(support_component, 3),
        'support_count': len(cluster.candidates),
        'outcome_consistency': round(outcome_component, 3),
        'final': round(final, 3),
    }


def _avg_confidence(candidates: list[CandidateRecommendation]) -> float:
    if not candidates:
        return 0.0
    total = sum(float(c.llm_confidence) for c in candidates)
    return total / len(candidates)


def _outcome_consistency(candidates: list[CandidateRecommendation]) -> float:
    """Fraction of candidates whose outcome_signal matches the majority.

    A cluster where 24 of 27 candidates came from lost conversations
    scores ~0.89. Mixed signal (12 positive / 12 negative) scores 0.5.
    Neutral-only clusters score 1.0 because there's no contradiction —
    the recommendation isn't tied to a specific outcome direction.
    """
    if not candidates:
        return 0.0
    counts = _outcome_distribution(candidates)
    majority = max(counts.values())
    return majority / sum(counts.values())


def _outcome_distribution(candidates: list[CandidateRecommendation]) -> dict[str, int]:
    counts = {'positive': 0, 'negative': 0, 'neutral': 0}
    for cand in candidates:
        counts[cand.outcome_signal] = counts.get(cand.outcome_signal, 0) + 1
    return {k: v for k, v in counts.items() if v > 0}


def _representative_examples(candidates: list[CandidateRecommendation]) -> dict[str, Any]:
    """Pick top-N by confidence, highest-confidence single, and newest."""
    sorted_by_conf = sorted(candidates, key=lambda c: float(c.llm_confidence), reverse=True)
    top = [_example_dict(c) for c in sorted_by_conf[:_MAX_REPRESENTATIVE]]
    highest = _example_dict(sorted_by_conf[0]) if sorted_by_conf else None
    newest = _example_dict(max(candidates, key=lambda c: c.created_at)) if candidates else None
    return {
        'top': top,
        'highest_confidence': highest,
        'newest': newest,
    }


def _example_dict(cand: CandidateRecommendation) -> dict[str, Any]:
    evidence = cand.evidence
    return {
        'candidate_id': str(cand.id),
        'evidence_id': str(evidence.id) if evidence else None,
        'source_system': evidence.source_system if evidence else '',
        'evidence_type': evidence.evidence_type if evidence else '',
        'external_id': evidence.external_id if evidence else '',
        'title': cand.title,
        'llm_confidence': float(cand.llm_confidence),
        'outcome_signal': cand.outcome_signal,
    }
