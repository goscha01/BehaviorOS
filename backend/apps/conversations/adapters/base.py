"""Base abstractions for conversation adapters.

An adapter yields raw source records — dicts as they came from the source
system. Normalization (source-specific field mapping) happens in
`apps.conversations.normalization.<source>`. Persistence and orchestration
happens in `apps.conversations.services`.

This separation matches the pattern in `apps.learning.adapters` — adapters
never touch the ORM, never talk to LeadBridge/ServiceFlow, never emit
EvidenceEvents.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterator, Mapping, Optional


class ConversationSourceAdapter(ABC):
    """Yields raw source records from a conversation source (Quo, later
    Thumbtack/Yelp/Callio)."""

    #: Short slug identifying this source. Persisted as `Conversation.source`.
    source: str

    @abstractmethod
    def fetch_records(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Iterator[Mapping]:
        """Yield raw source records lazily.

        Implementations MUST:
        - be safe to interrupt (each yielded record represents a complete
          unit of work; the caller commits before requesting the next)
        - honor `since`/`until` as inclusive-start / exclusive-end filters
          when the source system supports server-side date filtering; when
          it doesn't, filter client-side
        - honor `limit` as a hard cap on total records yielded
        - be idempotent — the same call with the same args yields the same
          records in the same order (or documents why not)
        """
