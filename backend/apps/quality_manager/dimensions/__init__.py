"""QM dimension registry.

Each dimension self-registers via `@register`. The engine iterates
`iter_registered()` per run. Registration order is preserved for
deterministic evaluation output.
"""

from apps.quality_manager.dimensions.base import (
    BaseDimension,
    DimensionResult,
    EvidenceRef,
    State,
)


_REGISTRY: list[type[BaseDimension]] = []


def register(cls: type[BaseDimension]) -> type[BaseDimension]:
    if cls not in _REGISTRY:
        _REGISTRY.append(cls)
    return cls


def iter_registered() -> list[type[BaseDimension]]:
    return list(_REGISTRY)


def get_dimension(name: str) -> type[BaseDimension] | None:
    for cls in _REGISTRY:
        if cls.name == name:
            return cls
    return None


# Import concrete dimensions here so they self-register on module load.
# V1: pricing_correctness (2026-08-22), response_timing (2026-08-23),
# customer_question_answered (2026-08-23), required_actions (2026-08-23).
# Registration order matters for engine iteration.
from apps.quality_manager.dimensions import pricing_correctness  # noqa: F401,E402
from apps.quality_manager.dimensions import response_timing  # noqa: F401,E402
from apps.quality_manager.dimensions import customer_question_answered  # noqa: F401,E402
from apps.quality_manager.dimensions import required_actions  # noqa: F401,E402


__all__ = [
    'BaseDimension',
    'DimensionResult',
    'EvidenceRef',
    'State',
    'register',
    'iter_registered',
    'get_dimension',
]
