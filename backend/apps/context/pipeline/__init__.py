"""Evidence Pipeline — Phase 3 orchestrator.

Every runtime interaction now flows through this pipeline. The pipeline:

1. Wraps the inbound `ContextRequest` in an `EvidenceEventDTO`.
2. Persists it as an `EvidenceEvent` (best-effort — persistence failure
   never blocks a wire response).
3. Updates rolling aggregates (`CustomerHistoryAggregate`, `OrgStatistics`).
4. Notifies evidence-event learning hooks.
5. Delegates to `ContextEngine` for context generation.
6. Returns an `EngineResult` — the wire contract is unchanged.

The public API surface below is the entry point for both live runtime
calls (from the view) and historical imports (from
`apps.context.pipeline.imports`).
"""

from apps.context.pipeline.events import EvidenceEventDTO, evidence_from_context_request
from apps.context.pipeline.pipeline import EvidencePipeline, PipelineResult


__all__ = [
    'EvidenceEventDTO',
    'EvidencePipeline',
    'PipelineResult',
    'evidence_from_context_request',
]
