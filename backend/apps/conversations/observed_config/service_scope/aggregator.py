"""Aggregate per-conversation service-scope events into
ObservedBusinessFact rows (Pipeline 1D Ship D).

Emits ONE ObservedBusinessFact per (fact_type, service, scope_item,
other_topic?). All three fact_types are persisted, but ONLY
`agent_scope_statement` participates in the diff — enforced at the
diff layer, not here.

Subject key intentionally OMITS `relationship` — a bucket that
represents oven-in-deep-clean should retain BOTH "5 convs said
INCLUDED" AND "2 convs said EXTRA_CHARGE" in one value payload, so
CONFLICT / VARIABLE detection has both sides visible. Context
(frequency, condition) is also NOT in the key — retained as a
histogram in value_json.

Same invariants as prior ships: support_n = distinct CONVERSATIONS,
intake dedup by (fact_type, subject_key_hash, conversation_id,
turn_id), update_or_create for retry safety.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from apps.conversations.models import (
    Conversation, ObservedBusinessFact,
)
from apps.conversations.observed_config.base import (
    canonical_subject_key,
)


def aggregate_and_persist(
    *, run, per_conv_extractions, total_processed: int,
) -> int:
    buckets: dict[tuple[str, str], _Bucket] = {}
    seen_dedup: set[tuple[str, str, str, str]] = set()

    for extraction in per_conv_extractions:
        for entry in extraction.scope_events:
            fact_type = entry.get('fact_type')
            subject_key = _normalize_subject_key(entry)
            if 'scope_item' not in subject_key:
                continue
            _canon, sha, dims = canonical_subject_key(subject_key)
            ev = entry.get('evidence') or {}
            dedup_key = (
                fact_type, sha, extraction.conversation_id,
                ev.get('turn_id') or '',
            )
            if dedup_key in seen_dedup:
                continue
            seen_dedup.add(dedup_key)
            key = (fact_type, sha)
            b = buckets.get(key)
            if b is None:
                b = _Bucket(
                    fact_type=fact_type,
                    subject_key=subject_key,
                    subject_key_dimensions=dims,
                    subject_key_hash=sha,
                )
                buckets[key] = b
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
    for (fact_type, sha), b in buckets.items():
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
            domain=ObservedBusinessFact.Domain.SERVICE_SCOPE,
            fact_type=fact_type,
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
    scope_item = str(entry.get('scope_item') or '').strip().lower()
    if not scope_item:
        return out
    out['scope_item'] = scope_item
    if scope_item == 'other':
        other = str(entry.get('other_topic') or '').strip().lower()
        if other:
            out['other_topic'] = other
    svc = entry.get('service')
    if svc:
        out['service'] = str(svc).strip().lower()
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
    # relationship distribution (agent_scope_statement only)
    _rel_by_conv: dict = None  # {conversation_id: [relationship...]}
    # Context distributions (per fact_type; useful across all three)
    _context_by_conv: dict = None  # {conversation_id: [{'frequency','condition'}]}
    sample_variants: list = None

    def __post_init__(self):
        self.conversation_ids = set()
        self.turn_evidence = []
        self.confidences = []
        self._rel_by_conv = {}
        self._context_by_conv = {}
        self.sample_variants = []

    def add(self, entry: dict, *, conversation_id: str) -> None:
        self.conversation_ids.add(conversation_id)
        ev = entry.get('evidence') or {}
        self.turn_evidence.append({
            'conversation_id': conversation_id,
            'turn_id': ev.get('turn_id', ''),
            'actor': ev.get('actor'),
        })
        try:
            self.confidences.append(float(entry.get('confidence', 0.0)))
        except (TypeError, ValueError):
            pass
        # Relationship distribution (only for agent_scope_statement).
        rel = entry.get('relationship')
        if rel:
            self._rel_by_conv.setdefault(
                conversation_id, []
            ).append(rel)
        # Context distribution (all fact_types).
        ctx = entry.get('context') or {}
        if ctx.get('frequency') or ctx.get('condition'):
            self._context_by_conv.setdefault(
                conversation_id, []
            ).append({
                'frequency': ctx.get('frequency'),
                'condition': ctx.get('condition'),
            })
        # Verbatim sample
        if len(self.sample_variants) < 10:
            self.sample_variants.append({
                'conversation_id': conversation_id,
                'turn_id': ev.get('turn_id', ''),
                'actor': ev.get('actor'),
                'relationship': rel,
                'evidence_text': (ev.get('evidence_text') or '')[:250],
                'context': ctx,
            })

    def compute_value_json(self, *, total_processed: int) -> dict:
        n = len(self.conversation_ids)
        payload: dict = {
            'fact_type': self.fact_type,
            'observed_rate': (
                n / total_processed if total_processed > 0 else None
            ),
            'sample_variants': self.sample_variants,
        }
        # Relationship distribution — count DISTINCT conversations
        # per relationship (a conversation with the same relationship
        # stated twice counts once).
        if self._rel_by_conv:
            per_conv_dominant: dict[str, str] = {}
            for cid, rels in self._rel_by_conv.items():
                # If a conversation contains multiple relationships
                # for the same scope_item (rare — agent contradicted
                # themselves), take the LAST as dominant.
                per_conv_dominant[cid] = rels[-1]
            rel_counts = Counter(per_conv_dominant.values())
            payload['relationship_distribution'] = dict(rel_counts)
            payload['relationship_conflict_conversations'] = [
                cid for cid, rels in self._rel_by_conv.items()
                if len(set(rels)) > 1
            ]
        # Context distribution
        if self._context_by_conv:
            freq_counts = Counter()
            cond_counts = Counter()
            for cid, entries in self._context_by_conv.items():
                for e in entries:
                    if e.get('frequency'):
                        freq_counts[e['frequency']] += 1
                    if e.get('condition'):
                        cond_counts[e['condition']] += 1
            payload['context_distribution'] = {
                'frequency': dict(freq_counts),
                'condition': dict(cond_counts),
            }
        return payload
