"""Customer-signal audit — audits the CUSTOMER's own turn text at each
first-occurrence of a semantic CUSTOMER_SIGNAL event, and classifies
whether the extractor's label matches the actual utterance.

Companion to `action_semantics_audit.py` which audits the AGENT's reply
text. This module handles the mirror question: "when the extractor
tagged this customer turn as CUSTOMER_HESITATION (or any CUSTOMER_SIGNAL),
does the raw customer text actually mean that?"

Used by Pipeline 1B-6 Phase 0 to verify CUSTOMER_HESITATION before
consolidating it into a state taxonomy. The 1B-5 report flagged that
some CUSTOMER_HESITATION samples read as reassurance-after-confirmation
rather than classical hesitation.

Not persisted — derived analytical state.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from apps.conversations.analysis.conditional import Event
from apps.conversations.semantic.ontology import CUSTOMER_SIGNAL_EVENTS


AUDITOR_VERSION = 'customer-signal-auditor-v1'


# Per-signal taxonomies. Each entry: (categories, system_prompt).
# Keyed by CUSTOMER_SIGNAL event_type. Add new taxonomies here as
# more signals need auditing.
CUSTOMER_SIGNAL_TAXONOMIES: dict[str, dict] = {}


CUSTOMER_HESITATION_CATEGORIES: frozenset[str] = frozenset({
    'genuine_uncertainty',        # customer expresses "I'm not sure", "I need to think"
    'reassurance_seeking',        # asks a clarifying question hoping to hear confirmation
    'concern_resolved_relief',    # actually indicates resolution, not hesitation
    'deferment',                  # "I'll get back to you", "let me check"
    'objection',                  # implicit objection dressed as hesitation
    'extractor_misclassification',# the turn is clearly not about hesitation at all
    'other',
})


CUSTOMER_SIGNAL_TAXONOMIES['CUSTOMER_HESITATION'] = {
    'categories': CUSTOMER_HESITATION_CATEGORIES,
    'system_prompt': (
        'You are auditing a semantic-event extractor. The extractor '
        'tagged the customer turn below as CUSTOMER_HESITATION. Read '
        "the customer's raw text and classify what it actually means:\n\n"
        '- genuine_uncertainty: customer expresses actual uncertainty '
        'or indecision ("I\'m not sure", "I need to think about it").\n'
        '- reassurance_seeking: customer asks a clarifying question '
        'hoping to hear a specific reassuring answer ("So the total '
        'is just $150, right?" or "Just checking — the cleaner brings '
        'supplies?").\n'
        '- concern_resolved_relief: customer expresses relief that a '
        'concern was resolved ("Ok whew, thanks!", "Great, that\'s '
        'what I hoped").\n'
        '- deferment: customer wants to delay ("Let me check with my '
        'partner", "I\'ll get back to you next week").\n'
        '- objection: an implicit objection dressed as hesitation '
        '("Hmm, that\'s more than I expected").\n'
        '- extractor_misclassification: the turn is clearly not about '
        'hesitation at all (extractor got it wrong).\n'
        '- other: text is present but does not fit any category cleanly.\n\n'
        'Return ONLY a JSON object of the form '
        '{"category": "<one of the above>", "confidence": <0.0-1.0>, '
        '"rationale": "<one short sentence>"}.'
    ),
}


# ---------------------------------------------------------------------------
# Types + result
# ---------------------------------------------------------------------------


@dataclass
class RawTurnText:
    ordinal: int
    speaker: str
    text: str


@dataclass
class CustomerSignalEntry:
    conversation_id: str
    condition_event: str
    outcome_class: str            # 'positive' | 'negative'
    llm_category: str
    llm_confidence: float
    llm_rationale: str
    customer_text: str
    c_turn_start: int


@dataclass
class CustomerSignalAuditResult:
    entries: list[CustomerSignalEntry] = field(default_factory=list)

    def by_category(self) -> dict[str, list[CustomerSignalEntry]]:
        out: dict[str, list[CustomerSignalEntry]] = defaultdict(list)
        for e in self.entries:
            out[e.llm_category].append(e)
        return out

    def outcome_rates_by_category(self) -> dict[str, tuple[int, int, float]]:
        out: dict[str, tuple[int, int, float]] = {}
        for cat, entries in self.by_category().items():
            pos = sum(1 for e in entries if e.outcome_class == 'positive')
            total = len(entries)
            rate = pos / total if total else 0.0
            out[cat] = (pos, total, rate)
        return out


# ---------------------------------------------------------------------------
# Classifier + audit
# ---------------------------------------------------------------------------


ClassifyFn = Callable[[str, str], tuple[str, float, str]]
# (condition_event, customer_text) -> (category, confidence, rationale)


def build_llm_classifier(
    llm_client, condition_event: str, *, model: str = 'gpt-4o-mini',
) -> ClassifyFn:
    """Wrap a LearningLLMClient into a ClassifyFn keyed by the taxonomy
    registered for `condition_event`."""
    if condition_event not in CUSTOMER_SIGNAL_TAXONOMIES:
        raise ValueError(
            f'no customer-signal taxonomy registered for {condition_event!r}. '
            f'Available: {sorted(CUSTOMER_SIGNAL_TAXONOMIES.keys())}'
        )
    entry = CUSTOMER_SIGNAL_TAXONOMIES[condition_event]
    allowed: frozenset[str] = entry['categories']
    system_prompt: str = entry['system_prompt']

    def _fallback(text: str) -> str:
        if not text.strip():
            return 'other'
        return 'other' if 'other' in allowed else next(iter(allowed))

    def _classify(condition: str, customer_text: str) -> tuple[str, float, str]:
        if not customer_text.strip():
            return 'other', 1.0, 'customer text was empty'
        user_prompt = (
            f'Extractor tagged this customer turn as {condition}.\n\n'
            f'Customer text:\n"""{customer_text}"""\n\n'
            f'Classify. Return the JSON object.'
        )
        try:
            r = llm_client.analyze(
                system_prompt=system_prompt, user_prompt=user_prompt,
                model=model, max_tokens=200,
            )
        except Exception as exc:
            return _fallback(customer_text), 0.0, f'llm error: {exc!r}'
        parsed = r.parsed_json
        if not isinstance(parsed, dict):
            return _fallback(customer_text), 0.0, 'llm returned non-object'
        cat = parsed.get('category')
        if cat not in allowed:
            return _fallback(customer_text), 0.0, (
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


def extract_customer_text_at_signal(
    turns: list[RawTurnText], signal_turn_ordinal: int, *,
    max_chars: int = 500,
) -> str:
    """Return the customer text at (or near) the signal's parent turn.

    Tries the exact-ordinal turn first (extractor set turn_start to
    the parent index). Falls back to the nearest earlier customer turn
    if the exact ordinal isn't a customer turn (voice bulk-transcript
    splits can produce this).
    """
    exact = next((t for t in turns if t.ordinal == signal_turn_ordinal), None)
    if exact and exact.speaker == 'customer' and exact.text.strip():
        return exact.text[:max_chars]
    # Fallback: nearest earlier customer turn
    candidates = [t for t in turns
                  if t.ordinal <= signal_turn_ordinal
                  and t.speaker == 'customer' and t.text.strip()]
    if candidates:
        return max(candidates, key=lambda t: t.ordinal).text[:max_chars]
    # Last resort: any customer turn in conv
    any_c = [t for t in turns if t.speaker == 'customer' and t.text.strip()]
    if any_c:
        return any_c[0].text[:max_chars]
    return ''


def audit(
    *,
    conversation_events: dict[str, list[Event]],
    conversation_turns: dict[str, list[RawTurnText]],
    conversation_outcomes: dict[str, str],
    condition_event: str,
    classify_fn: ClassifyFn,
) -> CustomerSignalAuditResult:
    """For each conversation, find the first-occurrence of `condition_event`
    and audit the customer's raw text at that turn against the
    registered taxonomy."""
    if condition_event not in CUSTOMER_SIGNAL_EVENTS:
        raise ValueError(
            f'{condition_event!r} is not a CUSTOMER_SIGNAL event'
        )
    result = CustomerSignalAuditResult()
    for conv_id, all_events in conversation_events.items():
        outcome = conversation_outcomes.get(conv_id)
        if outcome is None:
            continue
        turns = conversation_turns.get(conv_id, [])
        # First occurrence of the signal, ordered by (turn_start, ordinal)
        events_sorted = sorted(all_events, key=lambda e: (e.turn_start, e.ordinal))
        c_event = next(
            (e for e in events_sorted if e.event_type == condition_event),
            None,
        )
        if c_event is None:
            continue
        text = extract_customer_text_at_signal(turns, c_event.turn_start)
        category, confidence, rationale = classify_fn(condition_event, text)
        result.entries.append(CustomerSignalEntry(
            conversation_id=conv_id,
            condition_event=condition_event,
            outcome_class=outcome,
            llm_category=category,
            llm_confidence=confidence,
            llm_rationale=rationale,
            customer_text=text,
            c_turn_start=c_event.turn_start,
        ))
    return result
