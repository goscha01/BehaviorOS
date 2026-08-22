"""Canonical Conversation Context — data types.

Attribute-level shape
---------------------
Every canonical attribute is the winner of a precedence contest
between one or more `Observation`s. The winner is exposed as a
`CanonicalAttribute`; the losers remain visible through
`CanonicalConversationContext.observations` so analyzers can render
"conflict" flags rather than silently trusting the winner.

Every observation carries WHERE it came from (`source`,
`source_field`), WHEN it was observed (`observed_at`), and WHAT
KIND of authority it has (`authority`). The precedence rule uses
all three, not just one — a more recent conversation correction can
supersede an older LB survey answer for the same dimension.

JSON persistence
----------------
`.to_json()` / `.from_json()` roundtrip these dataclasses onto the
`ConversationContext.attributes_json` / `.observations_json` fields
without losing precision. Callers that only need to READ the winner
can use `attributes_json` directly — the shape is stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class Authority(str, Enum):
    """Trust rank for an observation, coarser than a numeric confidence.

    Ordered — comparing two authorities directly is meaningful.
    Higher = more trusted at the same timestamp. When two observations
    conflict, the precedence resolver uses (authority, observed_at,
    source_specificity) to pick a canonical winner.

    Levels:
      * `SOURCE_STRUCTURED` — a source system's structured field the
        source itself is authoritative for (e.g. LB Thumbtack survey
        `Bedrooms` answer). Highest normal trust.
      * `SOURCE_DERIVED` — a source system's derived value that came
        from source-system inference (e.g. Yelp `job_names[0]` mapped
        to a service type by LB's normalizer).
      * `CONVERSATION_EXPLICIT` — an unambiguous statement inside the
        conversation ("I have 3 bedrooms" — direct customer/agent
        assertion). Weaker than source-structured for stable
        attributes but stronger for volatile ones.
      * `CONVERSATION_LLM` — LLM-extracted from conversation text
        (P3 resolved_context). Weakest — LLMs mis-read; may reflect
        agent's assumption rather than customer's statement.
      * `MANUAL` — human-edited (owner review approval). Overrides
        every automated source for that attribute.
    """

    MANUAL = 'manual'
    SOURCE_STRUCTURED = 'source_structured'
    SOURCE_DERIVED = 'source_derived'
    CONVERSATION_EXPLICIT = 'conversation_explicit'
    CONVERSATION_LLM = 'conversation_llm'


# Higher = more trusted. Precedence resolver uses this ordering.
# MANUAL beats everything else at any timestamp because it's a
# human deliberately overriding automated inference.
_AUTHORITY_RANK: dict[Authority, int] = {
    Authority.MANUAL: 100,
    Authority.SOURCE_STRUCTURED: 60,
    Authority.SOURCE_DERIVED: 40,
    Authority.CONVERSATION_EXPLICIT: 55,  # beats SOURCE_DERIVED, not SOURCE_STRUCTURED
    Authority.CONVERSATION_LLM: 20,
}


def authority_rank(a: Authority) -> int:
    return _AUTHORITY_RANK.get(a, 0)


@dataclass
class Observation:
    """One raw observation about one canonical attribute.

    Multiple observations per attribute are expected: a lead survey
    answer AND a customer restating the same fact AND an agent asking
    for clarification are three observations. The resolver picks the
    canonical one but retains them all so analyzers can render
    corroboration / conflict.

    `value` is intentionally typed as `Any` — bedrooms is an int,
    frequency is a string enum, addons is a list. Attribute-specific
    validation lives in the resolvers that produce observations.
    """

    attribute: str
    value: Any
    source: str                         # 'leadbridge', 'conversation', 'serviceflow', 'callio', 'manual'
    source_field: str                   # e.g. 'lead_details:Bedrooms', 'turn:t0025', 'sf.job.attributes.bedrooms'
    observed_at: datetime               # when the source system observed / stated this
    authority: Authority
    raw_value: Optional[str] = None     # what the source literally wrote before parsing
    derivation: Optional[str] = None    # 'literal', 'midpoint', 'enum_mapping', 'first_int', etc.
    text: Optional[str] = None          # the surrounding turn text if applicable (evidence for reviewers)
    source_version: Optional[str] = None  # e.g. LB mapping_version, extractor version

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            'attribute': self.attribute,
            'value': self.value,
            'source': self.source,
            'source_field': self.source_field,
            'observed_at': self.observed_at.isoformat(),
            'authority': self.authority.value,
        }
        if self.raw_value is not None:
            out['raw_value'] = self.raw_value
        if self.derivation is not None:
            out['derivation'] = self.derivation
        if self.text is not None:
            out['text'] = self.text
        if self.source_version is not None:
            out['source_version'] = self.source_version
        return out

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Observation:
        return cls(
            attribute=d['attribute'],
            value=d['value'],
            source=d['source'],
            source_field=d['source_field'],
            observed_at=_parse_iso(d['observed_at']),
            authority=Authority(d['authority']),
            raw_value=d.get('raw_value'),
            derivation=d.get('derivation'),
            text=d.get('text'),
            source_version=d.get('source_version'),
        )


@dataclass
class CanonicalAttribute:
    """The precedence-winning value for one canonical attribute.

    Points back to the winning observation so callers can render "we
    say 3 bedrooms because LB Thumbtack Bedrooms survey answer said
    so on 2026-06-15."
    """

    attribute: str
    value: Any
    winning_observation_index: int  # index into observations list for this attribute
    reason: str                      # human-readable resolver decision explanation

    def to_json(self) -> dict[str, Any]:
        return {
            'attribute': self.attribute,
            'value': self.value,
            'winning_observation_index': self.winning_observation_index,
            'reason': self.reason,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> CanonicalAttribute:
        return cls(
            attribute=d['attribute'],
            value=d['value'],
            winning_observation_index=d['winning_observation_index'],
            reason=d['reason'],
        )


@dataclass
class ConflictReport:
    """Diagnostic surfaced when two competing observations disagree
    on the same canonical attribute.

    Presence in `CanonicalConversationContext.conflicts` is NOT a
    veto — the canonical value is still resolved. Consumers may
    choose to treat conflicted attributes with extra caution (e.g.
    the pricing matcher may downgrade the confidence of a MATCH
    verdict on a cell whose bedrooms attribute is flagged).

    `severity`:
      * `informational` — losing observation is older or weaker,
        conflict is well-explained by precedence.
      * `warning` — competing observations have similar authority
        and comparable freshness. The winner is defensible but
        not obvious.
      * `escalate` — competing observations have equal authority
        and equal freshness. Analyzer should quarantine.
    """

    attribute: str
    winning_value: Any
    losing_values: list[Any]
    severity: str  # 'informational' | 'warning' | 'escalate'
    explanation: str

    def to_json(self) -> dict[str, Any]:
        return {
            'attribute': self.attribute,
            'winning_value': self.winning_value,
            'losing_values': self.losing_values,
            'severity': self.severity,
            'explanation': self.explanation,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ConflictReport:
        return cls(
            attribute=d['attribute'],
            winning_value=d['winning_value'],
            losing_values=list(d['losing_values']),
            severity=d['severity'],
            explanation=d['explanation'],
        )


@dataclass
class CanonicalConversationContext:
    """The Canonical Context Resolution Layer's output for one conversation.

    Attributes carry canonical (winning) values only. Observations carry
    everything the resolver saw (winners + losers). Conflicts carry a
    diagnostic per attribute where the resolver had to pick between
    competing observations.

    source_versions is the cache-invalidation fingerprint — bump any
    input version and the resolver rebuilds cleanly.
    """

    conversation_id: str
    resolved_at: datetime
    attributes: dict[str, CanonicalAttribute] = field(default_factory=dict)
    observations: dict[str, list[Observation]] = field(default_factory=dict)
    conflicts: dict[str, ConflictReport] = field(default_factory=dict)
    source_versions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Coverage summary: {attribute: {'known': bool, 'source': str|None,
    #                                'authority': str|None}}
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, attribute: str) -> Optional[Any]:
        """Convenience: return the canonical value for an attribute, or None
        when the resolver had no evidence."""
        entry = self.attributes.get(attribute)
        return entry.value if entry is not None else None

    def known(self, attribute: str) -> bool:
        return attribute in self.attributes

    def to_json(self) -> dict[str, Any]:
        return {
            'conversation_id': self.conversation_id,
            'resolved_at': self.resolved_at.isoformat(),
            'attributes': {
                k: v.to_json() for k, v in self.attributes.items()
            },
            'observations': {
                k: [o.to_json() for o in lst]
                for k, lst in self.observations.items()
            },
            'conflicts': {
                k: v.to_json() for k, v in self.conflicts.items()
            },
            'source_versions': self.source_versions,
            'coverage': self.coverage,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> CanonicalConversationContext:
        return cls(
            conversation_id=d['conversation_id'],
            resolved_at=_parse_iso(d['resolved_at']),
            attributes={
                k: CanonicalAttribute.from_json(v)
                for k, v in d.get('attributes', {}).items()
            },
            observations={
                k: [Observation.from_json(o) for o in lst]
                for k, lst in d.get('observations', {}).items()
            },
            conflicts={
                k: ConflictReport.from_json(v)
                for k, v in d.get('conflicts', {}).items()
            },
            source_versions=dict(d.get('source_versions', {})),
            coverage=dict(d.get('coverage', {})),
        )


# --------- helpers ---------

def _parse_iso(s: str) -> datetime:
    # Django ISO strings can include 'Z' or +00:00 — Python <3.11 requires
    # +00:00 for fromisoformat. Normalize the trailing 'Z'.
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    return datetime.fromisoformat(s)


# Canonical attribute names — kept as constants so consumers grep
# for symbols instead of magic strings.
class Attr:
    SERVICE = 'service'
    SERVICE_TIER = 'service_tier'
    BEDROOMS = 'bedrooms'
    BATHROOMS = 'bathrooms'
    SQUARE_FOOTAGE = 'square_footage'
    FREQUENCY = 'frequency'
    ADDONS = 'addons'


ALL_ATTRIBUTES = (
    Attr.SERVICE,
    Attr.SERVICE_TIER,
    Attr.BEDROOMS,
    Attr.BATHROOMS,
    Attr.SQUARE_FOOTAGE,
    Attr.FREQUENCY,
    Attr.ADDONS,
)


# Attributes for which structured source data should beat LLM
# conversation inferences by default. Volatile attributes (like
# `addons` — customer often changes their mind mid-conversation)
# are NOT in this list; those use the standard authority ranking.
STABLE_ATTRIBUTES = frozenset({
    Attr.BEDROOMS,
    Attr.BATHROOMS,
    Attr.SQUARE_FOOTAGE,
})
