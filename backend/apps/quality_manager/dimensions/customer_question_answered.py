"""Customer Question Answered dimension — QM V1 third shipped dimension.

Minimal semantic per operator directive (2026-08-23):

  1. Identify explicit customer questions in the conversation via
     heuristic (contains '?' OR starts with an interrogative word).
     No LLM for detection — question identification is a shape
     check, not a semantic judgment.
  2. Collect agent replies that came AFTER each question and BEFORE
     the next customer message.
  3. One small LLM call per conversation batches all its questions
     and returns per-question verdicts (yes/partial/no/unknown).
  4. State mapping:
       yes        → PASS
       no         → FAIL (severity=warning)
       partial    → FAIL (severity=info)
       unknown    → UNKNOWN_NOT_EVALUABLE
       no agent reply after the question → UNKNOWN
         (reason=no_agent_reply_after_question)
       conversation has no customer questions at all → NOT_APPLICABLE
  5. Evidence per evaluation: the customer question turn +
     agent-reply turn ids + LLM verdict + LLM one-sentence rationale.

Does NOT:
  * evaluate whether the answer was FACTUALLY CORRECT (that's a
    future dimension — Contradiction / incorrect info)
  * split a multi-question customer message into multiple items
    (whole message = one question in V1)
  * add new context infrastructure or config paths

Cost expectations (Spotless 624 corpus): ~1 LLM call per
conversation that has ≥1 question, ~$0.10 per run total using
gpt-4o-mini. If a conversation has 0 questions, no LLM call is
made — it exits with NOT_APPLICABLE.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable

from apps.quality_manager.dimensions import register
from apps.quality_manager.dimensions.base import (
    BaseDimension,
    DimensionResult,
    EvidenceRef,
    State,
)


logger = logging.getLogger(__name__)


VERSION = 'qm-v1-question-answered.1'
MODEL = 'gpt-4o-mini'

_INTERROGATIVE_STARTERS = (
    'what', 'how', 'when', 'where', 'why', 'which', 'who', 'whose',
    'do', 'does', 'did', 'is', 'are', 'was', 'were', 'am',
    'can', 'could', 'will', 'would', 'should', 'may', 'might',
    'have', 'has', 'had',
)
_INTERROGATIVE_RX = re.compile(
    r'^\s*(?:' + '|'.join(_INTERROGATIVE_STARTERS) + r')\b',
    re.IGNORECASE,
)


SYSTEM_PROMPT = '''You evaluate whether an AGENT actually addressed a CUSTOMER's
question in a service-business chat. You are NOT evaluating factual
correctness — only whether the agent's reply addressed what was
asked.

For each numbered question, decide:
  "yes"     — the agent's reply directly addresses what the customer asked
  "partial" — the reply addresses part of the question but leaves the
              core ask unanswered (e.g. customer asks price + timing,
              agent gives price only)
  "no"      — the agent ignored, deflected, or changed subject
  "unknown" — the question is ambiguous or the evidence is insufficient
              to decide (e.g. rhetorical, mid-sentence fragment,
              unrelated small talk)

Return ONLY JSON in this shape:
{
  "verdicts": [
    {"q_id": "q1", "verdict": "yes|no|partial|unknown", "rationale": "<one short sentence>"},
    ...one entry per numbered question, in the same order...
  ]
}

Do not add commentary outside the JSON.
'''


def _is_customer_question(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    if '?' in t:
        return True
    return bool(_INTERROGATIVE_RX.match(t))


def _build_user_prompt(items: list[dict]) -> str:
    """Render the batched questions + agent replies for the LLM.

    `items` shape:
      [
        {
          'q_id': 'q1',
          'question_turn_id': 't0012',
          'question_text': 'Do you clean windows?',
          'agent_replies': [
            {'turn_id': 't0013', 'text': 'Yes, exterior windows are $50 extra.'},
            ...
          ],
        },
        ...
      ]
    """
    lines: list[str] = [
        'Evaluate each numbered question independently.',
        '',
    ]
    for item in items:
        lines.append(f"[{item['q_id']}] CUSTOMER (turn {item['question_turn_id']}):")
        lines.append(f"  {item['question_text']}")
        replies = item.get('agent_replies') or []
        if replies:
            lines.append('  AGENT REPLIES (in order, before next customer msg):')
            for reply in replies:
                snippet = (reply.get('text') or '').strip().replace('\n', ' ')
                if len(snippet) > 400:
                    snippet = snippet[:400] + '…'
                lines.append(f"    ({reply.get('turn_id')}) {snippet}")
        else:
            lines.append('  AGENT REPLIES: (none — no agent turn after this question)')
        lines.append('')
    return '\n'.join(lines)


def _verdict_to_state(verdict: str) -> tuple[State, str, str]:
    """Return (state, severity, reason_code) for an LLM verdict."""
    v = (verdict or '').strip().lower()
    if v == 'yes':
        return State.PASS, '', 'agent_addressed_question'
    if v == 'partial':
        return State.FAIL, 'info', 'agent_partial_answer'
    if v == 'no':
        return State.FAIL, 'warning', 'agent_did_not_address'
    return State.UNKNOWN_NOT_EVALUABLE, '', 'llm_verdict_unknown'


@register
class CustomerQuestionAnsweredDimension(BaseDimension):
    name = 'customer_question_answered'
    version = VERSION

    def evaluate(
        self, *,
        reconstruction_run,
        conversation,
    ) -> Iterable[DimensionResult]:
        from apps.conversations.models import (
            ConversationTurn, Speaker,
        )
        from apps.learning.services.llm_client import LLMClient

        conv_id = str(conversation.id)
        turns = list(
            ConversationTurn.objects
            .filter(conversation=conversation)
            .order_by('occurred_at')
            .only('id', 'source_turn_id', 'speaker', 'occurred_at', 'text')
        )
        if not turns:
            yield DimensionResult(
                dimension=self.name, state=State.NOT_APPLICABLE,
                conversation_id=conv_id,
                reason_code='no_turns',
                rationale_text='Conversation has no turns.',
            )
            return

        # Walk conversation, find customer questions + agent-replies
        # between them and the NEXT customer turn.
        items: list[dict] = []
        pending_question: dict | None = None
        for turn in turns:
            speaker = turn.speaker
            text = turn.text or ''
            if speaker == Speaker.CUSTOMER:
                # Close out the previous pending question (its reply
                # window ends when the next customer message starts).
                if pending_question is not None:
                    items.append(pending_question)
                    pending_question = None
                if _is_customer_question(text):
                    pending_question = {
                        'q_id': f'q{len(items) + 1}',
                        'question_turn_id': turn.source_turn_id,
                        'question_text': text.strip(),
                        'agent_replies': [],
                    }
            elif speaker == Speaker.AGENT and pending_question is not None:
                pending_question['agent_replies'].append({
                    'turn_id': turn.source_turn_id,
                    'text': text,
                })
            # SYSTEM / UNKNOWN speakers ignored for this dimension.
        if pending_question is not None:
            items.append(pending_question)

        if not items:
            yield DimensionResult(
                dimension=self.name, state=State.NOT_APPLICABLE,
                conversation_id=conv_id,
                reason_code='no_customer_questions',
                rationale_text=(
                    'Conversation has no customer messages that look like '
                    'a question (no "?" and no interrogative starter).'
                ),
            )
            return

        # Split items: those with 0 agent replies → UNKNOWN directly
        # (skip LLM; no reply = nothing for LLM to judge).
        needing_llm: list[dict] = []
        for item in items:
            if not item.get('agent_replies'):
                yield DimensionResult(
                    dimension=self.name, state=State.UNKNOWN_NOT_EVALUABLE,
                    conversation_id=conv_id,
                    reason_code='no_agent_reply_after_question',
                    rationale_text=(
                        f'Customer asked (turn {item["question_turn_id"]}) '
                        f'but no agent turn followed before the next '
                        f'customer message or end of conversation.'
                    ),
                    evidence=[EvidenceRef(
                        kind='conversation_turn',
                        ref=item['question_turn_id'],
                        description=(item['question_text'] or '')[:200],
                    )],
                )
            else:
                needing_llm.append(item)

        if not needing_llm:
            return

        # One LLM call per conversation, batched over all questions.
        user_prompt = _build_user_prompt(needing_llm)
        try:
            client = LLMClient()
            result = client.analyze(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                model=MODEL,
                max_tokens=800,
            )
            parsed = result.parsed_json or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                'qm/question-answered: LLM call failed for conv=%s: %s',
                conv_id, exc,
            )
            for item in needing_llm:
                yield _unknown_from_llm_failure(item, conv_id, str(exc))
            return

        verdicts_by_qid = {}
        for v in (parsed.get('verdicts') or []):
            if isinstance(v, dict) and v.get('q_id'):
                verdicts_by_qid[v['q_id']] = v

        for item in needing_llm:
            v = verdicts_by_qid.get(item['q_id'])
            if not v:
                yield _unknown_from_llm_failure(
                    item, conv_id, 'llm_output_missing_q_id',
                )
                continue
            state, severity, reason_code = _verdict_to_state(v.get('verdict'))
            rationale = (v.get('rationale') or '').strip()[:400]
            evidence = [
                EvidenceRef(
                    kind='conversation_turn',
                    ref=item['question_turn_id'],
                    description=(item['question_text'] or '')[:200],
                ),
            ]
            for reply in item.get('agent_replies') or []:
                evidence.append(EvidenceRef(
                    kind='conversation_turn',
                    ref=reply.get('turn_id') or '',
                    description=(reply.get('text') or '')[:200],
                ))
            evidence.append(EvidenceRef(
                kind='llm_verdict',
                ref=v.get('verdict') or 'unknown',
                description=(
                    rationale or 'LLM returned no rationale'
                ),
            ))
            yield DimensionResult(
                dimension=self.name,
                state=state,
                conversation_id=conv_id,
                severity=severity,
                reason_code=reason_code,
                rationale_text=(
                    rationale
                    or f'LLM verdict={v.get("verdict")} for question at '
                       f'turn {item["question_turn_id"]}'
                ),
                evidence=evidence,
            )

    def evaluate_corpus(self, *, reconstruction_run) -> Iterable[DimensionResult]:
        # No corpus-level pattern for V1. Per-conversation only.
        return []


def _unknown_from_llm_failure(item: dict, conv_id: str, reason: str) -> DimensionResult:
    return DimensionResult(
        dimension='customer_question_answered',
        state=State.UNKNOWN_NOT_EVALUABLE,
        conversation_id=conv_id,
        reason_code='llm_evaluation_failed',
        rationale_text=(
            f'LLM evaluation could not produce a verdict for question at '
            f'turn {item["question_turn_id"]}: {reason[:120]}'
        ),
        evidence=[EvidenceRef(
            kind='conversation_turn',
            ref=item['question_turn_id'],
            description=(item['question_text'] or '')[:200],
        )],
    )
