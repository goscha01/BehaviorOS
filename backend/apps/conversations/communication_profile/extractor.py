"""CommunicationProfileV1 extractor.

Aggregates HOW a business communicates from its normalized
ConversationTurn rows. Deterministic where possible; a single small
LLM call handles classification-style dimensions (pricing directness,
tone, objection patterns) that require reading representative agent
turns.

Design constraints (from MVP spec):
  - Reuse existing normalized conversations. No new corpus.
  - Support_n + confidence + evidence turn ids on every dimension.
  - Do not use conversion outcome to decide inclusion (this is
    communication *description*, not causal inference).
  - LLM used for classification only, on a bounded sample.

Output shape mirrors default_profile.DEFAULT_COMMUNICATION_PROFILE so
the diff engine can walk the dot-path registry directly.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from collections import Counter
from decimal import Decimal
from typing import Optional

from django.utils import timezone

from apps.conversations.communication_profile.default_profile import (
    DEFAULT_PROFILE_VERSION,
)
from apps.conversations.models import (
    CommunicationProfileRun, ConversationTurn, LearningCorpus,
    LearningCorpusMember, Speaker,
)

logger = logging.getLogger(__name__)


PROFILE_VERSION = 'communication-profile-v1'
DEFAULT_MODEL = 'gpt-4o-mini'

# Sample sizes — small on purpose. This is descriptive extraction, not
# causal inference; we don't need N=200 to know how a business talks.
MAX_TURNS_FOR_LLM_SAMPLE = 40
MAX_TURNS_PER_DIMENSION = 20
MIN_SUPPORT_FOR_HIGH_CONFIDENCE = 30
MIN_SUPPORT_FOR_MEDIUM_CONFIDENCE = 8
MIN_SUPPORT_TO_REPORT = 3  # below this → INSUFFICIENT_EVIDENCE downstream


def create_run(*, org, corpus: LearningCorpus,
               tenant_external_id: str,
               model: str = DEFAULT_MODEL) -> CommunicationProfileRun:
    return CommunicationProfileRun.objects.create(
        org=org,
        corpus=corpus,
        tenant_external_id=tenant_external_id,
        profile_version=PROFILE_VERSION,
        model=model,
        status=CommunicationProfileRun.Status.PENDING,
    )


def run_extraction(
    *,
    run: CommunicationProfileRun,
    llm_client,
    max_turns_sample: int = MAX_TURNS_FOR_LLM_SAMPLE,
) -> CommunicationProfileRun:
    """Execute the extractor. Idempotent-ish: re-running a completed run
    is a no-op; a FAILED run can be retried."""
    if run.status == CommunicationProfileRun.Status.COMPLETED:
        return run
    run.status = CommunicationProfileRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=['status', 'started_at', 'updated_at'])

    try:
        member_ids = list(
            LearningCorpusMember.objects
            .filter(corpus=run.corpus)
            .values_list('conversation_id', flat=True)
        )
        run.corpus_conversations = len(member_ids)

        # Only agent turns — CommunicationProfile is about how the
        # business talks, not the customer.
        agent_turns = list(
            ConversationTurn.objects
            .filter(
                conversation_id__in=member_ids,
                speaker=Speaker.AGENT,
            )
            .exclude(text='')
            .order_by('conversation_id', 'occurred_at')
            .values(
                'id', 'conversation_id', 'text', 'occurred_at',
            )
        )
        run.agent_turns_scanned = len(agent_turns)

        # --- Deterministic aggregations ------------------------------------
        response_style = _aggregate_response_style(agent_turns)
        qualification_style = _aggregate_qualification_style(agent_turns)

        # --- LLM classification (single call per grouped dimension set) ----
        sample = _sample_for_llm(agent_turns, max_turns_sample)
        classified = {}
        if sample and llm_client is not None:
            try:
                classified = _classify_via_llm(
                    sample=sample, run=run, llm_client=llm_client,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    'comm-profile LLM classification failed for run %s: %s',
                    run.id, exc,
                )
                classified = {}

        profile: dict = {
            'profile_version': PROFILE_VERSION,
            'compares_to_default_version': DEFAULT_PROFILE_VERSION,
            'response_style': response_style,
            'qualification_style': qualification_style,
            'pricing_communication': classified.get('pricing_communication',
                                                     _empty_dim_group()),
            'booking_style': classified.get('booking_style',
                                             _empty_dim_group()),
            'objection_style': classified.get('objection_style',
                                               _empty_dim_group()),
            'tone': classified.get('tone', _empty_dim_group()),
        }

        run.profile_json = profile
        run.status = CommunicationProfileRun.Status.COMPLETED
        run.completed_at = timezone.now()
        run.save()
    except Exception as exc:  # noqa: BLE001
        logger.exception('comm-profile extraction failed for run %s', run.id)
        run.status = CommunicationProfileRun.Status.FAILED
        run.error_message = str(exc)[:2000]
        run.completed_at = timezone.now()
        run.save()
        raise
    return run


# ---------------------------------------------------------------------------
# Deterministic aggregations
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+')
_QUESTION = re.compile(r'\?')


def _aggregate_response_style(agent_turns: list[dict]) -> dict:
    """Distribution of agent turn length (sentences + words) and whether
    typical turns contain a single question."""
    if not agent_turns:
        return _empty_dim_group()

    sentence_counts: list[int] = []
    word_counts: list[int] = []
    question_counts: list[int] = []
    evidence_for_length: list[str] = []

    for t in agent_turns:
        text = (t['text'] or '').strip()
        if not text:
            continue
        sc = len([s for s in _SENTENCE_SPLIT.split(text) if s.strip()])
        wc = len([w for w in text.split() if w])
        qc = len(_QUESTION.findall(text))
        sentence_counts.append(sc)
        word_counts.append(wc)
        question_counts.append(qc)
        if len(evidence_for_length) < 10:
            evidence_for_length.append(str(t['conversation_id']))

    n = len(sentence_counts)
    if n == 0:
        return _empty_dim_group()

    median_sentences = int(round(statistics.median(sentence_counts)))
    median_words = int(round(statistics.median(word_counts)))
    p75_words = int(round(_percentile(word_counts, 75)))
    single_q_share = sum(1 for q in question_counts if q == 1) / n
    multi_q_share = sum(1 for q in question_counts if q >= 2) / n

    return {
        'typical_agent_sentences': {
            'value': median_sentences,
            'support_n': n,
            'confidence': _confidence_from_n(n),
            'evidence_conversation_ids': evidence_for_length[:5],
            'observed_p75_words': p75_words,
        },
        'typical_agent_words': {
            'value': median_words,
            'support_n': n,
            'confidence': _confidence_from_n(n),
            'evidence_conversation_ids': evidence_for_length[:5],
            'observed_distribution': {
                'p50': median_words, 'p75': p75_words,
                'p90': int(round(_percentile(word_counts, 90))),
            },
        },
        'asks_one_question_at_a_time': {
            # True when single-question turns dominate over multi-question.
            'value': single_q_share > multi_q_share
                     and single_q_share > 0.20,
            'support_n': n,
            'confidence': _confidence_from_n(n),
            'evidence_conversation_ids': evidence_for_length[:5],
            'observed_shares': {
                'zero_questions': round(
                    sum(1 for q in question_counts if q == 0) / n, 3,
                ),
                'one_question': round(single_q_share, 3),
                'multi_question': round(multi_q_share, 3),
            },
        },
    }


def _aggregate_qualification_style(agent_turns: list[dict]) -> dict:
    """Look at agent turns that contain a question — infer whether the
    business asks one question at a time (median question-count per
    question-bearing turn ≤ 1) vs grouped (> 1)."""
    q_turns = [t for t in agent_turns if '?' in (t['text'] or '')]
    if not q_turns:
        return _empty_dim_group()
    q_counts = [len(_QUESTION.findall(t['text'])) for t in q_turns]
    median_q = statistics.median(q_counts)
    single_share = sum(1 for c in q_counts if c == 1) / len(q_turns)
    grouped_share = sum(1 for c in q_counts if c >= 2) / len(q_turns)
    mode = 'one_at_a_time' if single_share > 0.60 else (
        'grouped' if grouped_share > 0.40 else 'mixed'
    )
    evidence = [str(t['conversation_id']) for t in q_turns[:5]]
    return {
        'questions_per_turn_mode': {
            'value': mode,
            'support_n': len(q_turns),
            'confidence': _confidence_from_n(len(q_turns)),
            'evidence_conversation_ids': evidence,
            'observed_median_questions_per_turn': median_q,
            'observed_shares': {
                'single_question_turns': round(single_share, 3),
                'grouped_question_turns': round(grouped_share, 3),
            },
        },
    }


# ---------------------------------------------------------------------------
# LLM classification
# ---------------------------------------------------------------------------

_LLM_SYSTEM = """You are a communication-profile classifier for a service
business's SMS/voice agent. You are given short samples of REAL AGENT
messages (customer replies are elided for brevity). Return a strict JSON
object describing the agent's communication style along four dimensions.

