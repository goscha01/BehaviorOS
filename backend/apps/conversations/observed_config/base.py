"""Shared Pipeline 1D primitives: canonical keys, diff categories,
generic extractor / parser / diff protocol.

Kept domain-neutral so pricing / qualification / FAQ / service_scope
each get their own module but reuse:
  - `canonical_subject_key` — sorted-key JSON + sha256 for join hash
  - `DiffCategory` — the 6-way classification the deterministic
    per-domain diff must emit
  - `MergedDiffRow` — the shape the audit endpoint renders per join
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional


class DiffCategory:
    """Per user spec: 6-way deterministic classification.

    MATCH — subject_key dimensions overlap AND observed value falls
      within configured value (per-domain match rule).
    CONFLICT — subject_key dimensions overlap AND observed value
      materially differs from configured (per-domain conflict rule).
    OBSERVED_NOT_CONFIGURED — observed fact has no compatible
      configured entry.
    CONFIGURED_NOT_OBSERVED — configured fact has no compatible
      observed evidence.
    VARIABLE_CONTEXT_DEPENDENT — observed distribution shows
      systematic variance (large IQR relative to median) that the
      static configured value cannot capture on its own.
    INSUFFICIENT_EVIDENCE — includes PARTIAL_KEY_COMPATIBLE: observed
      key is a subset of configured key so we can't unambiguously
      compare (support too small also lands here).
    """
    MATCH = 'MATCH'
    CONFLICT = 'CONFLICT'
    OBSERVED_NOT_CONFIGURED = 'OBSERVED_NOT_CONFIGURED'
    CONFIGURED_NOT_OBSERVED = 'CONFIGURED_NOT_OBSERVED'
    VARIABLE_CONTEXT_DEPENDENT = 'VARIABLE_CONTEXT_DEPENDENT'
    INSUFFICIENT_EVIDENCE = 'INSUFFICIENT_EVIDENCE'

    ALL = (
        MATCH, CONFLICT, OBSERVED_NOT_CONFIGURED,
        CONFIGURED_NOT_OBSERVED, VARIABLE_CONTEXT_DEPENDENT,
        INSUFFICIENT_EVIDENCE,
    )


def canonical_subject_key(key: dict) -> tuple[str, str, list[str]]:
    """Return (canonical_json, sha256_hex, sorted_dimension_list) for a
    subject key dict. Dimensions are the keys with non-null values —
    used for key-specificity gating in the diff."""
    dims = sorted([k for k, v in key.items() if v is not None])
    normalized = {k: key[k] for k in dims}
    canon = json.dumps(
        normalized, sort_keys=True, separators=(',', ':'),
    )
    sha = hashlib.sha256(canon.encode('utf-8')).hexdigest()
    return (canon, sha, dims)


# ─── Canonical service normalizer (2026-08-21) ─────────────────────
#
# LB stores each ServiceProfile with tenant-editable `slug`, `name`,
# and `service_group`. The observed extractor (LLM) writes canonical
# service tokens like "cleaning" / "carpet". These must land on the
# SAME token for the matcher's compatibility check to succeed.
#
# The alias table is intentionally small and residential-cleaning-
# focused for MVP. Additional verticals extend this map rather than
# adding parallel normalizers.

_SERVICE_ALIASES: dict[str, str] = {
    # residential cleaning family
    'cleaning': 'cleaning',
    'house_cleaning': 'cleaning',
    'house-cleaning': 'cleaning',
    'residential_cleaning': 'cleaning',
    'residential-cleaning': 'cleaning',
    'default_service': 'cleaning',
    'default-service': 'cleaning',
    'default': 'cleaning',
    'home_cleaning': 'cleaning',
    'home-cleaning': 'cleaning',
    # carpet cleaning family (kept separate — MATCH shouldn't cross
    # "cleaning" and "carpet_cleaning")
    'carpet': 'carpet_cleaning',
    'carpet_cleaning': 'carpet_cleaning',
    'carpet-cleaning': 'carpet_cleaning',
    # pressure washing
    'pressure_washing': 'pressure_washing',
    'pressure-washing': 'pressure_washing',
    'power_washing': 'pressure_washing',
}


def normalize_service(raw: str | None) -> str | None:
    """Map any of LB's service slugs / observed extractor tokens to a
    single canonical service token. Unknown strings are returned as-
    is (lowercased) so unfamiliar verticals aren't silently dropped."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    return _SERVICE_ALIASES.get(s, s)


def dimensions_are_compatible(
    a_dims: list[str], b_dims: list[str],
) -> str:
    """Classify (observed, configured) key-dimension relationship:
      'exact_match'      — dimension sets are identical
      'observed_subset'  — observed dims are a proper subset of configured
                            (PARTIAL_KEY_COMPATIBLE — no MATCH/CONFLICT)
      'configured_subset' — configured is a proper subset of observed
                             (rare; treat as PARTIAL_KEY_COMPATIBLE)
      'disjoint'         — no overlap (never comparable)
      'overlapping'      — non-empty intersection but neither is subset
                            (typically PARTIAL_KEY_COMPATIBLE)
    """
    a = set(a_dims)
    b = set(b_dims)
    if not (a & b):
        return 'disjoint'
    if a == b:
        return 'exact_match'
    if a < b:
        return 'observed_subset'
    if b < a:
        return 'configured_subset'
    return 'overlapping'


@dataclass
class MergedDiffRow:
    """The shape the audit endpoint renders per join.

    Every row carries: the join key + which dimensions were present on
    each side, the deterministic verdict, the observed distribution /
    value payload, the configured value, and evidence pointers.
    """
    domain: str
    fact_type: str
    subject_key_json: dict
    observed_dimensions: list[str]
    configured_dimensions: list[str]
    key_dimension_relationship: str
    verdict: str  # DiffCategory value
    verdict_rationale: str
    observed_value: Optional[dict]
    observed_support_n: int
    observed_evidence_conversation_ids: list[str]
    observed_evidence_turn_ids: list[dict]
    configured_value: Optional[dict]
    configured_source_pointer: Optional[dict] = None


def merge_diff_rows_bucketed(rows: list[MergedDiffRow]) -> dict:
    """Group MergedDiffRow list into the DiffCategory buckets."""
    out = {c: [] for c in DiffCategory.ALL}
    for r in rows:
        out.setdefault(r.verdict, []).append(_row_to_dict(r))
    totals = {c: len(out.get(c, [])) for c in DiffCategory.ALL}
    return {'buckets': out, 'totals': totals}


def _row_to_dict(r: MergedDiffRow) -> dict:
    return {
        'domain': r.domain,
        'fact_type': r.fact_type,
        'subject_key': r.subject_key_json,
        'observed_dimensions': r.observed_dimensions,
        'configured_dimensions': r.configured_dimensions,
        'key_dimension_relationship': r.key_dimension_relationship,
        'verdict': r.verdict,
        'verdict_rationale': r.verdict_rationale,
        'observed_value': r.observed_value,
        'observed_support_n': r.observed_support_n,
        'observed_evidence_conversation_ids': (
            r.observed_evidence_conversation_ids
        ),
        'observed_evidence_turn_ids': r.observed_evidence_turn_ids,
        'configured_value': r.configured_value,
        'configured_source_pointer': r.configured_source_pointer,
    }
