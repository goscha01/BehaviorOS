"""Aggregate per-conversation qualification events into
ObservedBusinessFact rows keyed by (field, service_context).

Emits THREE fact_types per field (whichever are present in the data):

  question_asked
    subject_key   = {field, service_context?}
    value_json    = {
      "conversations_with_ask": N,
      "eligible_conversations": total_processed,
      "ask_rate": N / total_processed,
      "sample_phrasings": [up to 8 verbatim quotes],
    }
    support_n = N (distinct conversations)

  answer_provided
    subject_key   = {field, service_context?}
    value_json    = {
      "conversations_with_answer": N,
      "conversations_with_ask": M  (paired to the question fact),
      "capture_rate": N / M          (answers among questions asked),
      "sample_answers": [verbatim],
    }
    support_n = N

  volunteered_before_question
    subject_key   = {field, service_context?}
    value_json    = {
      "conversations_with_volunteered": N,
      "eligible_conversations": total_processed,
      "volunteer_rate": N / total_processed,
      "sample_offers": [verbatim],
    }
    support_n = N

Aggregation invariants:
  - support_n = distinct CONVERSATIONS contributing, never events.
  - If a conversation asks about `bedrooms` five times, it counts ONCE
    toward `question_asked.support_n`.
  - Dedup at intake: (fact_type, subject_key_hash, conversation_id,
    field_turn_id).
  - `other` fields get suffixed with `other_topic` in subject_key
    (so `field=other, other_topic=hoa_rules` is a distinct bucket
    from `field=other, other_topic=vehicle_access`).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field as _field

from apps.conversations.models import (
    Conversation, ObservedBusinessFact,
)
from apps.conversations.observed_config.base import (
    canonical_subject_key,
)


QUALIFICATION_KEY_DIMENSIONS = (
    'field', 'other_topic', 'service_context',
)


def aggregate_and_persist(
    *, run, per_conv_extractions, total_processed: int,
) -> int:
    """Bucket events by (fact_type, subject_key), persist as
    ObservedBusinessFact rows. Returns number of rows written.

    Also emits paired capture-rate context: `answer_provided` rows
    reference their sibling `question_asked` row's support_n so the
    ratio is human-readable in the audit.
    """
    # Bucket by (fact_type, canonical_key_hash).
    buckets: dict[tuple[str, str], _Bucket] = {}
    seen_dedup: set[tuple[str, str, str, str]] = set()

    for extraction in per_conv_extractions:
        for entry in extraction.events:
            fact_type = entry.get('fact_type')
            subject_key = _normalize_subject_key(entry)
            _canon, sha, dims = canonical_subject_key(subject_key)
            field_turn_id = (
                (entry.get('evidence') or {}).get('field_turn_id', '')
            )
            dedup_key = (
                fact_type, sha, extraction.conversation_id,
                field_turn_id,
            )
            if dedup_key in seen_dedup:
                continue
            seen_dedup.add(dedup_key)
            bkey = (fact_type, sha)
            b = buckets.get(bkey)
            if b is None:
                b = _Bucket(
                    fact_type=fact_type,
                    subject_key=subject_key,
                    subject_key_dimensions=dims,
                    subject_key_hash=sha,
                )
                buckets[bkey] = b
            b.add(entry, conversation_id=extraction.conversation_id)

    # Cross-reference: for each `answer_provided` bucket, find the
    # matching `question_asked` bucket (same subject_key_hash) so we
    # can compute capture_rate.
    ask_support_by_hash: dict[str, int] = {}
    for (ft, sha), b in buckets.items():
        if ft == 'question_asked':
            ask_support_by_hash[sha] = len(b.conversation_ids)

    conv_ids_all = {
        cid
        for b in buckets.values()
        for cid in b.conversation_ids
    }
    conv_meta = {}
    for c in Conversation.objects.filter(id__in=conv_ids_all).only(
        'id', 'started_at',
    ):
        conv_meta[str(c.id)] = {'started_at': c.started_at}

    written = 0
    for (ft, sha), b in buckets.items():
        value_json = b.compute_value_json(
            total_processed=total_processed,
            ask_support_by_hash=ask_support_by_hash,
        )
        started_ats = [
            conv_meta.get(cid, {}).get('started_at')
            for cid in b.conversation_ids
        ]
        started_ats = [x for x in started_ats if x is not None]
        first_seen = min(started_ats) if started_ats else None
        last_seen = max(started_ats) if started_ats else None

        ObservedBusinessFact.objects.update_or_create(
            extraction_run=run,
            domain=ObservedBusinessFact.Domain.QUALIFICATION,
            fact_type=ft,
            subject_key_hash=sha,
            defaults={
                'org': run.org,
                'corpus': run.corpus,
                'subject_key_json': b.subject_key,
                'subject_key_dimensions': b.subject_key_dimensions,
                'value_json': value_json,
                'support_n': len(b.conversation_ids),
                'aggregate_confidence': (
                    sum(b.confidences) / len(b.confidences)
                    if b.confidences else 0.0
                ),
                'evidence_conversation_ids': (
                    list(b.conversation_ids)[:20]
                ),
                'evidence_turn_ids': list(b.turn_evidence)[:20],
                'first_seen_at': first_seen,
                'last_seen_at': last_seen,
            },
        )
        written += 1
    return written


def _normalize_subject_key(entry: dict) -> dict:
    out: dict = {}
    field_v = entry.get('field')
    if field_v:
        out['field'] = str(field_v).strip().lower()
    if out.get('field') == 'other':
        topic = str(entry.get('other_topic') or '').strip().lower()
        if topic:
            out['other_topic'] = topic
    sc = entry.get('service_context')
    if sc:
        out['service_context'] = str(sc).strip().lower()
    return out


@dataclass
class _Bucket:
    fact_type: str
    subject_key: dict
    subject_key_dimensions: list[str]
    subject_key_hash: str
    conversation_ids: set = None
    turn_evidence: list = None
    confidences: list = None
    sample_phrasings: list = None

    def __post_init__(self):
        self.conversation_ids = set()
        self.turn_evidence = []
        self.confidences = []
        self.sample_phrasings = []

    def add(self, entry: dict, *, conversation_id: str) -> None:
        self.conversation_ids.add(conversation_id)
        ev = entry.get('evidence') or {}
        self.turn_evidence.append({
            'conversation_id': conversation_id,
            'field_turn_id': ev.get('field_turn_id', ''),
            'referenced_question_turn_id': (
                ev.get('referenced_question_turn_id')
            ),
        })
        try:
            self.confidences.append(
                float(entry.get('confidence', 0.0)),
            )
        except (TypeError, ValueError):
            pass
        if len(self.sample_phrasings) < 8:
            self.sample_phrasings.append({
                'conversation_id': conversation_id,
                'turn_id': ev.get('field_turn_id', ''),
                'evidence_text': (ev.get('evidence_text') or '')[:200],
            })

    def compute_value_json(
        self, *, total_processed: int,
        ask_support_by_hash: dict[str, int],
    ) -> dict:
        n = len(self.conversation_ids)
        payload: dict = {
            'fact_type': self.fact_type,
            'sample_phrasings': self.sample_phrasings,
        }
        if self.fact_type == 'question_asked':
            payload['conversations_with_ask'] = n
            payload['eligible_conversations'] = total_processed
            payload['ask_rate'] = (
                n / total_processed if total_processed > 0 else None
            )
        elif self.fact_type == 'answer_provided':
            asks = ask_support_by_hash.get(self.subject_key_hash, 0)
            payload['conversations_with_answer'] = n
            payload['conversations_with_ask_paired'] = asks
            payload['capture_rate'] = (
                (n / asks) if asks > 0 else None
            )
            payload['eligible_conversations'] = total_processed
            payload['answer_rate'] = (
                n / total_processed if total_processed > 0 else None
            )
        elif self.fact_type == 'volunteered_before_question':
            payload['conversations_with_volunteered'] = n
            payload['eligible_conversations'] = total_processed
            payload['volunteer_rate'] = (
                n / total_processed if total_processed > 0 else None
            )
        return payload
