"""Evidence — the boundary type between adapters and the learning engine.

Every adapter yields Evidence instances. The ingestion service is the ONLY
component that translates Evidence → EvidenceInsight. Adapters must never
touch the ORM directly. This keeps adapters trivial to write for new
sources (HireFunnel, ProofPix, FixLoop, Google Reviews, ...) and keeps the
engine unaware of source-specific concerns.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class Evidence:
    """Adapter-produced record of one piece of evidence.

    Attributes:
        source_system: Free-form source key, e.g. "leadbridge", "callio", "google_reviews".
            Must be stable — it becomes half of the idempotency key.
        evidence_type: One of EvidenceInsight.EvidenceType values
            ("conversation", "call", "outcome", "other").
        external_id: Source-system-native ID for this piece of evidence.
            Together with source_system, forms the idempotency key.
        occurred_at: When the real-world event happened at the source.
            Adapters should pass a source-provided timestamp; do not use
            "now" as a fallback.
        source_payload: Raw evidence, structured however the source likes
            (transcript object, call record, outcome payload, review body).
            The analyzer reads this; keep it faithful to the source.
        outcome: Optional structured business outcome. Convention:
            {"status": "booked" | "cancelled" | "won" | ..., ...extra_fields}.
            The ingestion service extracts `status` into
            EvidenceInsight.outcome and stores the full dict in outcome_metadata.
        metadata: Adapter-level metadata about the fetch itself
            (cursor position, source URL, fetch time). Persisted to
            EvidenceInsight.ingest_metadata; not analyzed.
        business_rules_version: Version tag of the source system's Playbook
            / business rules at the time of the event. Lets us later
            compare recommendations before/after rule changes.
    """

    source_system: str
    evidence_type: str
    external_id: str
    occurred_at: datetime | None
    source_payload: Mapping[str, Any]
    outcome: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    business_rules_version: str = ''
