"""Base abstractions for entity resolvers.

A resolver takes a normalized `Conversation` and returns zero-or-more
`ResolutionResult`s — each result represents a deterministic link to
one downstream entity in an external system (LeadBridge lead,
ServiceFlow opportunity, etc).

Resolvers are:
- Read-only (they NEVER mutate the external system)
- Deterministic (external_id > phone_exact > email_exact, no fuzzy)
- Independently rerunnable (calling `resolve()` twice on the same
  conversation must produce the same results if the external state
  hasn't changed)

Persistence of the resulting `EntityLink` rows happens in the service
layer (Phase 7), not in the resolver.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable

from apps.conversations.models import Conversation


@dataclass(frozen=True)
class ResolutionResult:
    """One deterministic link produced by a resolver.

    The service layer converts these into `EntityLink` rows. Resolvers
    never write to the DB themselves.
    """

    target_system: str    # matches EntityLink.TargetSystem values
    target_type: str      # matches EntityLink.TargetType values
    target_id: str
    match_method: str     # matches EntityLink.MatchMethod values
    confidence: float = 1.0
    metadata: dict = field(default_factory=dict)


class BaseResolver(ABC):
    """Abstract resolver — one instance per external system."""

    #: Slug for the target system this resolver produces links into.
    target_system: str

    @abstractmethod
    def resolve(self, conversation: Conversation) -> Iterable[ResolutionResult]:
        """Return deterministic links for `conversation`.

        Must NOT persist to the DB.
        Must NEVER raise for a "no match" result — return an empty
        iterable instead. Exceptions may only be raised for genuine
        infrastructure failures (network, auth) that the caller should
        log as errors rather than treat as "unmatched."
        """
