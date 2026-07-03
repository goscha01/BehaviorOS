"""Conversation analyzer — chat conversations from LeadBridge and similar."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from apps.learning.analyzers.base import BaseEvidenceAnalyzer, register
from apps.learning.analyzers.prompts import (
    CONVERSATION_PROMPT_VERSION,
    CONVERSATION_SYSTEM_PROMPT,
)

if TYPE_CHECKING:
    from apps.learning.models import EvidenceInsight


@register
class ConversationAnalyzer(BaseEvidenceAnalyzer):
    evidence_type = 'conversation'
    prompt_version = CONVERSATION_PROMPT_VERSION

    def build_prompts(self, insight: 'EvidenceInsight') -> tuple[str, str]:
        payload = insight.source_payload or {}
        messages = payload.get('messages') or []
        lines = [
            f'{msg.get("role", "?")}: {msg.get("text", "")}'
            for msg in messages
            if isinstance(msg, dict)
        ]
        transcript = '\n'.join(lines) if lines else '(no messages)'

        user_prompt = (
            f'Source system: {insight.source_system}\n'
            f'Channel: {payload.get("channel", "n/a")}\n'
            f'Business-rules version at time of conversation: '
            f'{insight.source_business_rules_version or "n/a"}\n'
            f'Lead: {json.dumps(payload.get("lead", {}), ensure_ascii=False)}\n'
            f'Business outcome: {json.dumps(insight.outcome_metadata or {}, ensure_ascii=False)}\n'
            f'\n--- Transcript ---\n{transcript}\n'
        )
        return CONVERSATION_SYSTEM_PROMPT, user_prompt
