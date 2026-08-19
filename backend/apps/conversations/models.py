"""Normalized conversation, linkage, and outcome models.

These are source-independent and back the Pipeline 1A flow:

    Source adapter (Quo, later Thumbtack/Yelp/Callio)
        ↓
    Conversation + ConversationTurn (this module)
        ↓
    EntityLink to LeadBridge / ServiceFlow
        ↓
    OutcomeSnapshot (rerunnable)
        ↓
    apps.context.pipeline.EvidencePipeline

Design invariants:

- Every top-level row is org-scoped (tenant isolation).
- (org, source, source_conversation_id) is unique — safe re-import.
- Turn uniqueness is (conversation, source_turn_id) — safe re-import of a
  partial conversation, safe reassembly across paginated fetches.
- Raw source payloads are retained in `metadata` fields so re-normalization
  after a schema change never loses information.
- Outcomes are versioned by `captured_at`, not overwritten — a conversation
  that reaches revenue three weeks later still preserves the initial
  "quoted" snapshot.
"""

from django.db import models

from apps.common.models import BaseModel


class Channel(models.TextChoices):
    VOICE = 'voice', 'Voice'
    SMS = 'sms', 'SMS'
    CHAT = 'chat', 'Chat'
    UNKNOWN = 'unknown', 'Unknown'


class IngestionStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    NORMALIZED = 'normalized', 'Normalized'
    LINKED = 'linked', 'Linked'
    OUTCOMES_RESOLVED = 'outcomes_resolved', 'Outcomes resolved'
    EMITTED = 'emitted', 'Emitted to EvidencePipeline'
    FAILED = 'failed', 'Failed'


class Speaker(models.TextChoices):
    CUSTOMER = 'customer', 'Customer'
    AGENT = 'agent', 'Agent'
    SYSTEM = 'system', 'System'
    UNKNOWN = 'unknown', 'Unknown'


class Direction(models.TextChoices):
    INBOUND = 'inbound', 'Inbound'
    OUTBOUND = 'outbound', 'Outbound'
    UNKNOWN = 'unknown', 'Unknown'


class TargetSystem(models.TextChoices):
    LEADBRIDGE = 'leadbridge', 'LeadBridge'
    SERVICEFLOW = 'serviceflow', 'ServiceFlow'


class TargetType(models.TextChoices):
    LEAD = 'lead', 'Lead'
    CUSTOMER = 'customer', 'Customer'
    OPPORTUNITY = 'opportunity', 'Opportunity'
    JOB = 'job', 'Job'
    APPOINTMENT = 'appointment', 'Appointment'


class MatchMethod(models.TextChoices):
    EXTERNAL_ID = 'external_id', 'External ID'
    PHONE_EXACT = 'phone_exact', 'Phone (exact match)'
    EMAIL_EXACT = 'email_exact', 'Email (exact match)'
    MANUAL = 'manual', 'Manual'


class Conversation(BaseModel):
    """A source-independent conversation (voice / SMS / chat)."""

    org = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='conversations',
    )
    # Free-form so new sources (thumbtack, yelp, callio, etc.) don't require
    # a migration. Matches the `source_system` convention used by
    # apps.learning.EvidenceInsight.
    source = models.CharField(max_length=64)
    source_conversation_id = models.CharField(max_length=255)

    channel = models.CharField(
        max_length=16,
        choices=Channel.choices,
        default=Channel.UNKNOWN,
    )

    # Normalized E.164 (via apps.conversations.normalization.phone) or empty
    # when the source didn't provide a usable phone. Stored empty rather than
    # nullable so index lookups don't need IS NULL handling.
    customer_phone = models.CharField(max_length=32, blank=True, default='')
    customer_email = models.EmailField(blank=True, default='')

    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    # Full raw source payload for the conversation-level record (not turns).
    # Retained so re-normalization after a schema change can recover fields
    # we didn't originally know we cared about.
    metadata = models.JSONField(default=dict, blank=True)

    ingestion_status = models.CharField(
        max_length=32,
        choices=IngestionStatus.choices,
        default=IngestionStatus.PENDING,
    )

    # Correlation ID for the import run that created / last updated this row.
    # Lets us trace a Grafana log line back to every row a given import touched.
    import_run_id = models.CharField(max_length=64, blank=True, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['org', 'source', 'source_conversation_id'],
                name='conversations_org_source_extid_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['org', '-started_at']),
            models.Index(fields=['org', 'customer_phone']),
            models.Index(fields=['org', 'source', '-started_at']),
            models.Index(fields=['ingestion_status']),
        ]
        ordering = ['-started_at']

    def __str__(self) -> str:
        return f'{self.source}:{self.source_conversation_id} ({self.channel})'


