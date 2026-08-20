"""Pipeline 1B-4B: action-semantics audit.

1B-4A showed that ~90% of NO_ACTION observations for PROPERTY_DETAILS_PROVIDED
and AVAILABILITY_REQUESTED are EXTRACTION_MISS — the agent replied but the
LLM extractor didn't emit an AGENT_ACTION event for the reply. Before
turning any 1B-3 finding into a 1C recommendation we should verify:

  1. What did the agent ACTUALLY say in each observation? Not just
     which ontology type was extracted.
  2. When the extractor said FOLLOW_UP_SENT, is that really a generic
     nudge — or a substantive response the extractor mislabeled?
  3. Among NO_ACTION observations (extractor produced nothing), what
     kind of reply is the extractor missing?

For each observation, we:
  - Locate the first agent-authored turn(s) inside the response window
  - Concatenate their raw text (bounded, so LLM cost stays predictable)
  - Classify into a small semantic taxonomy independent of ontology-v2:

        substantive_next_step   price / availability / booking prompt /
                                scope clarification / qualification /
                                anything materially advancing the sale
        generic_follow_up       generic nudge with no substance
                                ("just checking in", "any updates?")
        acknowledgment_only     "thanks", "got it", no next step
        customer_continues_details   analyzer correctly waited — customer
                                sent more info before agent replied;
                                agent may not have needed to reply yet
        true_no_response        no agent turn inside window at all
        mixed_or_unclear        agent replied but classification ambiguous

  - Recompute per-category outcome rates.

The classifier is a callable so tests can inject a deterministic stub
and the CLI command wires in the LLM.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from apps.conversations.analysis.conditional import Event, NO_ACTION, find_first_response
from apps.conversations.analysis.no_action_audit import (
    RawTurn, _truncate, classify_no_action,
)
from apps.conversations.semantic.ontology import (
    CUSTOMER_SIGNAL_EVENTS, event_temporal_class,
)


AUDITOR_VERSION = 'action-semantics-auditor-v1'


SEMANTIC_CATEGORIES: frozenset[str] = frozenset({
    'substantive_next_step',
    'generic_follow_up',
    'acknowledgment_only',
    'customer_continues_details',
    'true_no_response',
    'mixed_or_unclear',
})


# Per-observation max agent-reply text we send to the classifier. Keeps
# LLM cost bounded and prevents a 30-turn ramble from dominating the input.
MAX_REPLY_TEXT_CHARS = 800
# Cap on how many consecutive agent turns count as "the first reply."
# Beyond that the customer usually spoke again anyway.
MAX_AGENT_TURN_COUNT = 3


@dataclass
class TurnText:
    """Fuller view of a ConversationTurn than RawTurn — carries the
    text body so the classifier can read the actual reply."""
    ordinal: int
    speaker: str
    text: str


@dataclass
class ActionSemanticEntry:
    conversation_id: str
    condition_event: str
    outcome_class: str                    # 'positive' | 'negative'
    extracted_action: str                 # AGENT_ACTION type or NO_ACTION
    no_action_fine_reason: Optional[str]  # only when extracted_action == NO_ACTION
    llm_category: str                     # one of SEMANTIC_CATEGORIES
    llm_confidence: float
    llm_rationale: str
    agent_reply_text: str                 # snippet sent to the classifier
    c_turn_start: int
    window_end_turn: int


@dataclass
class ActionSemanticsAuditResult:
    entries: list[ActionSemanticEntry] = field(default_factory=list)

    def by_llm_category(self) -> dict[str, list[ActionSemanticEntry]]:
        out: dict[str, list[ActionSemanticEntry]] = defaultdict(list)
        for e in self.entries:
            out[e.llm_category].append(e)
        return out

    def outcome_rates_by_category(self) -> dict[str, tuple[int, int, float]]:
        """Returns {category: (positive_count, total_count, positive_rate)}."""
        out: dict[str, tuple[int, int, float]] = {}
        for cat, entries in self.by_llm_category().items():
            pos = sum(1 for e in entries if e.outcome_class == 'positive')
            total = len(entries)
            rate = pos / total if total else 0.0
            out[cat] = (pos, total, rate)
        return out


# ---------------------------------------------------------------------------
# Text extraction from turns
# ---------------------------------------------------------------------------


def extract_first_agent_reply(
    turns: list[TurnText], *,
    c_turn_start: int, window_end_turn: int,
) -> tuple[str, list[TurnText]]:
    """Return (concatenated_text, contributing_turns).

    Walks turns after C's turn, in order. Finds the first agent turn
    inside the window; then keeps consecutive agent turns (still inside
    the window) up to MAX_AGENT_TURN_COUNT. Stops on:
      - a non-agent turn
      - a turn past window_end_turn
      - the MAX_AGENT_TURN_COUNT cap

    Returns ('', []) when no agent turn exists inside the window.
    """
    agent_turns: list[TurnText] = []
    inside = False
    for t in turns:
        if t.ordinal <= c_turn_start:
            continue
        if t.ordinal > window_end_turn:
            break
        if t.speaker == 'agent':
            if not inside:
                # First agent turn — start the run.
                inside = True
            agent_turns.append(t)
            if len(agent_turns) >= MAX_AGENT_TURN_COUNT:
                break
        else:
            if inside:
                # Consecutive-agent run ended.
                break
            # Not yet in an agent run — keep scanning.
            continue
    if not agent_turns:
        return '', []
    joined = ' \n '.join(t.text for t in agent_turns if t.text).strip()
    if len(joined) > MAX_REPLY_TEXT_CHARS:
        joined = joined[:MAX_REPLY_TEXT_CHARS] + '…'
    return joined, agent_turns


# ---------------------------------------------------------------------------
# LLM classifier — pluggable
# ---------------------------------------------------------------------------


ClassifyFn = Callable[[str, str, str], tuple[str, float, str]]
# Signature: (condition_event, extracted_action, agent_reply_text)
#            → (llm_category, confidence, rationale)


def _fallback_category(agent_reply_text: str) -> str:
    if not agent_reply_text.strip():
        return 'true_no_response'
    return 'mixed_or_unclear'


def build_llm_classifier(
    llm_client, *, model: str = 'gpt-4o-mini',
) -> ClassifyFn:
    """Wrap a LearningLLMClient into a ClassifyFn.

    Sends one classification per observation. Cheap: ~$0.0001 per call
    at gpt-4o-mini rates for a ~200-char reply.
    """
    import json as _json

    system_prompt = (
        f'You are a sales-conversation reply classifier. Given a '
        f'customer signal, the sales-conversation event our extractor '
        f'labeled the agent reply as, and the raw text of that reply, '
        f'return a JSON object choosing ONE category from this fixed '
        f'set: {sorted(SEMANTIC_CATEGORIES)}. Categories:\n'
        '- substantive_next_step: agent gave price, offered availability, '
        'requested booking, clarified scope, asked a qualification '
        'question, or otherwise materially advanced the sale.\n'
        '- generic_follow_up: a nudge with no substantive content '
        '("just checking in", "any updates?", "let me know if you '
        'need anything"). No price, no availability, no next step.\n'
        '- acknowledgment_only: "thanks!", "got it", "perfect" — '
        'polite acknowledgment with no forward motion.\n'
        '- customer_continues_details: the "reply" is actually a system '
        'artifact or the analyzer correctly waited; the customer sent '
        'more info before the agent needed to respond.\n'
        '- true_no_response: the reply text is empty or nonexistent.\n'
        '- mixed_or_unclear: text is present but doesn\'t fit any '
        'category cleanly.\n\n'
        'Return ONLY a JSON object of the form '
        '{"category": "<one of the above>", "confidence": <0.0-1.0>, '
        '"rationale": "<one short sentence>"}.'
    )

    def _classify(condition_event: str, extracted_action: str,
                   agent_reply_text: str) -> tuple[str, float, str]:
        # Fast-path: empty reply text bypasses the LLM.
        if not agent_reply_text.strip():
            return 'true_no_response', 1.0, 'agent reply text was empty'
        user_prompt = (
            f'Customer signal that preceded this reply: {condition_event}\n'
            f'Extractor-labeled agent action: {extracted_action}\n'
            f'Agent reply text:\n"""{agent_reply_text}"""\n\n'
            f'Classify the reply. Return the JSON object.'
        )
        try:
            r = llm_client.analyze(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                max_tokens=200,
            )
        except Exception as exc:
            return _fallback_category(agent_reply_text), 0.0, f'llm error: {exc!r}'
        parsed = r.parsed_json
        if not isinstance(parsed, dict):
            return _fallback_category(agent_reply_text), 0.0, 'llm returned non-object'
        cat = parsed.get('category')
        if cat not in SEMANTIC_CATEGORIES:
            return _fallback_category(agent_reply_text), 0.0, (
                f'llm returned unknown category: {cat!r}'
            )
        try:
            conf = float(parsed.get('confidence', 0.7))
        except (TypeError, ValueError):
            conf = 0.7
        conf = max(0.0, min(1.0, conf))
        rationale = str(parsed.get('rationale', ''))[:400]
        return cat, conf, rationale

    return _classify


# ---------------------------------------------------------------------------
# Audit orchestration
# ---------------------------------------------------------------------------


def audit(
    *,
    conversation_events: dict[str, list[Event]],
    conversation_turns: dict[str, list[TurnText]],
    conversation_outcomes: dict[str, str],
    condition_event: str,
    classify_fn: ClassifyFn,
    max_turn_distance: int = 20,
) -> ActionSemanticsAuditResult:
    """For each conversation, for the first occurrence of `condition_event`,
    inspect what the agent actually did, cross-reference the extracted
    AGENT_ACTION (if any), and classify the reply text.

    Every observation gets classified — NO_ACTION and non-NO_ACTION both.
    """
    result = ActionSemanticsAuditResult()

    for conv_id, all_events in conversation_events.items():
        if conv_id not in conversation_outcomes:
            continue
        outcome = conversation_outcomes[conv_id]
        turns = conversation_turns.get(conv_id, [])
        # RawTurn view for the NO_ACTION classifier
        raw_turns = [
            RawTurn(ordinal=t.ordinal, speaker=t.speaker) for t in turns
        ]
        events_sorted = sorted(all_events, key=lambda e: (e.turn_start, e.ordinal))
        # Find first occurrence of the condition event
        c_event = next(
            (e for e in events_sorted if e.event_type == condition_event),
            None,
        )
        if c_event is None:
            continue
        if condition_event not in CUSTOMER_SIGNAL_EVENTS:
            # Guardrail — this auditor only makes sense for customer signals
            continue

        # Reproduce 1B-3 semantics: find_first_response on truncated events
        truncated = _truncate(events_sorted)
        c_idx = None
        for j, tev in enumerate(truncated):
            if (tev.turn_start == c_event.turn_start
                    and tev.ordinal == c_event.ordinal
                    and tev.event_type == c_event.event_type):
                c_idx = j
                break
        if c_idx is None:
            continue
        extracted_action, reason_raw = find_first_response(
            truncated, c_idx, max_turn_distance=max_turn_distance,
        )
        no_action_fine: Optional[str] = None
        if extracted_action == NO_ACTION:
            no_action_fine, _win_end = classify_no_action(
                reason_raw=reason_raw, c_event=c_event,
                all_events=events_sorted, all_turns=raw_turns,
                max_turn_distance=max_turn_distance,
            )

        # Determine window end for text extraction. Same logic as
        # classify_no_action but we need it here regardless of extracted_action.
        from apps.conversations.analysis.no_action_audit import _find_window_end
        window_end = _find_window_end(
            reason_raw, c_event, events_sorted, len(raw_turns), max_turn_distance,
        )
        reply_text, contributing = extract_first_agent_reply(
            turns, c_turn_start=c_event.turn_start, window_end_turn=window_end,
        )
        # Fast-path: empty reply text is definitively true_no_response —
        # classifier-independent so stub / LLM / rule-based all agree.
        if not reply_text.strip():
            category, confidence, rationale = (
                'true_no_response', 1.0, 'agent reply text was empty',
            )
        else:
            category, confidence, rationale = classify_fn(
                condition_event, extracted_action, reply_text,
            )
        result.entries.append(ActionSemanticEntry(
            conversation_id=conv_id,
            condition_event=condition_event,
            outcome_class=outcome,
            extracted_action=extracted_action,
            no_action_fine_reason=no_action_fine,
            llm_category=category,
            llm_confidence=confidence,
            llm_rationale=rationale,
            agent_reply_text=reply_text,
            c_turn_start=c_event.turn_start,
            window_end_turn=window_end,
        ))
    return result
