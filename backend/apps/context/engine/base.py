"""Data contracts for the Context Engine.

Design notes worth writing down:

- `SourceOutput` is the Phase-2 Source contract. Every source returns one
  of these. The `facts` / `recommendations` / `warnings` split matches the
  spec — `facts` are observed data (customerProfile, businessInsights,
  conversationHints), `recommendations` are prescribed actions (mapped to
  `recommendedStrategy` on the wire), `warnings` are their own flat list.

- Inside `facts` and `recommendations`, keys are the WIRE slot names
  (`customerProfile`, `businessInsights`, `conversationHints`,
  `recommendedStrategy`). This keeps a stable mapping between a source's
  internal output and where it lands on the wire — the merger doesn't
  need per-source configuration to know where things belong.

- `MergedContext` is the post-merge internal representation. Fact/rec
  values are wrapped with provenance metadata; warnings carry provenance
  as reserved `_source` / `_confidence` / `_generated_at` keys so the
  runtime never sees them but debugging + future learning can.

- `EngineResult` bundles everything the view / logging needs:
  MergedContext + per-source diagnostics + versioning + latency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.utils import timezone


# --- Wire slot constants -----------------------------------------------------

CUSTOMER_PROFILE = 'customerProfile'
BUSINESS_INSIGHTS = 'businessInsights'
CONVERSATION_HINTS = 'conversationHints'
RECOMMENDED_STRATEGY = 'recommendedStrategy'
WARNINGS = 'warnings'

DICT_SLOTS = (CUSTOMER_PROFILE, BUSINESS_INSIGHTS, RECOMMENDED_STRATEGY)
LIST_SLOTS = (CONVERSATION_HINTS, WARNINGS)


# --- Request --------------------------------------------------------------

@dataclass
class ContextRequest:
    """Normalized inbound POST /v1/context body.

    Populated once by the view's boundary layer so every Source receives
    the same object — no per-Source `.get()` chains.
    """

    tenant_id: str
    runtime: str
    channel: str = ''
    event_type: str = ''
    customer_id: str = ''
    lead_id: str = ''
    conversation_id: str = ''
    message: str = ''
    metadata: dict = field(default_factory=dict)
    org: Any = None  # Organization | None. Set after tenant → org lookup.


# --- Source output --------------------------------------------------------

@dataclass
class SourceOutput:
    """What a single Context Source hands back to the engine.

    Contract (from Phase 2 spec):
        {
          "source": "...",
          "priority": 50,
          "confidence": 1.0,
          "facts": {},
          "recommendations": {},
          "warnings": []
        }

    Slot conventions:
    - `facts["customerProfile"]`: dict of `{fact_name: value}`
    - `facts["businessInsights"]`: dict of `{fact_name: value}`
    - `facts["conversationHints"]`: list of hint dicts
    - `recommendations["recommendedStrategy"]`: dict of `{rec_name: value}`
    - `warnings`: list of warning dicts

    A Source may leave any slot empty. The merger treats missing == empty.
    """

    source: str
    priority: int = 50
    confidence: float = 0.0
    facts: dict = field(default_factory=dict)
    recommendations: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    generated_at: datetime = field(default_factory=timezone.now)

    def is_empty(self) -> bool:
        for slot in DICT_SLOTS[:2]:  # customerProfile / businessInsights inside facts
            if self.facts.get(slot):
                return False
        if self.facts.get(CONVERSATION_HINTS):
            return False
        if self.recommendations.get(RECOMMENDED_STRATEGY):
            return False
        if self.warnings:
            return False
        return True


@dataclass
class ProvenanceEntry:
    """Where one fact came from. Attached to every value in the merged package."""

    source: str
    confidence: float
    generated_at: datetime

    def to_dict(self) -> dict:
        return {
            'source': self.source,
            'confidence': self.confidence,
            'generated_at': self.generated_at.isoformat(),
        }


@dataclass
class MergedContext:
    """Internal, provenance-carrying representation of a merged context package.

    Dict-slot values are shape:
        {"value": <the actual value>, "source": ..., "confidence": ..., "generated_at": ...}
    List-slot items carry provenance as reserved `_source` / `_confidence`
    / `_generated_at` keys — flatter than wrapping every item.

    The wire projection (in ContextEngine.wire_projection) strips both forms.
    """

    facts: dict = field(default_factory=dict)
    recommendations: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def is_empty(self) -> bool:
        if any(self.facts.get(slot) for slot in (CUSTOMER_PROFILE, BUSINESS_INSIGHTS)):
            return False
        if self.facts.get(CONVERSATION_HINTS):
            return False
        if self.recommendations.get(RECOMMENDED_STRATEGY):
            return False
        if self.warnings:
            return False
        return True


# --- Per-source diagnostic result -----------------------------------------

@dataclass
class SourceResult:
    """Diagnostic wrapper: what happened when we ran ONE Source.

    Distinct from SourceOutput — SourceOutput is what the Source SAYS,
    SourceResult is what the engine OBSERVED (including timing + errors).
    """

    source: str
    priority: int
    confidence: float
    contributed: bool
    latency_ms: int
    error: str = ''


# --- Engine result --------------------------------------------------------

@dataclass
class EngineResult:
    """Everything ContextEngine.build() returns.

    The view reads `status` + `wire_context` + `confidence` for the wire
    response, and all fields for the ContextRequestLog row.
    """

    status: str  # 'no_context' | 'context'
    confidence: float = 0.0
    wire_context: dict = field(default_factory=dict)
    merged: MergedContext = field(default_factory=MergedContext)
    source_results: list[SourceResult] = field(default_factory=list)
    latency_ms: int = 0
    context_version: str = ''
    generated_at: datetime = field(default_factory=timezone.now)

    @property
    def source_count(self) -> int:
        return sum(1 for r in self.source_results if r.contributed)


# --- Source base class ----------------------------------------------------

class BaseContextSource:
    """Subclass to add a new Context Source.

    Contract:
    - `name`: unique short key. Appears in provenance + logs.
    - `priority`: default merge priority for this Source (0..100). Higher
      priority wins on merge conflicts.
    - `provide()`: read from existing data, return a `SourceOutput`.
      MUST NOT raise; wrap adapter logic in try/except and return an
      empty output. The merger swallows exceptions as a belt AND
      suspenders, but returning early keeps the diagnostics clean.
    """

    name: str = ''
    priority: int = 50

    def provide(self, request: ContextRequest) -> SourceOutput:
        raise NotImplementedError
