"""Call analyzer — voice call transcripts from Callio."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from apps.learning.analyzers.base import BaseEvidenceAnalyzer, register
from apps.learning.analyzers.prompts import CALL_PROMPT_VERSION, CALL_SYSTEM_PROMPT

if TYPE_CHECKING:
    from apps.learning.models import EvidenceInsight


@register
class CallAnalyzer(BaseEvidenceAnalyzer):
    evidence_type = 'call'
    prompt_version = CALL_PROMPT_VERSION

    def build_prompts(self, insight: 'EvidenceInsight') -> tuple[str, str]:
        payload = insight.source_payload or {}
        turns = payload.get('transcript') or []
        lines = [
            f'[{turn.get("ts", "?")}s] {turn.get("speaker", "?")}: {turn.get("text", "")}'
            for turn in turns
            if isinstance(turn, dict)
        ]
        transcript = '\n'.join(lines) if lines else '(no transcript)'

        user_prompt = (
            f'Source system: {insight.source_system}\n'
            f'Duration: {payload.get("duration_seconds", "n/a")}s\n'
            f'Business-rules version at time of call: '
            f'{insight.source_business_rules_version or "n/a"}\n'
            f'Lead: {json.dumps(payload.get("lead", {}), ensure_ascii=False)}\n'
            f'Business outcome: {json.dumps(insight.outcome_metadata or {}, ensure_ascii=False)}\n'
            f'\n--- Transcript ---\n{transcript}\n'
        )
        return CALL_SYSTEM_PROMPT, user_prompt
