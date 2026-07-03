"""Base analyzer contract + registry.

Each analyzer targets one EvidenceType (`conversation`, `call`, `outcome`,
and future `interview`, `review`, `incident`). Analyzers register at
import time. The orchestrator dispatches by `insight.evidence_type`.

Analyzers are deliberately thin: build a prompt, hand it to the LLM
client, validate the parsed JSON. All LLM specifics live in the client;
all schema specifics live in `schema.py`. This keeps analyzers focused
on evidence-type-specific concerns (how to render a transcript vs. an
outcome record vs. an interview).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar, Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from apps.learning.models import EvidenceInsight
    from apps.learning.services.llm_client import LearningLLMClient


@dataclass
class AnalysisResult:
    """Everything the orchestrator needs to persist one analysis."""

    analysis_json: dict
    raw_response: str
    cost_usd: Decimal
    model_used: str
    prompt_version: str

    @property
    def summary(self) -> str:
        return str(self.analysis_json.get('summary', ''))


class BaseEvidenceAnalyzer(ABC):
    """Analyzer targeting one evidence_type.

    Subclasses set:
    - `evidence_type` (registry key; matches EvidenceInsight.EvidenceType values)
    - `prompt_version` (versioned in prompts.py; bumps trigger re-analysis)
    """

    evidence_type: ClassVar[str]
    prompt_version: ClassVar[str]

    @abstractmethod
    def build_prompts(self, insight: 'EvidenceInsight') -> tuple[str, str]:
        """Return (system_prompt, user_prompt) for this evidence."""

    def analyze(
        self,
        insight: 'EvidenceInsight',
        llm: 'LearningLLMClient',
        model: str,
    ) -> AnalysisResult:
        from apps.learning.analyzers.schema import validate_analysis

        system_prompt, user_prompt = self.build_prompts(insight)
        llm_result = llm.analyze(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
        )
        analysis_json = validate_analysis(llm_result.parsed_json, self.evidence_type)
        return AnalysisResult(
            analysis_json=analysis_json,
            raw_response=llm_result.raw_response,
            cost_usd=llm_result.cost_usd,
            model_used=llm_result.model_used,
            prompt_version=self.prompt_version,
        )


_registry: dict[str, type[BaseEvidenceAnalyzer]] = {}


def register(cls: type[BaseEvidenceAnalyzer]) -> type[BaseEvidenceAnalyzer]:
    key = getattr(cls, 'evidence_type', None)
    if not key:
        raise ValueError(f'{cls.__name__} must define a non-empty evidence_type')
    if key in _registry and _registry[key] is not cls:
        raise ValueError(
            f'evidence_type {key!r} already registered by {_registry[key].__name__}'
        )
    _registry[key] = cls
    return cls


def get_analyzer_for(evidence_type: str) -> BaseEvidenceAnalyzer:
    try:
        return _registry[evidence_type]()
    except KeyError:
        raise LookupError(
            f'No analyzer registered for evidence_type={evidence_type!r}. '
            f'Registered: {sorted(_registry)}'
        )


def iter_registered() -> Iterator[tuple[str, type[BaseEvidenceAnalyzer]]]:
    yield from sorted(_registry.items())