Rules:
- Base every classification ONLY on the provided samples. If evidence is
  thin, use "insufficient_evidence" for that dimension and set
  support_n to 0 for it.
- Do NOT invent policies (pricing, availability, service rules). You are
  describing communication STYLE.
- Keep example quotes short (≤120 chars). Anonymize any explicit prices
  or times to <PRICE>/<TIME>.

Output schema (JSON):
{
  "pricing_communication": {
     "directness": "direct" | "explain_then_price" | "context_dependent"
                 | "insufficient_evidence",
     "explains_price_range": true | false | null,
     "typical_pattern_examples": [{"quote": "...", "conversation_id": ...}],
     "support_n": <int>
  },
  "booking_style": {
     "proposes_specific_times": true | false | null,
     "typical_transition_pattern": "<short phrase describing how the
        agent moves to booking>" | "insufficient_evidence",
     "confirmation_language": "<verbatim short quote>" | "",
     "support_n": <int>
  },
  "objection_style": {
     "acknowledge_before_responding": true | false | null,
     "typical_patterns": [
        {"objection_type": "price"|"availability"|"trust"|"other",
         "response_pattern": "<short paraphrase>",
         "example_quote": "<short verbatim quote>",
         "support_n": <int>}
     ],
     "support_n": <int>
  },
  "tone": {
     "formality": "casual" | "neutral" | "formal" | "insufficient_evidence",
     "warmth": "low" | "medium" | "high" | "insufficient_evidence",
     "characteristic_phrases": [{"phrase": "<short verbatim>", "support_n": <int>}],
     "support_n": <int>
  }
}
"""


def _sample_for_llm(agent_turns: list[dict], k: int) -> list[dict]:
    if not agent_turns:
        return []
    if len(agent_turns) <= k:
        return agent_turns
    # Simple stride sampling across the whole set — no outcome bias.
    stride = max(1, len(agent_turns) // k)
    return [agent_turns[i] for i in range(0, len(agent_turns), stride)][:k]


def _classify_via_llm(
    *, sample: list[dict], run: CommunicationProfileRun, llm_client,
) -> dict:
    lines = []
    for i, t in enumerate(sample):
        text = (t['text'] or '').replace('\n', ' ').strip()
        if len(text) > 320:
            text = text[:320] + '…'
        lines.append(
            f'#{i+1} (conv={t["conversation_id"]}): {text}'
        )
    user_prompt = (
        'Sample agent messages from a house-cleaning service business. '
        'Classify communication style per the schema.\n\n'
        + '\n'.join(lines)
    )
    result = llm_client.analyze(
        system_prompt=_LLM_SYSTEM,
        user_prompt=user_prompt,
        model=run.model or DEFAULT_MODEL,
        max_tokens=1500,
    )
    run.llm_calls = (run.llm_calls or 0) + 1
    run.llm_cost_usd = (run.llm_cost_usd or Decimal('0')) + (
        result.cost_usd or Decimal('0')
    )
    parsed = result.parsed_json or {}
    if not isinstance(parsed, dict):
        try:
            parsed = json.loads(result.raw_response or '{}')
        except Exception:  # noqa: BLE001
            parsed = {}

    return {
        'pricing_communication': _normalize_dim_group(
            parsed.get('pricing_communication'),
            keys=('directness', 'explains_price_range'),
            base_support=len(sample),
        ),
        'booking_style': _normalize_dim_group(
            parsed.get('booking_style'),
            keys=('proposes_specific_times', 'confirmation_language'),
            base_support=len(sample),
            extra_flat_keys=('typical_transition_pattern',),
        ),
        'objection_style': _normalize_dim_group(
            parsed.get('objection_style'),
            keys=('acknowledge_before_responding',
                  'pricing_objection_approach'),
            base_support=len(sample),
            list_keys=('typical_patterns',),
        ),
        'tone': _normalize_dim_group(
            parsed.get('tone'),
            keys=('formality', 'warmth'),
            base_support=len(sample),
            list_keys=('characteristic_phrases',),
        ),
    }


def _normalize_dim_group(
    payload: Optional[dict],
    *,
    keys: tuple,
    base_support: int,
    list_keys: tuple = (),
    extra_flat_keys: tuple = (),
) -> dict:
    if not isinstance(payload, dict):
        return _empty_dim_group()
    support = int(payload.get('support_n') or 0) or base_support
    out: dict = {}
    for k in keys:
        raw = payload.get(k)
        if raw is None or raw == 'insufficient_evidence':
            out[k] = {
                'value': None,
                'support_n': 0,
                'confidence': 'INSUFFICIENT',
                'evidence_conversation_ids': [],
            }
        else:
            out[k] = {
                'value': raw,
                'support_n': support,
                'confidence': _confidence_from_n(support),
                'evidence_conversation_ids': [],
            }
    for k in extra_flat_keys:
        raw = payload.get(k)
        if raw and raw != 'insufficient_evidence':
            out[k] = {
                'value': raw,
                'support_n': support,
                'confidence': _confidence_from_n(support),
                'evidence_conversation_ids': [],
            }
    for k in list_keys:
        raw = payload.get(k) or []
        if isinstance(raw, list):
            out[k] = raw
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _empty_dim_group() -> dict:
    return {}


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return float(s[f] + (s[c] - s[f]) * (k - f))


def _confidence_from_n(n: int) -> str:
    if n >= MIN_SUPPORT_FOR_HIGH_CONFIDENCE:
        return 'HIGH'
    if n >= MIN_SUPPORT_FOR_MEDIUM_CONFIDENCE:
        return 'MEDIUM'
    if n >= MIN_SUPPORT_TO_REPORT:
        return 'LOW'
    return 'INSUFFICIENT'
