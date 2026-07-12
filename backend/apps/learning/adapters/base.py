"""Base adapter contract + registry.

Adapters register themselves at import time by decorating their class with
`@register`. The ingestion service looks up adapters by `source_system`
key. This keeps the engine unaware of specific sources — adding a new
source (HireFunnel, ProofPix, FixLoop, Google Reviews, ...) means writing
one class and importing it in `adapters/__init__.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Iterable, Iterator

from django.conf import settings

from apps.learning.adapters.dto import Evidence


@dataclass
class ResolvedCredentials:
    """Where the adapter got its URL/token from — DB, settings, or fixtures.

    `source` is 'integration' / 'settings' / 'fixture' so the ingestion
    service can render which config the adapter is actually using.
    """
    url: str
    token: str
    source: str
    label: str  # human-readable summary for logs / dashboard


def resolve_credentials(source_system: str, org=None) -> ResolvedCredentials:
    """Look up (url, token) for a source: SourceIntegration → settings → fixture.

    Kept as a free function (not a method on the ABC) so it can be
    imported without triggering Django-app loading in tools that just
    want the credential-resolution logic.
    """
    if org is not None:
        # Lazy import to avoid circulars during app registry loading.
        from apps.learning.models import SourceIntegration
        integration = (
            SourceIntegration.objects
            .filter(org=org, source_system=source_system, is_active=True)
            .first()
        )
        if integration and integration.url and integration.token:
            return ResolvedCredentials(
                url=integration.url,
                token=integration.token,
                source='integration',
                label=f'SourceIntegration id={integration.id}',
            )

    upper = source_system.upper()
    url = getattr(settings, f'{upper}_LEARNING_URL', '')
    token = getattr(settings, f'{upper}_LEARNING_TOKEN', '')
    if url and token:
        return ResolvedCredentials(
            url=url, token=token, source='settings',
            label=f'env {upper}_LEARNING_URL',
        )

    return ResolvedCredentials(
        url='', token='', source='fixture',
        label='bundled fixture',
    )


class EvidenceSourceAdapter(ABC):
    """Contract every source adapter must implement.

    Subclasses set `source_system` as a class attribute — that string is
    the registry key and becomes `Evidence.source_system` for every record
    the adapter yields.
    """

    source_system: ClassVar[str]

    @abstractmethod
    def fetch_since(self, since: datetime | None, *, org=None) -> Iterable[Evidence]:
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