class ConversationTurn(BaseModel):
    """One utterance / message within a conversation.

    Represents a transcript segment for voice or a single message for SMS/chat.
    Idempotent on (conversation, source_turn_id) — the same source turn is
    never persisted twice even if the parent conversation is re-imported.
    """

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='turns',
    )
    # Stable per-source identifier for this turn. Required — a turn without
    # a stable source ID cannot be safely re-imported. Callers must fabricate
    # deterministic IDs (e.g. `<conv_id>:idx:5`) when the source doesn't
    # supply them, rather than allowing NULLs.
    source_turn_id = models.CharField(max_length=255)

    speaker = models.CharField(
        max_length=16,
        choices=Speaker.choices,
        default=Speaker.UNKNOWN,
    )
    direction = models.CharField(
        max_length=16,
        choices=Direction.choices,
        default=Direction.UNKNOWN,
    )
    text = models.TextField(blank=True, default='')

    occurred_at = models.DateTimeField()

    # Transcript confidence when the source provides it (0.0–1.0). Null for
    # SMS/chat and for voice sources without confidence scores.
    confidence = models.FloatField(null=True, blank=True)

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['conversation', 'source_turn_id'],
                name='conversation_turns_conv_srcid_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['conversation', 'occurred_at']),
        ]
        ordering = ['occurred_at']

    def __str__(self) -> str:
        preview = (self.text or '')[:40]
        return f'{self.speaker}@{self.occurred_at.isoformat()}: {preview}'


class EntityLink(BaseModel):
    """Explicit deterministic linkage between a Conversation and an
    external entity (LeadBridge Lead, ServiceFlow Opportunity, etc).

    Stored as its own table (not as FKs on Conversation) because:
    - One conversation can map to many downstream entities
      (an SF opportunity, a job, and an appointment for a single call).
    - Match method + confidence provenance are per-link, not per-conversation.
    - Re-resolution can add new links without mutating older ones.

    We never silently persist fuzzy matches — every row records the exact
    deterministic rule that produced it.
    """

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='entity_links',
    )
    target_system = models.CharField(
        max_length=32,
        choices=TargetSystem.choices,
    )
    target_type = models.CharField(
        max_length=32,
        choices=TargetType.choices,
    )
    # Free-form since LB uses UUIDs, SF uses UUIDs, future systems may not.
    target_id = models.CharField(max_length=255)

    match_method = models.CharField(
        max_length=32,
        choices=MatchMethod.choices,
    )
    # 0.0–1.0. Deterministic matches record 1.0; manual overrides may set
    # lower to signal operator uncertainty.
    confidence = models.FloatField(default=1.0)
    matched_at = models.DateTimeField(auto_now_add=True)

    # Provenance: what token was matched, which resolver produced this,
    # any diagnostic detail. Kept as JSON so the resolver interface stays
    # narrow while richer provenance can be added over time.
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            # A conversation can only be linked to a given entity once via a
            # given match method. Re-resolving via a stronger method
            # (e.g. external_id after an earlier phone_exact) creates a new
            # row rather than mutating an existing one.
            models.UniqueConstraint(
                fields=[
                    'conversation',
                    'target_system',
                    'target_type',
                    'target_id',
                    'match_method',
                ],
                name='entity_links_dedupe_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['target_system', 'target_type', 'target_id']),
            models.Index(fields=['conversation', 'target_system']),
        ]
        ordering = ['-matched_at']

    def __str__(self) -> str:
        return (
            f'{self.target_system}:{self.target_type}:{self.target_id} '
            f'({self.match_method})'
        )


