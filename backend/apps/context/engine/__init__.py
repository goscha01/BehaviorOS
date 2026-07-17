"""Context Engine — the top-level orchestrator for POST /v1/context.

Phase 2 rename: what used to live under `apps.context.services` is now
concentrated here. The public wire contract is unchanged; internally the
Engine composes independently-authored Context Sources into one Merged
Context package, records provenance, and hands the wire projection to the
view.

Public names exported here are what the rest of the app should import.
Everything else is implementation detail of the engine package.
"""

from apps.context.engine.base import (
    BaseContextSource,
    ContextRequest,
    EngineResult,
    MergedContext,
    ProvenanceEntry,
    SourceOutput,
    SourceResult,
)
from apps.context.engine.context_engine import ContextEngine, log_context_request
from apps.context.engine.registry import (
    get_source,
    iter_registered,
    register,
)


# Bumped whenever the merger's algorithm changes in a way that would make
# a stored MergedContext incomparable with a fresh one. Persisted on every
# ContextRequestLog row so ops can filter historical data by version.
CONTEXT_VERSION = '2.0'


__all__ = [
    'BaseContextSource',
    'ContextEngine',
    'ContextRequest',
    'CONTEXT_VERSION',
    'EngineResult',
    'MergedContext',
    'ProvenanceEntry',
    'SourceOutput',
    'SourceResult',
    'get_source',
    'iter_registered',
    'log_context_request',
    'register',
]
