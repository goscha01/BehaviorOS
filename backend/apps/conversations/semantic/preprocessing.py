"""Turn preprocessing + chunking for semantic extraction (v2).

v1 → v2 changes (2026-08-19):
- Turn IDs are STRINGS ("t0015", "t0015.0") not integers, exposed to the
  LLM as stable opaque handles. LLM returns those same IDs verbatim in
  `turn_start`/`turn_end`; validator resolves back to parent DB idx.
  Eliminates the chunk-relative-index confusion that produced 23%
  rejection on multi-chunk conversations under v1.
- Bulk-transcript turns (kind='call_transcript_segment' with embedded
  "Agent:" / "+phone:" speaker markers concatenated into one blob) are
  SPLIT into speaker-aware sub-turns before the LLM sees them. Sub-turns
  share the parent DB idx so persisted turn_start/end still point at
  the parent row.

Rules unchanged from v1: chronological order, retain short customer
replies, drop empty SMS turns, keep empty call-marker turns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from apps.conversations.models import Conversation


CHUNK_CHAR_BUDGET = 8000
CHUNK_OVERLAP_TURNS = 3


@dataclass
class NormalizedTurn:
    turn_id: str          # LLM-facing opaque handle (e.g. "t0015" or "t0015.0")
    parent_idx: int       # DB turn index — stable, may repeat across split sub-turns
    speaker: str          # customer | agent | system | unknown
    direction: str
    text: str
    occurred_at: Optional[str]
    kind: str
    from_bulk_transcript: bool


@dataclass
class ConversationChunk:
    turns: list[NormalizedTurn]
    chunk_index: int
    is_only_chunk: bool


@dataclass
class TurnIdMap:
    """LLM-facing string turn_id → DB parent idx. Built by
    load_and_normalize, passed to the validator."""
    id_to_parent: dict[str, int] = field(default_factory=dict)

    @property
    def valid_ids(self) -> set[str]:
        return set(self.id_to_parent.keys())


# ---------------------------------------------------------------------------
# Bulk-transcript speaker splitting
# ---------------------------------------------------------------------------


# Speaker prefixes seen in real Spotless voice transcripts:
#   "Agent: ...text..."
#   "System: ...text..."  (rare)
#   "+13164444895: ...text..."
_SPEAKER_PREFIX_SPLIT = re.compile(
    r'(?=(?:^|\s)(?:Agent|System|\+\d{7,15})[:.]?\s+)',
    re.MULTILINE,
)


def _split_bulk_transcript(text: str) -> list[tuple[str, str]]:
    """Return [(speaker, utterance), ...] from a bulk-transcript blob.
    Speaker is 'agent' for 'Agent:', 'system' for 'System:', 'customer'
    for phone prefix. Fallback 'unknown' when no marker."""
    text = text.strip()
    if not text:
        return []
    parts = [p.strip() for p in _SPEAKER_PREFIX_SPLIT.split(text) if p and p.strip()]
    if not parts:
        return [('unknown', text)]
    out: list[tuple[str, str]] = []
    for p in parts:
        m = re.match(r'^(Agent|System|\+\d{7,15})[:.]?\s*(.*)$', p, re.DOTALL)
        if not m:
            out.append(('unknown', p))
            continue
        label, body = m.group(1), m.group(2).strip()
        if not body:
            continue
        if label == 'Agent':
            speaker = 'agent'
        elif label == 'System':
            speaker = 'system'
        else:
            speaker = 'customer'
        out.append((speaker, body))
    return out


# ---------------------------------------------------------------------------
# Loading + normalization
# ---------------------------------------------------------------------------


def _make_turn_id(parent_idx: int, sub: Optional[int] = None) -> str:
    """Zero-padded opaque ID. 4 digits handles up to 9999 turns; if we
    ever exceed that, format still parses via the id_map lookup."""
    base = f't{parent_idx:04d}'
    return base if sub is None else f'{base}.{sub}'


def load_and_normalize(conv: Conversation) -> tuple[list[NormalizedTurn], TurnIdMap]:
    """Load ConversationTurns, drop empty SMS, split bulk transcripts.
    Returns (normalized, turn_id_map). Empty CALL turns kept (a call
    happened even without transcript)."""
    normalized: list[NormalizedTurn] = []
    id_map = TurnIdMap()

    for parent_idx, t in enumerate(
        conv.turns.order_by('occurred_at', 'source_turn_id')
    ):
        kind = (t.metadata or {}).get('kind', '')
        text = (t.text or '').strip()
        is_call = kind in ('call_transcript_segment', 'call_no_transcript')
        occurred_iso = t.occurred_at.isoformat() if t.occurred_at else ''

        if not text and not is_call:
            continue

        if kind == 'call_transcript_segment' and text:
            segments = _split_bulk_transcript(text)
            if len(segments) > 1:
                for sub_i, (speaker, body) in enumerate(segments):
                    tid = _make_turn_id(parent_idx, sub_i)
                    id_map.id_to_parent[tid] = parent_idx
                    normalized.append(NormalizedTurn(
                        turn_id=tid, parent_idx=parent_idx,
                        speaker=speaker, direction=t.direction or 'unknown',
                        text=body, occurred_at=occurred_iso,
                        kind='call_transcript_segment_split',
                        from_bulk_transcript=True,
                    ))
                continue
            # Single-speaker or unparseable bulk — treat as one turn
            tid = _make_turn_id(parent_idx)
            id_map.id_to_parent[tid] = parent_idx
            normalized.append(NormalizedTurn(
                turn_id=tid, parent_idx=parent_idx,
                speaker=t.speaker or 'unknown',
                direction=t.direction or 'unknown',
                text=text, occurred_at=occurred_iso,
                kind='call_transcript_segment', from_bulk_transcript=True,
            ))
            continue

        tid = _make_turn_id(parent_idx)
        id_map.id_to_parent[tid] = parent_idx
        normalized.append(NormalizedTurn(
            turn_id=tid, parent_idx=parent_idx,
            speaker=t.speaker or 'unknown',
            direction=t.direction or 'unknown',
            text=text, occurred_at=occurred_iso,
            kind=kind or 'sms', from_bulk_transcript=False,
        ))

    return normalized, id_map


# ---------------------------------------------------------------------------
# Chunking + merge
# ---------------------------------------------------------------------------


def chunk_conversation(
    turns: list[NormalizedTurn],
    *,
    char_budget: int = CHUNK_CHAR_BUDGET,
    overlap_turns: int = CHUNK_OVERLAP_TURNS,
) -> list[ConversationChunk]:
    if not turns:
        return []
    total_chars = sum(len(t.text) for t in turns)
    if total_chars <= char_budget:
        return [ConversationChunk(turns=list(turns), chunk_index=0, is_only_chunk=True)]

    chunks: list[ConversationChunk] = []
    i, chunk_idx, n = 0, 0, len(turns)
    while i < n:
        acc_chars, j = 0, i
        while j < n and acc_chars + len(turns[j].text) <= char_budget:
            acc_chars += len(turns[j].text)
            j += 1
        if j == i:
            j = i + 1
        chunks.append(ConversationChunk(
            turns=turns[i:j], chunk_index=chunk_idx, is_only_chunk=False,
        ))
        chunk_idx += 1
        if j >= n:
            break
        i = max(j - overlap_turns, i + 1)
    return chunks


def merge_extracted_events(per_chunk_events: list[list[dict]]) -> list[dict]:
    """Dedupe events across overlapping chunks.

    Post-validator, events have INT turn_start/turn_end (parent DB
    indices resolved from LLM's string turn_ids). Dedup key:
    (event_type, actor, turn_start, turn_end)."""
    seen: set[tuple] = set()
    merged: list[dict] = []
    for events in per_chunk_events:
        for ev in events:
            key = (ev.get('event_type'), ev.get('actor'),
                   ev.get('turn_start'), ev.get('turn_end'))
            if key in seen:
                continue
            seen.add(key)
            merged.append(ev)
    return merged


def render_turns_for_prompt(chunk: ConversationChunk) -> str:
    """Render as `[<turn_id>][speaker] text` lines. Voice sub-turns get
    a [voice] marker so the LLM sees they came from a transcript."""
    lines: list[str] = []
    for t in chunk.turns:
        prefix = f'[{t.turn_id}][{t.speaker}]'
        if t.from_bulk_transcript:
            prefix += '[voice]'
        body = t.text.replace('\n', ' ').replace('\r', ' ')
        lines.append(f'{prefix} {body}')
    return '\n'.join(lines)