class OutcomeSnapshot(BaseModel):
    """Normalized outcome fields captured at a point in time.

    Multiple snapshots per conversation are supported: outcomes evolve
    downstream (a quote today becomes a booking tomorrow becomes a
    completion next week). Each resolver run captures a fresh snapshot
    rather than overwriting the last one — this preserves the audit trail
    "what did we know when we shipped that recommendation."

    Uniqueness lives at (conversation, captured_at) but the resolver
    dedupes at write time via `get_or_create(captured_at=<truncated ts>)`
    to avoid persistent-noise duplicates from re-running the resolver
    within the same minute.
    """

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='outcome_snapshots',
    )
    captured_at = models.DateTimeField()

    # LeadBridge normalized fields — nullable because LB may not yet know
    # or the entity may not be linked. Empty is treated as "unknown", not "no".
    lb_status = models.CharField(max_length=64, blank=True, default='')
    lb_engaged = models.BooleanField(null=True, blank=True)
    lb_booked = models.BooleanField(null=True, blank=True)
    lb_lost = models.BooleanField(null=True, blank=True)
    lb_cancelled = models.BooleanField(null=True, blank=True)

    # ServiceFlow normalized fields.
    sf_opportunity_status = models.CharField(max_length=64, blank=True, default='')
    sf_booked = models.BooleanField(null=True, blank=True)
    sf_completed = models.BooleanField(null=True, blank=True)
    sf_cancelled = models.BooleanField(null=True, blank=True)
    # Stored as Decimal-friendly integer cents to avoid float precision loss.
    # Callers convert dollars → cents at write time. Null = revenue unknown.
    sf_revenue_cents = models.BigIntegerField(null=True, blank=True)
    sf_recurring = models.BooleanField(null=True, blank=True)
    sf_job_count = models.PositiveIntegerField(null=True, blank=True)

    # Raw payloads the resolvers used to build the normalized fields.
    # Retained so a future normalization change can rerun over historical
    # snapshots without re-hitting LB/SF.
    source_payload = models.JSONField(default=dict, blank=True)

    # Provenance metadata: which resolver ran, which entity links were
    # consulted, any partial-failure notes.
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['conversation', 'captured_at'],
                name='outcome_snapshots_conv_captured_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['conversation', '-captured_at']),
        ]
        ordering = ['-captured_at']

    def __str__(self) -> str:
        return f'outcome@{self.captured_at.isoformat()} for {self.conversation_id}'


# ---------------------------------------------------------------------------
# Pipeline 1B-1: corpus, semantic extraction, evaluation
# ---------------------------------------------------------------------------


class LearningCorpus(BaseModel):
    """Frozen, versioned subset of Conversations for Pipeline 1B analysis.

    Rerunning any Pipeline 1B stage against `spotless_lb_quo_v1` MUST see
    the same conversations unless a new corpus version is created.
    Membership is stored explicitly in LearningCorpusMember rather than
    re-derived from a query, so a lead added to LB tomorrow can't
    accidentally sneak into an existing corpus.
    """

    org = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='learning_corpora',
    )
    name = models.CharField(max_length=128)
    version = models.CharField(max_length=64)
    # Freeform description of how the corpus was assembled — e.g.
    #   {"source": "lb_anchored", "turn_count_min": 5,
    #    "statuses": ["lost","engaged",...], "notes": "..."}
    selection_criteria = models.JSONField(default=dict, blank=True)
    # Denormalized member count for fast reads.
    member_count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['org', 'name', 'version'],
                name='learning_corpus_org_name_version_unique',
            ),
        ]
        indexes = [models.Index(fields=['org', 'name'])]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.name}@{self.version} (n={self.member_count})'


class LearningCorpusMember(BaseModel):
    """One conversation's membership in one corpus. Preserves the lead-id
    at capture time even if EntityLinks later change."""

    corpus = models.ForeignKey(
        LearningCorpus, on_delete=models.CASCADE, related_name='members',
    )
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE,
        related_name='corpus_memberships',
    )
    # Snapshot of the primary LB link at corpus-freeze time (nullable —
    # unmatched conversations may still be legitimate members later).
    lb_lead_id = models.CharField(max_length=128, blank=True, default='')
    # Snapshot of the outcome status at freeze time. Later outcome
    # snapshots on the Conversation don't affect the corpus's own labels.
    lb_status_at_freeze = models.CharField(max_length=64, blank=True, default='')
    turn_count_at_freeze = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['corpus', 'conversation'],
                name='corpus_member_conv_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['corpus', 'lb_status_at_freeze']),
            models.Index(fields=['conversation']),
        ]
        ordering = ['created_at']


