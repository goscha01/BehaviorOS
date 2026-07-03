"""Structured-output schema for the analyzer.

The analyzer's job is to emit machine-readable business learning, not
prose. Every analyzer returns a dict matching the schema below (values
may be empty but keys must exist).

Keeping this in one place gives us:
- One authoritative shape for the dashboard, clusterer, and reviewers.
- A version bump story — when the schema changes, `SCHEMA_VERSION` moves
  and old rows can be re-analyzed against the new shape.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 'analysis:v1'

CATEGORIES = (
    'pricing',
    'faq',
    'qualification',
    'playbook',
    'missing_info',
    'tone',
    'other',
)

REQUIRED_KEYS = (
    'summary',
    'category',
    'subcategory',
    'confidence',
    'customer_intent',
    'outcome_analysis',
    'candidate_playbook_rules',
    'candidate_faq',
    'signals',
)


class AnalysisSchemaError(ValueError):
    """Raised when the LLM's output can't be coerced into the schema."""


def empty_analysis() -> dict[str, Any]:
    """Zero-value dict conforming to the schema. Used by the stub provider
    and as a fallback so downstream code can trust the shape."""
    return {
        'summary': '',
        'category': 'other',
        'subcategory': '',
        'confidence': 0.0,
        'customer_intent': '',
        'outcome_analysis': '',
        'candidate_playbook_rules': [],
        'candidate_faq': [],
        'signals': [],
    }


def validate_analysis(payload: Any, evidence_type: str) -> dict[str, Any]:
    """Coerce and validate a parsed LLM response.

    Adds missing keys with empty values (LLMs sometimes drop optional
    lists), clamps confidence to [0, 1], and normalizes the category.
    Raises AnalysisSchemaError if the payload isn't a dict at all — that
    means the LLM failed to produce JSON.
    """
    if not isinstance(payload, dict):
        raise AnalysisSchemaError(
            f'Analyzer for {evidence_type} got non-dict payload: {type(payload).__name__}'
        )

    result = empty_analysis()
    for key in REQUIRED_KEYS:
        if key in payload:
            result[key] = payload[key]

    # Coerce types the analyzer downstream depends on.
    result['summary'] = str(result['summary'])[:2000]
    result['subcategory'] = str(result['subcategory'])[:120]
    result['customer_intent'] = str(result['customer_intent'])[:120]
    result['outcome_analysis'] = str(result['outcome_analysis'])[:1000]

    category = str(result['category']).strip().lower()
    if category not in CATEGORIES:
        category = 'other'
    result['category'] = category

    try:
        confidence = float(result['confidence'])
    except (TypeError, ValueError):
        confidence = 0.0
    result['confidence'] = max(0.0, min(1.0, confidence))

    result['candidate_playbook_rules'] = _clean_rules(result['candidate_playbook_rules'])
    result['candidate_faq'] = _clean_faq(result['candidate_faq'])
    result['signals'] = _clean_signals(result['signals'])
    return result


def _clean_rules(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            confidence = float(item.get('confidence', 0))
        except (TypeError, ValueError):
            confidence = 0.0
        cleaned.append({
            'title': str(item.get('title', ''))[:200],
            'description': str(item.get('description', ''))[:2000],
            'confidence': max(0.0, min(1.0, confidence)),
        })
    return cleaned


def _clean_faq(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cleaned.append({
            'question': str(item.get('question', ''))[:400],
            'answer': str(item.get('answer', ''))[:2000],
        })
    return cleaned


def _clean_signals(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(x)[:200] for x in items if x]
