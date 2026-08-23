"""Dimension base class + shared dataclasses.

A dimension:
  * declares `name` and `version`.
  * implements `evaluate(reconstruction_run, conversation) -> Iterable[DimensionResult]`.
    Returns 0..N results depending on how many independent evaluations
    apply to this conversation (e.g. one price observation vs. many).
  * NEVER raises for missing inputs — missing context → UNKNOWN result
    with a machine-readable reason_code, not an exception.

Evidence requirements are per-dimension. Some FAIL results reference a
configured rule + a conversation turn + the canonical context;
others may reference only a turn timestamp. The base class enforces
minimum structural correctness (state ∈ {PASS/FAIL/UNKNOWN/NOT_APPLICABLE},
severity set iff FAIL, reason_code present for non-PASS results).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional


class State(str, Enum):
    PASS = 'PASS'
    FAIL = 'FAIL'
    UNKNOWN_NOT_EVALUABLE = 'UNKNOWN_NOT_EVALUABLE'
    NOT_APPLICABLE = 'NOT_APPLICABLE'


@dataclass
class EvidenceRef:
    """One typed evidence pointer attached to a DimensionResult.

    `kind` values used today:
      canonical_context     — the resolver's per-conversation row
                              (ref = conversation_id)
      conversation_turn     — one turn (ref = source_turn_id;
                              description carries the quoted text)
      configured_rule       — a ConfiguredBusinessFact row
                              (ref = fact id or subject key)
      reconstructed_fact    — the aggregate that drove the verdict
                              (ref = ReconstructedBusinessFact id)
      observed_fact         — an ObservedBusinessFact row
                              (ref = fact id)
      matcher_output        — the deterministic matcher's per-cell
                              price_comparison JSON (ref = fact id)
    """

    kind: str
    ref: str
    description: str = ''

    def to_json(self) -> dict[str, Any]:
        return {
            'kind': self.kind,
            'ref': self.ref,
            'description': self.description,
        }


@dataclass
class DimensionResult:
    """One evaluation result per (conversation, dimension, subject) tuple.

    `conversation_id=None` means corpus-level (e.g. an aggregated
    pattern finding). The engine persists both via the same
    QualityEvaluation model.

    `subject_key` is the canonical subject the result is about
    (bedrooms, bathrooms, tier, addons, etc. — whatever the dimension
    keys on). For non-pricing dimensions this may be a different
    shape (e.g. Timing might key on {message_direction: 'inbound'}).
    """

    dimension: str
    state: State
    subject_key: dict[str, Any] = field(default_factory=dict)
    conversation_id: Optional[str] = None      # None → corpus-level
    severity: str = ''                          # only set when state=FAIL: info | warning | critical
    reason_code: str = ''
    rationale_text: str = ''
    evidence: list[EvidenceRef] = field(default_factory=list)
    source_reconstructed_fact_id: Optional[str] = None

    def __post_init__(self) -> None:
        # Structural correctness — severity only meaningful for FAIL.
        if self.state == State.FAIL and not self.severity:
            self.severity = 'warning'  # sensible default
        if self.state != State.FAIL and self.severity:
            self.severity = ''  # scrub


class BaseDimension:
    name: str = ''      # e.g. 'pricing_correctness'
    version: str = ''   # e.g. 'qm-v1-pricing.1'

    def evaluate(
        self, *,
        reconstruction_run,          # UnifiedBusinessReconstructionRun
        conversation,                # apps.conversations.models.Conversation
    ) -> Iterable[DimensionResult]:
        """Emit 0..N DimensionResult rows for this conversation.

        MUST NOT raise for missing inputs — use UNKNOWN_NOT_EVALUABLE
        with a reason_code instead. Raising is reserved for actual
        programming errors (e.g. malformed data), which the engine
        catches and records as a QualityRun-level error_message.
        """
        raise NotImplementedError

    def evaluate_corpus(
        self, *,
        reconstruction_run,
    ) -> Iterable[DimensionResult]:
        """Optional — emit corpus-level pattern findings (conversation
        will be None on the persisted rows). Dimensions that only work
        per-conversation can leave this as a no-op.

        Called ONCE per QM run, not once per conversation.
        """
        return []
