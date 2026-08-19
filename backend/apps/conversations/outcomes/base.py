"""Outcome fetcher abstractions.

An outcome fetcher takes the set of `EntityLink`s a conversation has
been linked to in one external system and returns the current outcome
state for those entities. Fetchers must be:

- Read-only (never mutates the source system)
- Rerunnable (a fresh call returns the CURRENT state; snapshots record
  point-in-time)
- Robust to missing entities (returns empty rather than raising when an
  entity is gone)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class LeadBridgeOutcome:
    """Normalized LB outcome fields as of the fetch time.

    All fields nullable — None means the LB API didn't have this info,
    NOT that the answer is "no."
    """
    lb_lead_id: str
    status: str = ''
    engaged: Optional[bool] = None
    booked: Optional[bool] = None
    lost: Optional[bool] = None
    cancelled: Optional[bool] = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ServiceFlowOutcome:
    """Normalized SF outcome fields.

    Because SF sees a conversation as multiple entities (customer,
    opportunity, job, appointment), one call may yield fields describing
    the opportunity AND the aggregate rollup across the customer's jobs.
    Callers merge results across entities into a single OutcomeSnapshot.
    """
    sf_entity_type: str      # matches EntityLink.TargetType
    sf_entity_id: str
    opportunity_status: str = ''
    booked: Optional[bool] = None
    completed: Optional[bool] = None
    cancelled: Optional[bool] = None
    revenue_cents: Optional[int] = None
    recurring: Optional[bool] = None
    job_count: Optional[int] = None
    raw: dict = field(default_factory=dict)


class BaseLeadBridgeOutcomeFetcher(ABC):
    @abstractmethod
    def fetch_outcomes(
        self, lead_ids: Iterable[str]
    ) -> list[LeadBridgeOutcome]:
        ...


class BaseServiceFlowOutcomeFetcher(ABC):
    @abstractmethod
    def fetch_outcomes(
        self, entities: list[tuple[str, str]]  # (type, id)
    ) -> list[ServiceFlowOutcome]:
        ...
