"""Source registry.

Sources self-register at import time via `@register`. `iter_registered()`
returns them in registration order — a stable, predictable ordering that
doesn't depend on a filesystem walk. The Context Merger sorts by priority
at merge time; the registry order is only a tiebreaker.

Adding a new Source = drop one file into `apps/context/sources/` and add
an import to `apps/context/sources/__init__.py`. The Engine itself needs
no code changes — that's the point of the Phase 2 refactor.
"""

from __future__ import annotations

from typing import Iterable

from apps.context.engine.base import BaseContextSource


_REGISTRY: dict[str, type[BaseContextSource]] = {}


def register(cls: type[BaseContextSource]) -> type[BaseContextSource]:
    name = cls.name
    if not name:
        raise ValueError(f'Source class {cls.__name__} must set `name`.')
    if name in _REGISTRY:
        raise ValueError(f'Source "{name}" is already registered.')
    _REGISTRY[name] = cls
    return cls


def iter_registered() -> Iterable[tuple[str, type[BaseContextSource]]]:
    return list(_REGISTRY.items())


def get_source(name: str) -> type[BaseContextSource]:
    return _REGISTRY[name]


def unregister(name: str) -> None:
    """Test helper — remove a registration. Not for production use."""
    _REGISTRY.pop(name, None)


def _clear_for_tests() -> None:
    """Test helper — nuke the whole registry."""
    _REGISTRY.clear()
