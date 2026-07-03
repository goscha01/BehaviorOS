"""Text fingerprinting for candidate recommendations.

Phase 1 clustering uses tokenized Jaccard similarity — no embeddings, no
external dependencies. The pipeline stores the sorted-token list on each
candidate and compares sets at cluster time.

Design choices worth calling out:
- Stopwords are minimal and deliberately keep domain-specific nouns
  (customer, pets, price) intact — those are the load-bearing tokens.
- Naive singularization strips trailing `s` from tokens longer than 3
  chars. This folds pet/pets, supply/supplies, chemical/chemicals into
  one bucket without pulling in NLTK.
- Similarity is Jaccard on token sets. Simple, fast, and good enough
  until we have enough volume to justify embeddings.
"""

from __future__ import annotations

import re
from typing import Iterable

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset({
    'a', 'an', 'the', 'and', 'or', 'but', 'to', 'of', 'in', 'on', 'at', 'for',
    'with', 'from', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'should',
    'could', 'can', 'may', 'might', 'must', 'shall', 'this', 'that', 'these',
    'those', 'about', 'as', 'if', 'when', 'while', 'because', 'so', 'than',
    'then', 'more', 'most', 'less', 'least', 'some', 'any', 'all', 'no',
    'not', 'we', 'us', 'our', 'you', 'your', 'they', 'them', 'their',
    'it', 'its', 'his', 'her', 'him', 'she', 'he',
    # Domain filler — vocabulary that appears in nearly every recommendation
    # and would otherwise inflate Jaccard for unrelated ideas.
    'mention', 'mentioning', 'tell', 'telling', 'note', 'ask', 'asking',
    'proactively', 'more', 'better',
})


def tokenize(text: str) -> list[str]:
    """Return normalized token list from arbitrary text.

    Steps: lowercase → split on non-alphanumerics → drop stopwords →
    naive singularize → drop tokens ≤ 2 chars → dedupe → sort.
    """
    if not text:
        return []
    lower = text.lower()
    raw = _TOKEN_RE.findall(lower)
    out: set[str] = set()
    for token in raw:
        if token in _STOPWORDS:
            continue
        if len(token) <= 2:
            continue
        singular = _singularize(token)
        out.add(singular)
    return sorted(out)


def fingerprint(text: str) -> str:
    """Canonical space-joined token string. Stable across identical inputs."""
    return ' '.join(tokenize(text))


def union_fingerprint(texts: Iterable[str]) -> tuple[str, list[str]]:
    """Fingerprint for a group of texts. Returns (canonical_string, sorted_token_list)."""
    tokens: set[str] = set()
    for text in texts:
        tokens.update(tokenize(text))
    sorted_tokens = sorted(tokens)
    return ' '.join(sorted_tokens), sorted_tokens


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard similarity on two token iterables. Empty-vs-empty returns 0."""
    set_a = set(a)
    set_b = set(b)
    if not set_a and not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


def _singularize(word: str) -> str:
    """Very naive plural stripping. Handles the common '-ies' → '-y' and
    trailing '-s' cases without pulling in an NLP library.
    """
    if len(word) > 4 and word.endswith('ies'):
        return word[:-3] + 'y'
    if len(word) > 3 and word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
        return word[:-1]
    return word
