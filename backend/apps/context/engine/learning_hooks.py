"""Extension points for future Learning Modules.

**Phase 2 explicitly ships NO learning.** This file exists to name the seams
we'll cut when learning arrives, so the engine doesn't have to be
re-architected then. Registering a hook today is a no-op.

Three seams the spec calls out:

1. Evidence — Learning Modules consume evidence (already produced by the
   `apps.learning` app). We do not re-expose that here; a Learning Module
   can query `EvidenceInsight` directly.

2. Context Packages — every merged package we emit is offered to hooks
   via `notify_context_built()`. A future analyzer can subscribe here to
   build a shadow evaluation without touching the runtime response path.

3. Recommendations — a Learning Module may want to inject or reweight
   `recommendedStrategy` entries. `mutate_result()` is the reserved seam:
   hooks receive the EngineResult BEFORE the wire projection is computed
   and may return a modified copy. Today it's a pass-through.

Contract:
- Hooks MUST NOT raise. If a hook throws, the engine logs + ignores and
  continues with the pre-hook result. Same "one broken source doesn't
  break the endpoint" invariant.
- Hooks are registered via `register_hook`. Order matters: hooks run in
  registration order and each sees the output of the previous one.

Phase 3 adds a fourth seam:

4. Evidence Events — fires per event handled by `EvidencePipeline`,
   AFTER persistence but BEFORE context is built. Learning Modules that
   want to update their own aggregates / indexes on every event
   subscribe here. Same "log-and-skip" isolation as the other seams.

Nothing is registered by default. Registration functions are exported so
Phase 3+ modules can wire themselves in without editing engine code.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, TYPE_CHECKING

from apps.context.engine.base import ContextRequest, EngineResult

if TYPE_CHECKING:
    from apps.context.models import EvidenceEvent
    from apps.context.pipeline.events import EvidenceEventDTO


logger = logging.getLogger(__name__)


ContextBuiltHook = Callable[[ContextRequest, EngineResult], None]
MutateResultHook = Callable[[ContextRequest, EngineResult], EngineResult]
EvidenceEventHook = Callable[[Any, Any], None]
# Signature: (EvidenceEventDTO, EvidenceEvent | None) — the DB row may be
# None when persistence failed. Learning Modules should be able to fall
# back to the DTO alone in that case.


_CONTEXT_BUILT_HOOKS: list[ContextBuiltHook] = []
_MUTATE_RESULT_HOOKS: list[MutateResultHook] = []
_EVIDENCE_EVENT_HOOKS: list[EvidenceEventHook] = []


def register_context_built_hook(hook: ContextBuiltHook) -> ContextBuiltHook:
    _CONTEXT_BUILT_HOOKS.append(hook)
    return hook


def register_mutate_result_hook(hook: MutateResultHook) -> MutateResultHook:
    _MUTATE_RESULT_HOOKS.append(hook)
    return hook


def register_evidence_event_hook(hook: EvidenceEventHook) -> EvidenceEventHook:
    _EVIDENCE_EVENT_HOOKS.append(hook)
    return hook


def notify_context_built(request: ContextRequest, result: EngineResult) -> None:
    """Invoke every context-built hook. Failures are logged, not raised."""
    for hook in _CONTEXT_BUILT_HOOKS:
        try:
            hook(request, result)
        except Exception:
            logger.exception('Context-built hook %s failed', getattr(hook, '__name__', hook))


def notify_evidence_event(dto, event) -> None:
    """Invoke every evidence-event hook.

    `dto` is the EvidenceEventDTO; `event` is the persisted EvidenceEvent
    row or None (persistence may have failed — hooks should not blindly
    dereference it).
    """
    for hook in _EVIDENCE_EVENT_HOOKS:
        try:
            hook(dto, event)
        except Exception:
            logger.exception('Evidence-event hook %s failed', getattr(hook, '__name__', hook))


def mutate_result(request: ContextRequest, result: EngineResult) -> EngineResult:
    """Run mutate-result hooks. Broken hooks are logged and skipped —
    the un-mutated result carries through.
    """
    current = result
    for hook in _MUTATE_RESULT_HOOKS:
        try:
            new_result = hook(request, current)
            if new_result is not None:
                current = new_result
        except Exception:
            logger.exception('Mutate-result hook %s failed', getattr(hook, '__name__', hook))
    return current


def _clear_for_tests() -> None:
    _CONTEXT_BUILT_HOOKS.clear()
    _MUTATE_RESULT_HOOKS.clear()
    _EVIDENCE_EVENT_HOOKS.clear()