class SemanticExtractionRun(BaseModel):
    """One (corpus × extractor × ontology × prompt × model) invocation.

    Multiple runs against the same corpus coexist so ontology/prompt
    changes can be compared without losing history. Old events NEVER
    get overwritten.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        PARTIAL = 'partial', 'Partial (some records failed)'
        FAILED = 'failed', 'Failed'

    org = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='semantic_extraction_runs',
    )
    corpus = models.ForeignKey(
        LearningCorpus, on_delete=models.CASCADE,
        related_name='extraction_runs',
    )
    extractor_version = models.CharField(max_length=64)
    ontology_version = models.CharField(max_length=64)
    prompt_version = models.CharField(max_length=64)
    model = models.CharField(max_length=64)
    provider = models.CharField(max_length=32, blank=True, default='')

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    records_processed = models.PositiveIntegerField(default=0)
    records_failed = models.PositiveIntegerField(default=0)
    events_created = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    error_summary = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['corpus', 'extractor_version', 'ontology_version',
                        'prompt_version', 'model'],
                name='extraction_run_version_key_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['corpus', 'status']),
            models.Index(fields=['-started_at']),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return (
            f'{self.corpus.name}@{self.corpus.version} '
            f'{self.extractor_version}+{self.ontology_version}+{self.prompt_version} '
            f'({self.model})'
        )


class ConversationSemanticEvent(BaseModel):
    """One extracted semantic event within one conversation.

    Never mutated after write — reruns produce NEW events under a new
    extraction_run. The (conversation, extraction_run, ordinal) tuple
    uniquely identifies an event within a run.
    """

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE,
        related_name='semantic_events',
    )
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE,
        related_name='semantic_events',
    )
    # Optional pointer to the specific LB link this event relates to —
    # allows attributing events to a specific lead when a conversation
    # has multiple links.
    entity_link = models.ForeignKey(
        EntityLink, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='semantic_events',
    )
    extraction_run = models.ForeignKey(
        SemanticExtractionRun, on_delete=models.CASCADE,
        related_name='events',
    )
    # Position within this run's event set for the conversation.
    # Preserves LLM's original ordering.
    ordinal = models.PositiveIntegerField()

    event_type = models.CharField(max_length=64)  # validated against ontology
    actor = models.CharField(max_length=16)       # validated against ontology
    turn_start = models.PositiveIntegerField()
    turn_end = models.PositiveIntegerField()
    occurred_at = models.DateTimeField(null=True, blank=True)
    confidence = models.FloatField()
    attributes = models.JSONField(default=dict, blank=True)
    # Verbatim excerpt from the conversation supporting the event.
    # Truncated at 1000 chars — full evidence is recoverable via
    # (conversation, turn_start..turn_end).
    evidence_text = models.CharField(max_length=1000, blank=True, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['extraction_run', 'conversation', 'ordinal'],
                name='semantic_event_run_conv_ordinal_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['extraction_run', 'event_type']),
            models.Index(fields=['conversation', 'extraction_run']),
            models.Index(fields=['event_type']),
            models.Index(fields=['actor']),
        ]
        ordering = ['extraction_run', 'conversation', 'ordinal']

    def __str__(self) -> str:
        return f'{self.event_type} by {self.actor} t{self.turn_start}-{self.turn_end}'


class SemanticEventEvaluation(BaseModel):
    """Human review of one extracted event. Used to build a semantic
    eval set that survives prompt/model changes."""

    class Judgment(models.TextChoices):
        CORRECT = 'correct', 'Correct'
        PARTIALLY_CORRECT = 'partially_correct', 'Partially correct'
        INCORRECT = 'incorrect', 'Incorrect'
        MISSED_EVENT = 'missed_event', 'Missed event (should exist)'
        WRONG_EVENT_TYPE = 'wrong_event_type', 'Wrong event type'
        WRONG_ACTOR = 'wrong_actor', 'Wrong actor'
        WRONG_SPAN = 'wrong_span', 'Wrong turn span'

    # `event` is nullable so evaluators can log MISSED_EVENT judgments
    # that don't correspond to an existing extracted row.
    event = models.ForeignKey(
        ConversationSemanticEvent, on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='evaluations',
    )
    # Free identifier — email, name, or "auto" for eventual programmatic checks.
    reviewer = models.CharField(max_length=128, blank=True, default='')
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE,
        related_name='semantic_evaluations',
    )
    extraction_run = models.ForeignKey(
        SemanticExtractionRun, on_delete=models.CASCADE,
        related_name='evaluations',
    )
    judgment = models.CharField(max_length=32, choices=Judgment.choices)
    # For MISSED_EVENT: what type SHOULD have been extracted (from ontology).
    expected_event_type = models.CharField(max_length=64, blank=True, default='')
    expected_turn_start = models.PositiveIntegerField(null=True, blank=True)
    expected_turn_end = models.PositiveIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True, default='')

    class Meta:
        indexes = [
            models.Index(fields=['extraction_run', 'judgment']),
            models.Index(fields=['conversation']),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.judgment}: event={self.event_id or "(missed)"}'
