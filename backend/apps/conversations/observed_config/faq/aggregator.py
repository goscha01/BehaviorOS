"""Aggregate per-conversation BUSINESS_FAQ events into
ObservedBusinessFact rows keyed by canonical (topic, intent, other_topic?).

TRANSACTIONAL_OPERATION events do NOT reach this aggregator — they
were persisted as OntologyReviewCandidates upstream.

Each observed FAQ fact retains:
  subject_key = {topic, intent, other_topic?}
  value_json = {
    "observed_variants": [verbatim question phrasings],
    "sample_agent_answers": [verbatim agent responses],
    "eligible_conversations": total_processed,
    "customer_ask_rate": support_n / total_processed,
    "distinct_intents_within_topic": (populated at read time by diff)
  }
  support_n = distinct CONVERSATIONS asking this intent

Same invariants as pricing/qualification:
  - support_n = distinct conversations, never events
  - Dedup at intake: (topic, intent, other_topic, conversation_id,
                      question_turn_id)
  - update_or_create so Celery retries never duplicate rows
"""

from __future__ import annotations

from dataclasses import dataclass, field as _field
from typing import Optional

from apps.conversations.models import (
    Conversation, ObservedBusinessFact,
)
from apps.conversations.observed_config.base import (
    canonical_subject_key,
)


FAQ_KEY_DIMENSIONS = ('topic', 'other_topic', 'intent')


def aggregate_and_persist(
    *, run, per_conv_extractions, total_processed: int,
) -> int:
    buckets: dict[str, _Bucket] = {}
    seen_dedup: set[tuple[str, str, str]] = set()

    for extraction in per_conv_extractions:
        for entry in extraction.business_faq_events:
            subject_key = _normalize_subject_key(entry)
            _canon, sha, dims = canonical_subject_key(subject_key)
            ev = entry.get('evidence') or {}
            dedup_key = (
                sha, extraction.conversation_id,
                ev.get('question_turn_id') or '',
            )
            if dedup_key in seen_dedup:
                continue
            seen_dedup.add(dedup_key)
            b = buckets.get(sha)
            if b is None:
                b = _Bucket(
                    subject_key=subject_key,
                    subject_key_dimensions=dims,
                    subject_key_hash=sha,
                )
                buckets[sha] = b
            b.add(entry, conversation_id=extraction.conversation_id)

    conv_ids_all = {
        cid for b in buckets.values() for cid in b.conversation_ids
    }
    conv_meta = {}
    for c in Conversation.objects.filter(id__in=conv_ids_all).only(
        'id', 'started_at',
    ):
        conv_meta[str(c.id)] = {'started_at': c.started_at}

    written = 0
    for sha, b in buckets.items():
        value_json = b.compute_value_json(total_processed=total_processed)
        started_ats = [
            conv_meta.get(cid, {}).get('started_at')
            for cid in b.conversation_ids
        ]
        started_ats = [x for x in started_ats if x is not None]
        first_seen = min(started_ats) if started_ats else None
        last_seen = max(started_ats) if started_ats else None

        ObservedBusinessFact.objects.update_or_create(
            extraction_run=run,
            domain=ObservedBusinessFact.Domain.FAQ,
            fact_type='customer_question',
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
    topic = str(entry.get('topic') or '').strip().lower()
    if not topic:
        return out
    out['topic'] = topic
    if topic == 'other':
        other = str(entry.get('other_topic') or '').strip().lower()
        if other:
            out['other_topic'] = other
    intent = str(entry.get('intent') or '').strip().lower()
    if intent:
        out['intent'] = intent
    return out


@dataclass
class _Bucket:
    subject_key: dict
    subject_key_dimensions: list[str]
    subject_key_hash: str
    conversation_ids: set = None
    turn_evidence: list = None
    confidences: list = None
    observed_variants: list = None
    sample_agent_answers: list = None

    def __post_init__(self):
        self.conversation_ids = set()
        self.turn_evidence = []
        self.confidences = []
        self.observed_variants = []
        self.sample_agent_answers = []

    def add(self, entry: dict, *, conversation_id: str) -> None:
        first_seen = conversation_id not in self.conversation_ids
        self.conversation_ids.add(conversation_id)
        ev = entry.get('evidence') or {}
        self.turn_evidence.append({
            'conversation_id': conversation_id,
            'question_turn_id': ev.get('question_turn_id', ''),
            'agent_answer_turn_id': ev.get('agent_answer_turn_id'),
        })
        try:
            self.confidences.append(float(entry.get('confidence', 0.0)))
        except (TypeError, ValueError):
            pass
        if len(self.observed_variants) < 12:
            variant = {
                'conversation_id': conversation_id,
                'turn_id': ev.get('question_turn_id', ''),
                'evidence_text': (ev.get('evidence_text') or '')[:250],
            }
            self.observed_variants.append(variant)
        agent_text = ev.get('agent_answer_text')
        if agent_text and len(self.sample_agent_answers) < 8:
            self.sample_agent_answers.append({
                'conversation_id': conversation_id,
                'turn_id': ev.get('agent_answer_turn_id') or '',
                'evidence_text': agent_text[:250],
            })

    def compute_value_json(self, *, total_processed: int) -> dict:
        n = len(self.conversation_ids)
        return {
            'observed_variants': self.observed_variants,
            'sample_agent_answers': self.sample_agent_answers,
            'eligible_conversations': total_processed,
            'customer_ask_rate': (
                n / total_processed if total_processed > 0 else None
            ),
        }
