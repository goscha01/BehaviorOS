"""Turn preprocessing + chunking for semantic extraction.

Rules (per task spec §7):
- Order chronologically
- Preserve actor/speaker + timestamps
- Remove purely technical/system noise (empty bodies, pure metadata turns)
- Keep short customer responses like 'yes', 'no', 'ok' — they affect sequence
- Bulk-transcript synthetic turns kept, marked so extractor can emit
  multiple events over one such turn
- Deterministic chunking with overlap for very long conversations
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from apps.conversations.models import Conversation, ConversationTurn


# Character-count budget per chunk. Approximation: 4 chars ≈ 1 token, so
# 8000 chars ≈ 2000 tokens of turn content. Plus system prompt + ontology
# fits comfortably in a 128k context window with room for many-event output.
CHUNK_CHAR_BUDGET = 8000

# Turns of overlap between adjacent chunks so events spanning a chunk
# boundary can still be extracted by either chunk (deduped at merge time).
CHUNK_OVERLAP_TURNS = 3


@dataclass
class NormalizedTurn:
    idx: int                   # 0-based ordinal within the full conversation
    speaker: str
    direction: str
    text: str
    occurred_at: Optional[str] # ISO-8601 or empty
    kind: str                  # 'sms' | 'call_transcript_segment' | 'call_no_transcript'
    from_bulk_transcript: bool


@dataclass
class ConversationChunk:
    turns: list[NormalizedTurn]
    turn_index_offset: int     # first turn's original ordinal in the conversation
    chunk_index: int           # 0-based
    is_only_chunk: bool


def load_and_normalize(conv: Conversation) -> list[NormalizedTurn]:
    """Load ConversationTurns from DB and produce the extractor's input shape.

    Drops turns with empty text WHEN they're not calls (call turns with
    no text are still meaningful signal — a call happened even without
    transcript).
    """
    normalized: list[NormalizedTurn] = []
    turns = list(conv.turns.order_by('occurred_at', 'source_turn_id'))
    for idx, t in enumerate(turns):
        kind = (t.metadata or {}).get('kind', '')
        text = (t.text or '').strip()
        is_call = kind in ('call_transcript_segment', 'call_no_transcript')
        # Skip empty SMS turns — they add no signal. Keep empty call
        # turns (they at least attest a call happened).
        if not text and not is_call:
            continue
        normalized.append(NormalizedTurn(
            idx=idx,
            speaker=t.speaker or 'unknown',
            direction=t.direction or 'unknown',
            text=text,
            occurred_at=t.occurred_at.isoformat() if t.occurred_at else '',
            kind=kind or 'sms',
            from_bulk_transcript=(kind == 'call_transcript_segment'),
        ))
    return normalized


def chunk_conversation(
    turns: list[NormalizedTurn],
    *,
    char_budget: int = CHUNK_CHAR_BUDGET,
    overlap_turns: int = CHUNK_OVERLAP_TURNS,
) -> list[ConversationChunk]:
    """Split into deterministic chunks by character budget, with turn overlap.

    Small conversations return a single chunk (`is_only_chunk=True`).
    Overlap lets events at chunk boundaries be extracted by either chunk;
    duplicate events are deduped in `merge_extracted_events`.
    """
    if not turns:
        return []

    # Fast path — fits in one chunk.
    total_chars = sum(len(t.text) for t in turns)
    if total_chars <= char_budget:
        return [ConversationChunk(
            turns=list(turns),
            turn_index_offset=turns[0].idx,
            chunk_index=0,
            is_only_chunk=True,
        )]

    chunks: list[ConversationChunk] = []
    i = 0
    chunk_idx = 0
    n = len(turns)
    while i < n:
        acc_chars = 0
        j = i
        while j < n and acc_chars + len(turns[j].text) <= char_budget:
            acc_chars += len(turns[j].text)
            j += 1
        # Ensure at least one turn per chunk (guards against a single
        # turn > budget — extractor may truncate but at least sees it).
        if j == i:
            j = i + 1
        chunks.append(ConversationChunk(
            turns=turns[i:j],
            turn_index_offset=turns[i].idx,
            chunk_index=chunk_idx,
            is_only_chunk=False,
        ))
        chunk_idx += 1
        if j >= n:
            break
        # Advance with overlap.
        i = max(j - overlap_turns, i + 1)
    return chunks


def merge_extracted_events(
    per_chunk_events: list[list[dict]],
) -> list[dict]:
    """Dedupe events extracted from overlapping chunks.

    Dedup key: (event_type, actor, turn_start, turn_end). For duplicates,
    prefer the one from the earlier chunk (the one that saw more prior
    context). Preserves per-chunk ordering across the merged list.
    """
    seen: set[tuple] = set()
    merged: list[dict] = []
    for events in per_chunk_events:
        for ev in events:
            key = (
                ev.get('event_type'), ev.get('actor'),
                ev.get('turn_start'), ev.get('turn_end'),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(ev)
    return merged


def render_turns_for_prompt(chunk: ConversationChunk) -> str:
    """Render turns as compact `[idx][speaker] text` lines. Speakers
    are limited to customer|agent|system|unknown per ontology."""
    lines: list[str] = []
    for t in chunk.turns:
        prefix = f'[{t.idx}][{t.speaker}]'
        if t.from_bulk_transcript:
            prefix += '[voice_transcript]'
        # Preserve linebreaks inside text as spaces to keep one-line-per-turn.
        body = t.text.replace('\n', ' ').replace('\r', ' ')
        lines.append(f'{prefix} {body}')
    return '\n'.join(lines)
