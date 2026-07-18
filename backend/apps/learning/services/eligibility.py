"""Promotion eligibility evaluator.

Decides whether a given EvidenceEvent should enter the learning corpus
(become an EvidenceInsight) or be filtered out. Every event gets exactly
one decision — no event stays perpetually unprocessed.

Two rules of thumb:
    1. When in doubt, SKIP. A recommendation trained on synthetic /
       diagnostic / verification traffic is worse than no recommendation.
    2. Skipping is NOT deletion. Skipped events stay in EvidenceEvent
       so operators can audit "why aren't we learning from this?" and
       tune the rules — but they never enter EvidenceInsight and thus
       never influence recommendations.

The evaluator is a pure function of the event + settings — no DB
side-effects, no LLM. Trivially testable, trivially explainable.

Decision categories (persisted verbatim into `EvidenceEvent.promotion_reason`
when status=SKIPPED):

    skip_diagnostic   — contextRequestId or payload marker identifies this
                        as a verification/test probe (not a real customer).
    skip_incomplete   — no meaningful transcript/summary/outcome, or the
                        call was too short to have produced useful signal.
    skip_synthetic    — customer_id matches a known test/synthetic pattern.
    skip_duplicate    — the event has already been promoted (idempotency
                        guard for beat re-runs against manually-reset rows).
    skip_unsupported  — historical event (adapter path), or event_type
                        not on the allow-list, or unknown runtime.

Runtime configuration (config/settings/*):

    LEARNING_MIN_CALL_DURATION_SECONDS      default 15
    LEARNING_ALLOW_MISSING_RUNTIME_OUTCOME  default False
    LEARNING_DIAGNOSTIC_CONTEXT_ID_PREFIXES default ('ctx_verify', 'ctx_test', 'ctx_smoke')
    LEARNING_DIAGNOSTIC_PAYLOAD_MARKERS     default ('diagnostic', 'is_test', 'test_run')
    LEARNING_SYNTHETIC_CUSTOMER_IDS         default ('+18135551234', '+15555550100', ...)
    LEARNING_SUPPORTED_EVENT_TYPES          default ('call_completed',)
    LEARNING_SUPPORTED_RUNTIMES             default ('callio', 'leadbridge', 'serviceflow')
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from django.conf import settings
from django.db import models

from apps.context.models import EvidenceEvent


# --- Decision result -------------------------------------------------


class PromotionDecision(models.TextChoices):
    """Single-source-of-truth vocabulary for skip categories. Same values
    persisted into EvidenceEvent.promotion_reason so operators can
    aggregate skips by reason without a translation layer."""

    ELIGIBLE = 'eligible', 'Eligible for promotion'
    SKIP_DIAGNOSTIC = 'skip_diagnostic', 'Verification / test probe — not a real customer'
    SKIP_INCOMPLETE = 'skip_incomplete', 'No meaningful content or below duration threshold'
    SKIP_SYNTHETIC = 'skip_synthetic', 'Synthetic customer identifier'
    SKIP_DUPLICATE = 'skip_duplicate', 'Already promoted — beat re-run guard'
    SKIP_UNSUPPORTED = 'skip_unsupported', 'Event type / runtime / source_kind not supported'


@dataclass(frozen=True)
class EligibilityResult:
    decision: str  # one of PromotionDecision values
    detail: str = ''  # human-readable specifics ("duration 3s < min 15s")

    @property
    def is_eligible(self) -> bool:
        return self.decision == PromotionDecision.ELIGIBLE


# --- Defaults -------------------------------------------------------


_DEFAULT_MIN_DURATION_SECONDS = 15
_DEFAULT_DIAGNOSTIC_PREFIXES = ('ctx_verify', 'ctx_test', 'ctx_smoke')
_DEFAULT_DIAGNOSTIC_MARKERS = ('diagnostic', 'is_test', 'test_run')
# Fake customer numbers used across our own verification curls + fixtures.
# +1813555XXXX and +1555XXXXXXX are reserved-for-testing ranges (the entire
# +1 555 01XX block is designated for fictional use). Real customer traffic
# never uses these.
_DEFAULT_SYNTHETIC_CUSTOMERS = (
    '+18135551234',
    '+15555550100',
    '+15555550101',
    '+15555550199',
)
_DEFAULT_SUPPORTED_EVENT_TYPES = ('call_completed',)
_DEFAULT_SUPPORTED_RUNTIMES = ('callio', 'leadbridge', 'serviceflow')


def _cfg(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


# --- Rule primitives -----------------------------------------------


def _payload_metadata(event: EvidenceEvent) -> Mapping[str, Any]:
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    md = payload.get('metadata')
    return md if isinstance(md, Mapping) else {}


def _looks_diagnostic(event: EvidenceEvent) -> tuple[bool, str]:
    """The evaluator's first line of defense — anything that looks like
    OUR OWN traffic (verification curls, smoke tests, LB probes) is
    filtered before it can contaminate learning."""
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    md = _payload_metadata(event)

    prefixes = tuple(_cfg('LEARNING_DIAGNOSTIC_CONTEXT_ID_PREFIXES', _DEFAULT_DIAGNOSTIC_PREFIXES))
    ctx_id = str(payload.get('contextRequestId', ''))
    for prefix in prefixes:
        if ctx_id.startswith(prefix):
            return True, f'contextRequestId starts with {prefix!r}'

    markers = tuple(_cfg('LEARNING_DIAGNOSTIC_PAYLOAD_MARKERS', _DEFAULT_DIAGNOSTIC_MARKERS))
    # Truthy value under any marker key in payload OR metadata.
    for marker in markers:
        if payload.get(marker) or md.get(marker):
            return True, f'payload marker {marker!r} is truthy'

    return False, ''


def _looks_synthetic(event: EvidenceEvent) -> tuple[bool, str]:
    synthetic = tuple(_cfg('LEARNING_SYNTHETIC_CUSTOMER_IDS', _DEFAULT_SYNTHETIC_CUSTOMERS))
    if event.customer_id and event.customer_id in synthetic:
        return True, f'customer_id {event.customer_id!r} is in synthetic set'
    return False, ''


def _has_meaningful_content(event: EvidenceEvent) -> tuple[bool, str]:
    """Some evidence signal must be present — even just a runtimeOutcome or
    a non-trivial transcript excerpt. An empty envelope teaches nothing."""
    md = _payload_metadata(event)
    if event.message_excerpt and event.message_excerpt.strip():
        return True, ''
    if md.get('runtimeOutcome'):
        return True, ''
    summary = md.get('aiDecisionSummary')
    if isinstance(summary, Mapping) and any(v not in (None, '', False, [], {}) for v in summary.values()):
        return True, ''
    transcript_ref = md.get('transcriptRef')
    if transcript_ref:
        return True, ''
    return False, 'no transcript/summary/outcome/transcriptRef'


def _meets_min_duration(event: EvidenceEvent) -> tuple[bool, str]:
    min_seconds = int(_cfg('LEARNING_MIN_CALL_DURATION_SECONDS', _DEFAULT_MIN_DURATION_SECONDS))
    if min_seconds <= 0:
        return True, ''
    md = _payload_metadata(event)
    duration = md.get('durationSeconds')
    if duration is None:
        # Missing duration → treat as unknown, allow through (avoid false
        # negatives on events that pre-date the field). Log-only concern.
        return True, ''
    try:
        d = float(duration)
    except (TypeError, ValueError):
        return True, ''
    if d < min_seconds:
        return False, f'duration {d}s < min {min_seconds}s'
    return True, ''


def _has_runtime_outcome(event: EvidenceEvent) -> tuple[bool, str]:
    if _cfg('LEARNING_ALLOW_MISSING_RUNTIME_OUTCOME', False):
        return True, ''
    md = _payload_metadata(event)
    outcome = md.get('runtimeOutcome')
    if outcome:
        return True, ''
    return False, 'metadata.runtimeOutcome absent or empty'


def _is_supported(event: EvidenceEvent) -> tuple[bool, str]:
    if event.source_kind != EvidenceEvent.SourceKind.RUNTIME:
        return False, f'source_kind={event.source_kind} (adapter path handles historical)'
    supported_events = tuple(_cfg('LEARNING_SUPPORTED_EVENT_TYPES', _DEFAULT_SUPPORTED_EVENT_TYPES))
    if event.event_type not in supported_events:
        return False, f'event_type={event.event_type!r} not in supported set {supported_events!r}'
    supported_runtimes = tuple(_cfg('LEARNING_SUPPORTED_RUNTIMES', _DEFAULT_SUPPORTED_RUNTIMES))
    if event.runtime not in supported_runtimes:
        return False, f'runtime={event.runtime!r} not in supported set {supported_runtimes!r}'
    return True, ''


# --- Evaluator ------------------------------------------------------


def evaluate_eligibility(event: EvidenceEvent) -> EligibilityResult:
    """Apply the eligibility ladder. First failing rule wins — determinism
    matters for downstream analytics ("why did we skip these?").

    Order is deliberate:
      duplicate → unsupported → diagnostic → synthetic → incomplete
    Duplicate wins early so re-runs against already-promoted events terminate
    fast. Unsupported wins next so we don't waste effort on historical events
    that follow a different path. Diagnostic wins over synthetic so we don't
    mask an "our own probe" scenario with a customer-ID rule that could later
    change. Incomplete is last — it's the most nuanced, so we let the cheaper
    filters catch known-bad cases first.
    """
    if event.promotion_status == EvidenceEvent.PromotionStatus.PROMOTED:
        return EligibilityResult(
            decision=PromotionDecision.SKIP_DUPLICATE,
            detail='already promoted (promotion_status=PROMOTED)',
        )

    ok, why = _is_supported(event)
    if not ok:
        return EligibilityResult(decision=PromotionDecision.SKIP_UNSUPPORTED, detail=why)

    is_diag, why = _looks_diagnostic(event)
    if is_diag:
        return EligibilityResult(decision=PromotionDecision.SKIP_DIAGNOSTIC, detail=why)

    is_synth, why = _looks_synthetic(event)
    if is_synth:
        return EligibilityResult(decision=PromotionDecision.SKIP_SYNTHETIC, detail=why)

    ok, why = _meets_min_duration(event)
    if not ok:
        return EligibilityResult(decision=PromotionDecision.SKIP_INCOMPLETE, detail=why)

    ok, why = _has_meaningful_content(event)
    if not ok:
        return EligibilityResult(decision=PromotionDecision.SKIP_INCOMPLETE, detail=why)

    ok, why = _has_runtime_outcome(event)
    if not ok:
        return EligibilityResult(decision=PromotionDecision.SKIP_INCOMPLETE, detail=why)

    return EligibilityResult(decision=PromotionDecision.ELIGIBLE)
