"""Normalize Quo conversation records into our source-independent shape.

Input contract — a Quo "conversation envelope" as returned by the
LeadBridge Quo proxy endpoint (Phase 9). Structure follows the field
names used by Quo's own API (participantPhoneNumber, phoneNumber,
messages[], calls[]) so the LB proxy can pass records through with
minimal transformation.

    {
      "id": "CN_xyz",                        # Quo conversation ID (required)
      "channel": "voice" | "sms" | "mixed",   # optional; inferred if absent
      "workspaceNumber": "+18139212100",     # tenant's Quo phone
      "participantNumber": "+19045551234",   # customer's phone
      "createdAt": "2026-08-01T09:00:00Z",
      "lastActivityAt": "2026-08-01T10:30:00Z",
      "messages": [ ... SMS/chat messages ... ],
      "calls":    [ ... voice calls, optionally with transcripts ... ],
      "raw":      {...},                     # opaque, retained wholesale
    }

    message = {
      "id": "MSG_abc",
      "body": "text",
      "direction": "in" | "out",
      "fromNumber": "+...",
      "toNumber": "+...",
      "createdAt": "...",
    }

    call = {
      "id": "CALL_xyz",
      "direction": "in" | "out",
      "duration": 180,
      "recordingUrl": "https://...",   # optional
      "createdAt": "...",
      "answeredAt": "...",
      "endedAt": "...",
      "transcript": [                    # optional; missing = no transcript
        {"id": "seg_1", "speaker": "customer" | "agent",
         "text": "...", "startedAt": "...", "confidence": 0.94}
      ],
    }

Output — a `NormalizedConversation` dataclass that maps 1:1 onto the
Conversation model, plus a list of NormalizedTurn objects that map onto
ConversationTurn. The service layer (Phase 7) does the actual DB writes.

Failure modes:
- Missing `id` → raises `QuoNormalizationError`. This is a hard error;
  a Quo record without an ID cannot be safely stored.
- Missing/invalid phone → normalized value stays empty string, record
  otherwise proceeds. Downstream matching may fail but the record is
  preserved.
- Malformed timestamps → best-effort parse; records with no parseable
  timestamp on ANY event (call/message) raise QuoNormalizationError
  (nothing to sort turns by).
- Extra unknown fields → retained verbatim inside `raw_metadata`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from apps.conversations.models import Channel, Direction, Speaker
from apps.conversations.normalization.phone import normalize_e164


class QuoNormalizationError(ValueError):
    """Raised when a Quo record cannot be normalized safely.

    Callers (adapter, importer) MUST catch this per-record and continue
    processing the batch — one bad record must never abort the run.
    """


@dataclass
class NormalizedTurn:
    source_turn_id: str
    speaker: str
    direction: str
    text: str
    occurred_at: datetime
    confidence: Optional[float] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class NormalizedConversation:
    source: str  # 'quo' for this normalizer
    source_conversation_id: str
    channel: str
    customer_phone: str  # E.164 or empty
    customer_email: str  # empty for Quo (email not exposed at conv level)
    started_at: datetime
    ended_at: Optional[datetime]
    metadata: dict = field(default_factory=dict)
    turns: list[NormalizedTurn] = field(default_factory=list)


def normalize_quo_record(record: Mapping[str, Any]) -> NormalizedConversation:
    """Convert one Quo conversation envelope into our normalized shape."""
    if not isinstance(record, Mapping):
        raise QuoNormalizationError('Quo record must be a mapping')

    source_id = record.get('id')
    if not source_id or not isinstance(source_id, str):
        raise QuoNormalizationError('Quo record is missing "id"')

    messages = _as_list(record.get('messages'))
    calls = _as_list(record.get('calls'))

    turns: list[NormalizedTurn] = []
    for msg in messages:
        try:
            turns.append(_normalize_message(msg, conv_id=source_id))
        except _SkipTurn:
            # Individual malformed turn — skip it, keep the rest.
            continue

    for call in calls:
        try:
            turns.extend(_normalize_call(call, conv_id=source_id))
        except _SkipTurn:
            continue

    if not turns:
        raise QuoNormalizationError(
            f'Quo conversation {source_id} has no processable turns'
        )

    # started_at / ended_at derived from turn timestamps rather than trusting
    # a conversation-level field that may lag behind the actual events.
    turn_times = [t.occurred_at for t in turns]
    started_at = _coerce_datetime(record.get('createdAt')) or min(turn_times)
    ended_at = _coerce_datetime(record.get('lastActivityAt')) or max(turn_times)

    channel = _infer_channel(record.get('channel'), messages=messages, calls=calls)
    customer_phone = normalize_e164(record.get('participantNumber')) or ''

    metadata = {
        'workspace_number': record.get('workspaceNumber', ''),
        'raw': record.get('raw', {}),
    }
    # Preserve any unknown top-level Quo fields so future normalization
    # runs can recover them.
    known = {
        'id', 'channel', 'workspaceNumber', 'participantNumber',
        'createdAt', 'lastActivityAt', 'messages', 'calls', 'raw',
    }
    extra = {k: v for k, v in record.items() if k not in known}
    if extra:
        metadata['unknown_fields'] = extra

    # Deterministic turn ordering: by timestamp, then by source_turn_id
    # to break ties for events that share a second boundary.
    turns.sort(key=lambda t: (t.occurred_at, t.source_turn_id))

    return NormalizedConversation(
        source='quo',
        source_conversation_id=source_id,
        channel=channel,
        customer_phone=customer_phone,
        customer_email='',
        started_at=started_at,
        ended_at=ended_at,
        metadata=metadata,
        turns=turns,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SkipTurn(Exception):
    """Signal from turn-level normalizers that this turn should be skipped
    but the parent conversation is fine to persist."""


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return []


def _infer_channel(
    supplied: Any, *, messages: list, calls: list
) -> str:
    if isinstance(supplied, str) and supplied.lower() in (
        Channel.VOICE, Channel.SMS, Channel.CHAT
    ):
        return supplied.lower()
    if calls and messages:
        # Both present: pick the dominant medium by count.
        return Channel.VOICE if len(calls) >= len(messages) else Channel.SMS
    if calls:
        return Channel.VOICE
    if messages:
        return Channel.SMS
    return Channel.UNKNOWN


def _normalize_message(msg: Mapping[str, Any], *, conv_id: str) -> NormalizedTurn:
    if not isinstance(msg, Mapping):
        raise _SkipTurn
    msg_id = msg.get('id') or msg.get('providerMessageId')
    occurred_at = _coerce_datetime(msg.get('createdAt'))
    if not msg_id or not occurred_at:
        raise _SkipTurn

    direction = _map_direction(msg.get('direction'))
    speaker = Speaker.CUSTOMER if direction == Direction.INBOUND else Speaker.AGENT
    text = msg.get('body') or ''

    return NormalizedTurn(
        source_turn_id=f'msg:{msg_id}',
        speaker=speaker,
        direction=direction,
        text=text,
        occurred_at=occurred_at,
        confidence=None,
        metadata={
            'kind': 'sms',
            'from': msg.get('fromNumber') or msg.get('from') or '',
            'to': msg.get('toNumber') or msg.get('to') or '',
            'status': msg.get('status', ''),
        },
    )


def _normalize_call(
    call: Mapping[str, Any], *, conv_id: str
) -> Iterable[NormalizedTurn]:
    """One call yields either N transcript segments (if transcript is present)
    or one "audio-only" summary turn (if not).

    We always emit at least one turn per call — a call with no transcript is
    still evidence that a call happened, and downstream analysis should be
    able to see it.
    """
    if not isinstance(call, Mapping):
        raise _SkipTurn

    call_id = call.get('id')
    call_started = (
        _coerce_datetime(call.get('answeredAt'))
        or _coerce_datetime(call.get('createdAt'))
    )
    if not call_id or not call_started:
        raise _SkipTurn

    direction = _map_direction(call.get('direction'))
    transcript = _as_list(call.get('transcript'))

    if not transcript:
        # No transcript — emit a single audio-only marker turn so the call
        # is visible in the conversation history.
        yield NormalizedTurn(
            source_turn_id=f'call:{call_id}',
            # Speaker unknown for audio-only records — the caller vs
            # dispatcher split is only recoverable via transcript.
            speaker=Speaker.UNKNOWN,
            direction=direction,
            text='',
            occurred_at=call_started,
            confidence=None,
            metadata={
                'kind': 'call_no_transcript',
                'duration_seconds': call.get('duration'),
                'recording_url': call.get('recordingUrl', ''),
                'ended_at': call.get('endedAt', ''),
            },
        )
        return

    # Transcript present — one turn per segment.
    for idx, seg in enumerate(transcript):
        if not isinstance(seg, Mapping):
            continue
        seg_ts = _coerce_datetime(seg.get('startedAt')) or call_started
        seg_id = seg.get('id') or f'{call_id}:seg:{idx}'
        speaker_raw = (seg.get('speaker') or '').lower()
        speaker = speaker_raw if speaker_raw in (
            Speaker.CUSTOMER, Speaker.AGENT, Speaker.SYSTEM
        ) else Speaker.UNKNOWN
        yield NormalizedTurn(
            source_turn_id=f'call:{call_id}:{seg_id}',
            speaker=speaker,
            # Voice transcript direction reflects the CALL direction (whole
            # call was inbound or outbound); per-segment direction doesn't
            # apply the same way SMS does.
            direction=direction,
            text=seg.get('text') or '',
            occurred_at=seg_ts,
            confidence=_coerce_float(seg.get('confidence')),
            metadata={
                'kind': 'call_transcript_segment',
                'call_id': call_id,
                'segment_index': idx,
                'duration_seconds': call.get('duration'),
                'recording_url': call.get('recordingUrl', ''),
            },
        )


def _map_direction(value: Any) -> str:
    if not isinstance(value, str):
        return Direction.UNKNOWN
    v = value.lower()
    if v in ('in', 'inbound', 'incoming'):
        return Direction.INBOUND
    if v in ('out', 'outbound', 'outgoing'):
        return Direction.OUTBOUND
    return Direction.UNKNOWN


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    # Handle common ISO-8601 forms including trailing 'Z'.
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
