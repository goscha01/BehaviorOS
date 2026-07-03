"""Outcome analyzer — operational records from ServiceFlow."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from apps.learning.analyzers.base import BaseEvidenceAnalyzer, register
from apps.learning.analyzers.prompts import (
    OUTCOME_PROMPT_VERSION,
    OUTCOME_SYSTEM_PROMPT,
)

if TYPE_CHECKING:
    from apps.learning.models import EvidenceInsight


@register
class OutcomeAnalyzer(BaseEvidenceAnalyzer):
    evidence_type = 'outcome'
    prompt_version = OUTCOME_PROMPT_VERSION

    def build_prompts(self, insight: 'EvidenceInsight') -> tuple[str, str]:
        payload = insight.source_payload or {}
        user_prompt = (
            f'Source system: {insight.source_system}\n'
            f'Business-rules version: {insight.source_business_rules_version or "n/a"}\n'
            f'Service type: {payload.get("service_type", "n/a")}\n'
            f'Customer ID: {payload.get("customer_id", "n/a")}\n'
            f'Created at: {payload.get("created_at", "n/a")}\n'
            f'Completed at: {payload.get("completed_at", "n/a")}\n'
            f'Outcome: {json.dumps(insight.outcome_metadata or {}, ensure_ascii=False)}\n'
        )
        return OUTCOME_SYSTEM_PROMPT, user_prompt
