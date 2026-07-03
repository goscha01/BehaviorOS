"""Base adapter contract + registry.

Adapters register themselves at import time by decorating their class with
`@register`. The ingestion service looks up adapters by `source_system`
key. This keeps the engine unaware of specific sources — adding a new
source (HireFunnel, ProofPix, FixLoop, Google Reviews, ...) means writing
one class and importing it in `adapters/__init__.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar, Iterable, Iterator

from apps.learning.adapters.dto import Evidence


class EvidenceSourceAdapter(ABC):
    """Contract every source adapter must implement.

    Subclasses set `source_system` as a class attribute — that string is
    the registry key and becomes `Evidence.source_system` for every record
    the adapter yields.
    """

    source_system: ClassVar[str]

    @abstractmethod
    def fetch_since(self, since: datetime | None) -> Iterable[Evidence]:
        """Yield Evidence for events at or after `since`.

        If `since` is None, adapters may choose a sensible default lookback
        (e.g. last 24h). Must be idempotent — the ingestion service
        upserts on `(source_system, external_id)`.

        Adapters should raise for total failures (auth broken, source
        unreachable). The ingestion service catches per-source failures
        and continues with other adapters.
        """


_registry: dict[str, type[EvidenceSourceAdapter]] = {}


def register(cls: type[EvidenceSourceAdapter]) -> type[EvidenceSourceAdapter]:
    """Class decorator that adds an adapter to the registry.

    Duplicate registration raises to catch accidental collisions between
    two adapters claiming the same `source_system` key.
    """
    key = getattr(cls, 'source_system', None)
    if not key:
        raise ValueError(f'{cls.__name__} must define a non-empty source_system')
    if key in _registry and _registry[key] is not cls:
        raise ValueError(
            f'source_system {key!r} already registered by {_registry[key].__name__}'
        )
    _registry[key] = cls
    return cls


def get_adapter(source_system: str) -> EvidenceSourceAdapter:
    """Instantiate an adapter by source_system key."""
    try:
        return _registry[source_system]()
    except KeyError:
        raise LookupError(
            f'No adapter registered for source_system={source_system!r}. '
            f'Registered: {sorted(_registry)}'
        )


def iter_registered() -> Iterator[tuple[str, type[EvidenceSourceAdapter]]]:
    """Yield (source_system, adapter_class) pairs, sorted by key for stability."""
    yield from sorted(_registry.items())
