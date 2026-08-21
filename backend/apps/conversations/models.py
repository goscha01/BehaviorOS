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


# ---------------------------------------------------------------------------
# Pipeline 1B-2: candidate behavioral pattern discovery
# ---------------------------------------------------------------------------


class PatternDiscoveryRun(BaseModel):
    """One end-to-end pattern-discovery run on (corpus × extraction_run).

    Rerunning with the same (corpus, extraction_run, analyzer_version,
    split_seed) is a no-op via the unique constraint — different seeds
    or analyzer versions yield NEW runs, old candidates preserved.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE,
        related_name='pattern_discovery_runs',
    )
    corpus = models.ForeignKey(
        LearningCorpus, on_delete=models.CASCADE,
        related_name='pattern_discovery_runs',
    )
    extraction_run = models.ForeignKey(
        SemanticExtractionRun, on_delete=models.CASCADE,
        related_name='pattern_discovery_runs',
    )
    analyzer_version = models.CharField(max_length=64)
    split_seed = models.PositiveIntegerField(
        help_text='Random seed for the stratified 80/20 split. Same seed = same split.',
    )
    config = models.JSONField(default=dict, blank=True,
                              help_text='Discovery config snapshot')
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    discovery_conversation_ids = models.JSONField(default=list, blank=True)
    holdout_conversation_ids = models.JSONField(default=list, blank=True)
    positive_class_statuses = models.JSONField(default=list, blank=True)
    negative_class_statuses = models.JSONField(default=list, blank=True)

    n_discovery_positive = models.PositiveIntegerField(default=0)
    n_discovery_negative = models.PositiveIntegerField(default=0)
    n_holdout_positive = models.PositiveIntegerField(default=0)
    n_holdout_negative = models.PositiveIntegerField(default=0)

    candidates_found = models.PositiveIntegerField(default=0)
    candidates_reproduced_on_holdout = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['corpus', 'extraction_run', 'analyzer_version', 'split_seed'],
                name='pattern_run_key_unique',
            ),
        ]
        indexes = [models.Index(fields=['corpus', 'status'])]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'PatternRun {self.corpus.name}@{self.corpus.version} seed={self.split_seed}'


class CandidatePattern(BaseModel):
    """One candidate behavioral pattern. Never mutated — reruns under a
    new (analyzer_version, split_seed) produce a NEW discovery_run with
    fresh candidates."""

    class Kind(models.TextChoices):
        SINGLE_EVENT = 'single_event', 'Single event presence'
        ORDERED_SEQUENCE = 'ordered_sequence', 'Ordered N-gram sequence'

    class HoldoutStatus(models.TextChoices):
        REPRODUCED = 'reproduced', 'Direction reproduced'
        NOT_REPRODUCED = 'not_reproduced', 'Direction did NOT reproduce'
        UNDERPOWERED = 'underpowered', 'Holdout n too small'

    class TemporalClass(models.TextChoices):
        PRE_OUTCOME = 'PRE_OUTCOME', 'Pre-outcome (predictive candidate)'
        OUTCOME_PROXY = 'OUTCOME_PROXY', 'Near-tautological with label'
        POST_OUTCOME = 'POST_OUTCOME', 'After outcome determined'
        UNKNOWN = 'UNKNOWN', 'Ambiguous / mixed timing'
        MIXED = 'MIXED', 'Sequence spans multiple classes'

    discovery_run = models.ForeignKey(
        PatternDiscoveryRun, on_delete=models.CASCADE, related_name='candidates',
    )
    pattern_id = models.CharField(max_length=32,
                                   help_text='Like P0017 within the discovery_run')
    kind = models.CharField(max_length=32, choices=Kind.choices)
    # SINGLE_EVENT: [event_type]. ORDERED_SEQUENCE: [e1, e2, ...].
    sequence = models.JSONField()
    temporal_class = models.CharField(max_length=16, choices=TemporalClass.choices)

    # Discovery — raw presence rates
    discovery_positive_present = models.PositiveIntegerField(default=0)
    discovery_positive_total = models.PositiveIntegerField(default=0)
    discovery_negative_present = models.PositiveIntegerField(default=0)
    discovery_negative_total = models.PositiveIntegerField(default=0)
    discovery_positive_rate = models.FloatField(default=0.0)
    discovery_negative_rate = models.FloatField(default=0.0)
    discovery_effect_size = models.FloatField(default=0.0,
                                               help_text='pos_rate - neg_rate')
    discovery_effect_ci_low = models.FloatField(default=0.0)
    discovery_effect_ci_high = models.FloatField(default=0.0)

    # Length-adjustment: direction on short (below-median turns) vs long
    # halves of the discovery set. 'positive', 'negative', 'null', or
    # empty when the stratum has too few records.
    length_adjusted_direction_short = models.CharField(max_length=16, blank=True, default='')
    length_adjusted_direction_long = models.CharField(max_length=16, blank=True, default='')

    # Holdout
    holdout_positive_present = models.PositiveIntegerField(default=0)
    holdout_positive_total = models.PositiveIntegerField(default=0)
    holdout_negative_present = models.PositiveIntegerField(default=0)
    holdout_negative_total = models.PositiveIntegerField(default=0)
    holdout_positive_rate = models.FloatField(default=0.0)
    holdout_negative_rate = models.FloatField(default=0.0)
    holdout_effect_size = models.FloatField(default=0.0)
    holdout_status = models.CharField(max_length=32, choices=HoldoutStatus.choices,
                                       blank=True, default='')

    # Evidence — up to 50 conversation IDs per side (rest recoverable
    # by re-scanning). Keeps row size sane.
    evidence_positive_ids = models.JSONField(default=list, blank=True)
    evidence_negative_ids = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['discovery_run', 'pattern_id'],
                name='candidate_pattern_id_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['discovery_run', 'temporal_class']),
            models.Index(fields=['discovery_run', '-discovery_effect_size']),
        ]
        ordering = ['discovery_run', 'pattern_id']

    def __str__(self) -> str:
        return f'{self.pattern_id} [{self.kind}] {self.sequence}'


# ---------------------------------------------------------------------------
# Pipeline 1B-3: conditional (customer signal → agent action) analysis
# ---------------------------------------------------------------------------


class ConditionalAnalysisRun(BaseModel):
    """One end-to-end conditional-action analysis run.

    Same 80/20 stratified split model as PatternDiscoveryRun, but the
    unit of analysis is (condition_event, action_event) cells rather
    than event-sequence presence.

    Unique on (corpus, extraction_run, analyzer_version, split_seed) so
    reruns with the same parameters are no-ops; changing the seed or
    analyzer version yields a new run and preserves old results.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE,
        related_name='conditional_analysis_runs',
    )
    corpus = models.ForeignKey(
        LearningCorpus, on_delete=models.CASCADE,
        related_name='conditional_analysis_runs',
    )
    extraction_run = models.ForeignKey(
        SemanticExtractionRun, on_delete=models.CASCADE,
        related_name='conditional_analysis_runs',
    )
    analyzer_version = models.CharField(max_length=64)
    split_seed = models.PositiveIntegerField(
        help_text='Same-seed convention as PatternDiscoveryRun for split reproducibility.',
    )
    config = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    discovery_conversation_ids = models.JSONField(default=list, blank=True)
    holdout_conversation_ids = models.JSONField(default=list, blank=True)
    positive_class_statuses = models.JSONField(default=list, blank=True)
    negative_class_statuses = models.JSONField(default=list, blank=True)

    n_discovery_positive = models.PositiveIntegerField(default=0)
    n_discovery_negative = models.PositiveIntegerField(default=0)
    n_holdout_positive = models.PositiveIntegerField(default=0)
    n_holdout_negative = models.PositiveIntegerField(default=0)

    # LEAD_MISMATCH conversations are excluded from all denominators —
    # they were never sales opportunities. Preserved in the DB (events
    # are not deleted); this counter is the audit trail for what got
    # dropped and why.
    n_excluded_lead_mismatch = models.PositiveIntegerField(default=0)
    n_excluded_status = models.PositiveIntegerField(default=0)

    # Total per-conversation (C, first-response) observations enumerated
    # across each split. Diagnostic — one conversation with three distinct
    # C-types contributes 3 observations.
    n_discovery_observations = models.PositiveIntegerField(default=0)
    n_holdout_observations = models.PositiveIntegerField(default=0)

    patterns_found = models.PositiveIntegerField(default=0)
    patterns_supported = models.PositiveIntegerField(default=0)
    patterns_reproduced_on_holdout = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['corpus', 'extraction_run', 'analyzer_version', 'split_seed'],
                name='conditional_run_key_unique',
            ),
        ]
        indexes = [models.Index(fields=['corpus', 'status'])]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'ConditionalRun {self.corpus.name}@{self.corpus.version} seed={self.split_seed}'


class ConditionalActionPattern(BaseModel):
    """One (condition_event, action_event) comparison cell.

    Primary comparison: rate(positive | C + A) vs rate(positive | C + other AGENT_ACTION).
    Secondary baseline: rate(positive | C + A) vs rate(positive | C + no response).

    A "supported" pattern has enough conversations in BOTH the C+A cell
    and the C+other-A comparator (>= min_cell_support in each). Cells
    where only C+A meets support are marked DIRECTIONAL_ONLY — the rate
    is reportable but the comparison is not.
    """

    class OverallStatus(models.TextChoices):
        SUPPORTED = 'SUPPORTED', 'Enough support in both cells for a comparison'
        DIRECTIONAL_ONLY = 'DIRECTIONAL_ONLY', 'C+A cell supported but comparator too small'
        UNDERPOWERED = 'UNDERPOWERED', 'C+A cell below min_cell_support'

    class HoldoutStatus(models.TextChoices):
        REPRODUCED = 'HOLDOUT_REPRODUCED', 'Primary direction reproduced on holdout'
        FAILED = 'HOLDOUT_FAILED', 'Primary direction did NOT reproduce'
        UNDERPOWERED = 'UNDERPOWERED', 'Holdout cell too small'

    analysis_run = models.ForeignKey(
        ConditionalAnalysisRun, on_delete=models.CASCADE,
        related_name='patterns',
    )
    pattern_id = models.CharField(
        max_length=32,
        help_text='Like CA0017 within the analysis_run',
    )
    condition_event = models.CharField(
        max_length=64,
        help_text='CUSTOMER_SIGNAL ontology event type',
    )
    action_event = models.CharField(
        max_length=64,
        help_text='AGENT_ACTION ontology event type',
    )

    # Discovery cell counts. CA = condition + this action. CO = condition
    # + other AGENT_ACTION (primary comparator). CN = condition + no
    # eligible response in window (secondary baseline).
    d_ca_positive = models.PositiveIntegerField(default=0)
    d_ca_negative = models.PositiveIntegerField(default=0)
    d_co_positive = models.PositiveIntegerField(default=0)
    d_co_negative = models.PositiveIntegerField(default=0)
    d_cn_positive = models.PositiveIntegerField(default=0)
    d_cn_negative = models.PositiveIntegerField(default=0)

    # Discovery rates (pos / (pos + neg) per cell) — 0 if cell empty.
    d_ca_rate = models.FloatField(default=0.0)
    d_co_rate = models.FloatField(default=0.0)
    d_cn_rate = models.FloatField(default=0.0)

    # Effect sizes on discovery (rate_a - rate_b).
    d_primary_effect = models.FloatField(
        default=0.0, help_text='rate(CA) - rate(CO) — the business question')
    d_primary_ci_low = models.FloatField(default=0.0)
    d_primary_ci_high = models.FloatField(default=0.0)
    d_secondary_effect = models.FloatField(
        default=0.0, help_text='rate(CA) - rate(CN) — reported, not ranked on')
    d_secondary_ci_low = models.FloatField(default=0.0)
    d_secondary_ci_high = models.FloatField(default=0.0)

    # Length-adjusted primary direction — same convention as 1B-2.
    length_adjusted_direction_short = models.CharField(
        max_length=16, blank=True, default='')
    length_adjusted_direction_long = models.CharField(
        max_length=16, blank=True, default='')

    # Holdout cell counts + effects.
    h_ca_positive = models.PositiveIntegerField(default=0)
    h_ca_negative = models.PositiveIntegerField(default=0)
    h_co_positive = models.PositiveIntegerField(default=0)
    h_co_negative = models.PositiveIntegerField(default=0)
    h_cn_positive = models.PositiveIntegerField(default=0)
    h_cn_negative = models.PositiveIntegerField(default=0)
    h_primary_effect = models.FloatField(default=0.0)
    h_secondary_effect = models.FloatField(default=0.0)

    overall_status = models.CharField(
        max_length=32, choices=OverallStatus.choices,
        default=OverallStatus.UNDERPOWERED)
    holdout_status = models.CharField(
        max_length=32, choices=HoldoutStatus.choices,
        default=HoldoutStatus.UNDERPOWERED)

    # Up to 50 conversation IDs per side of the C+A cell for evidence.
    evidence_positive_ids = models.JSONField(default=list, blank=True)
    evidence_negative_ids = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['analysis_run', 'pattern_id'],
                name='conditional_pattern_id_unique',
            ),
            models.UniqueConstraint(
                fields=['analysis_run', 'condition_event', 'action_event'],
                name='conditional_pattern_ca_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['analysis_run', 'condition_event']),
            models.Index(fields=['analysis_run', 'overall_status']),
            models.Index(fields=['analysis_run', '-d_primary_effect']),
        ]
        ordering = ['analysis_run', 'pattern_id']

    def __str__(self) -> str:
        return (f'{self.pattern_id} {self.condition_event} → '
                f'{self.action_event} ({self.overall_status})')


# ---------------------------------------------------------------------------
# Pipeline 1B-4: current-configuration benchmark
# ---------------------------------------------------------------------------
#
# A TenantConfigSnapshot is the immutable capture of a source system's
# behavior-affecting configuration at a point in time. From it we derive
# BehavioralPolicy rows (normalized customer_signal → prescribed_action
# rules) and PolicyAlignmentAssessment rows that compare each policy to
# the observed ConditionalActionPattern evidence from Pipeline 1B-3.
#
# Immutability convention: snapshots are only inserted, never updated.
# The DB doesn't enforce this — the fetch command checks by content-hash
# before writing. If a fetch produces the same hash as the latest
# snapshot, it's a no-op. Preserves audit trail (each write is a new row).


class TenantConfigSnapshot(BaseModel):
    """One immutable capture of a tenant's behavior-affecting config from
    an external source system (LeadBridge today, Callio future).

    Same (org, source_system, tenant_external_id, service_group,
    raw_config_sha256) collides on re-fetch when nothing changed — the
    fetcher detects and skips.
    """

    class SourceSystem(models.TextChoices):
        LEADBRIDGE = 'leadbridge', 'LeadBridge'
        CALLIO = 'callio', 'Callio'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE,
        related_name='tenant_config_snapshots',
    )
    source_system = models.CharField(max_length=32, choices=SourceSystem.choices)
    # LB uses `userId` (UUID). Callio would use its own tenant key.
    tenant_external_id = models.CharField(max_length=128)
    # 'house_cleaning' for now. Empty when config is service-agnostic.
    service_group = models.CharField(max_length=64, blank=True, default='')
    # Whatever version of the source system contract we captured.
    contract_version = models.CharField(max_length=32)
    # Full JSON body from the source's config endpoint.
    raw_config = models.JSONField()
    # Content hash of raw_config for dedup + drift detection.
    raw_config_sha256 = models.CharField(max_length=64)
    # Diagnostics counters populated by the fetcher.
    fetched_from_url = models.CharField(max_length=512, blank=True, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['org', 'source_system', 'tenant_external_id',
                        'service_group', 'raw_config_sha256'],
                name='tenant_config_snapshot_dedup_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['org', 'source_system', 'tenant_external_id',
                                  '-created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return (f'{self.source_system}:{self.tenant_external_id} '
                f'sg={self.service_group or "-"} '
                f'sha={self.raw_config_sha256[:12]}')


class BehavioralPolicy(BaseModel):
    """One normalized behavioral rule derived from a TenantConfigSnapshot.

    Represents a `condition → prescribed action sequence` mapping.
    Multiple policies can share the same condition — e.g. the global AI
    prompt says one thing and a service_profile.ai_instructions_json
    says another. The analyzer inspects all of them.

    Not every part of a config becomes a policy. Pricing tables, FAQ
    text, qualification schema — those stay in the snapshot as
    contextual inputs. Only rules that read as "when C happens, do A"
    are extracted here.
    """

    class Channel(models.TextChoices):
        TEXT = 'text', 'Text (LB SMS/chat replies)'
        VOICE = 'voice', 'Voice (Callio)'
        POLICY = 'policy', 'Channel-agnostic policy'

    snapshot = models.ForeignKey(
        TenantConfigSnapshot, on_delete=models.CASCADE,
        related_name='policies',
    )
    # Ontology CUSTOMER_SIGNAL event type. Validated at write time by
    # the normalizer against ontology.EVENT_TYPES; string here so schema
    # doesn't need a migration when ontology bumps.
    condition_event = models.CharField(max_length=64)
    # Ordered list of ontology AGENT_ACTION event types. Empty list
    # means the policy is `condition → intentionally do nothing`
    # (rare but real — e.g. "don't respond to obvious spam").
    prescribed_action_events = models.JSONField(default=list)
    channel = models.CharField(max_length=16, choices=Channel.choices,
                                default=Channel.TEXT)
    # Verbatim text of the rule as it appears in the source config.
    source_rule_text = models.TextField()
    # Location of the rule inside the raw_config JSON, e.g.
    #   {"config_path": "user.global_ai_prompt", "span": [420, 610]}
    #   {"config_path": "service_profiles[0].ai_instructions_json"}
    # Kept as JSON so consumers can jump straight to the source.
    source_pointer = models.JSONField(default=dict, blank=True)
    # 0.0–1.0. Normalizer's confidence that this rule was correctly
    # extracted from the raw config.
    extraction_confidence = models.FloatField(default=1.0)
    # Ordinal within the snapshot for stable ordering.
    ordinal = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=['snapshot', 'condition_event']),
            models.Index(fields=['snapshot', 'channel']),
        ]
        ordering = ['snapshot', 'ordinal']

    def __str__(self) -> str:
        actions = ' → '.join(self.prescribed_action_events) or '(no action)'
        return f'{self.condition_event} → {actions} [{self.channel}]'


class PolicyAlignmentAssessment(BaseModel):
    """One deterministic classification comparing a BehavioralPolicy
    against a ConditionalActionPattern from the linked analysis run.

    Classification (per 1B-4 spec):
    - CONFIG_SUPPORTED — at least one prescribed action has SUPPORTED
      evidence with positive primary effect
    - CONFIG_QUESTIONABLE — prescribed action has SUPPORTED evidence
      with negative primary effect, AND an alternative action has
      SUPPORTED positive evidence for the same condition
    - EXECUTION_GAP — prescribed action rarely observed (< threshold)
      relative to other observed actions for the same condition
    - INSUFFICIENT_EVIDENCE — cells too small to say anything reliable

    The rationale string records which thresholds fired so the
    classification is auditable. The narrative field is optional
    human-readable text (LLM-generated) that explains the result —
    NEVER drives the classification itself.
    """

    class AlignmentStatus(models.TextChoices):
        CONFIG_SUPPORTED = 'CONFIG_SUPPORTED', 'Prescribed behavior supported by evidence'
        CONFIG_QUESTIONABLE = 'CONFIG_QUESTIONABLE', 'Prescribed weaker than alternatives'
        EXECUTION_GAP = 'EXECUTION_GAP', 'Config prescribes X, observations do Y'
        INSUFFICIENT_EVIDENCE = 'INSUFFICIENT_EVIDENCE', 'Cells too small to compare'

    snapshot = models.ForeignKey(
        TenantConfigSnapshot, on_delete=models.CASCADE,
        related_name='alignment_assessments',
    )
    analysis_run = models.ForeignKey(
        ConditionalAnalysisRun, on_delete=models.CASCADE,
        related_name='alignment_assessments',
    )
    policy = models.ForeignKey(
        BehavioralPolicy, on_delete=models.CASCADE,
        related_name='assessments',
    )
    # The specific ConditionalActionPattern the policy was matched
    # against. Nullable because "no observations of any prescribed
    # action" is a legitimate outcome (→ INSUFFICIENT_EVIDENCE or
    # EXECUTION_GAP depending on what WAS observed instead).
    primary_pattern = models.ForeignKey(
        ConditionalActionPattern, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    alignment_status = models.CharField(
        max_length=32, choices=AlignmentStatus.choices,
    )
    # Which thresholds/rules produced this classification. Human-
    # auditable string, e.g.:
    #   "SUPPORTED pattern (C, PRICE_GIVEN): d_primary_effect=+0.14"
    #   "prescribed rate=0.18 (< 0.20); max alt rate=0.54 (> 0.40)"
    deterministic_rationale = models.TextField()
    # Optional LLM-generated narrative. Never used for classification.
    llm_narrative = models.TextField(blank=True, default='')
    # Conversation IDs pulled from the primary pattern's evidence.
    evidence_conversation_ids = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['snapshot', 'analysis_run', 'policy'],
                name='alignment_assessment_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['snapshot', 'alignment_status']),
            models.Index(fields=['analysis_run', 'alignment_status']),
        ]
        ordering = ['snapshot', 'policy']

    def __str__(self) -> str:
        return f'{self.alignment_status} [{self.policy}]'


# ---------------------------------------------------------------------------
# Pipeline 1B-6: CustomerState v1 — inferred state primitive
# ---------------------------------------------------------------------------
#
# A CustomerState is BehaviorOS's normalized abstraction of where a
# customer is in the sales journey. Inferred deterministically from
# semantic events; multiple event paths can enter the same state
# (that's the whole point of the abstraction).
#
# InferenceRun tracks the (extraction × inference-version) key so
# re-running with new rules under the same extraction creates a NEW
# inference set — old rows preserved.


class CustomerStateInferenceRun(BaseModel):
    """One end-to-end state-inference pass over a corpus × extraction."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE,
        related_name='state_inference_runs',
    )
    corpus = models.ForeignKey(
        LearningCorpus, on_delete=models.CASCADE,
        related_name='state_inference_runs',
    )
    extraction_run = models.ForeignKey(
        SemanticExtractionRun, on_delete=models.CASCADE,
        related_name='state_inference_runs',
    )
    inference_version = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    n_conversations_inferred = models.PositiveIntegerField(default=0)
    n_transitions_emitted = models.PositiveIntegerField(default=0)
    config_snapshot_hash = models.CharField(max_length=64, blank=True, default='',
                                             help_text='Rules-config hash for '
                                                        'audit trail')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['corpus', 'extraction_run', 'inference_version'],
                name='state_inference_run_unique',
            ),
        ]
        indexes = [models.Index(fields=['corpus', 'status'])]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return (f'StateInferenceRun {self.corpus.name}@{self.corpus.version} '
                f'{self.inference_version}')


class InferredCustomerState(BaseModel):
    """One state transition in a conversation's inferred state history.

    Never mutated. A conversation with N state changes has N rows here,
    ordered by `ordinal` starting at 0 (the initial move from UNKNOWN).
    Transitions preserve the previous_state so the full history is
    walkable without joins.
    """

    inference_run = models.ForeignKey(
        CustomerStateInferenceRun, on_delete=models.CASCADE,
        related_name='states',
    )
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE,
        related_name='inferred_states',
    )
    ordinal = models.PositiveIntegerField(
        help_text='0-based position in this conversation state history',
    )
    state = models.CharField(max_length=32)
    previous_state = models.CharField(max_length=32)
    trigger_event_types = models.JSONField(
        default=list,
        help_text='Ontology event types that produced this transition',
    )
    trigger_event_ordinals = models.JSONField(
        default=list,
        help_text='Semantic-event ordinals (provenance to '
                   'ConversationSemanticEvent.ordinal)',
    )
    effective_turn = models.PositiveIntegerField(
        help_text='DB parent turn_start of the latest triggering event',
    )
    reason = models.TextField(blank=True, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['inference_run', 'conversation', 'ordinal'],
                name='inferred_state_run_conv_ordinal_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['inference_run', 'state']),
            models.Index(fields=['conversation', 'inference_run']),
        ]
        ordering = ['inference_run', 'conversation', 'ordinal']

    def __str__(self) -> str:
        return (f'{self.previous_state} → {self.state} '
                f'@turn{self.effective_turn}')


# ---------------------------------------------------------------------------
# Pipeline 1C: evidence-grounded recommendation synthesis
# ---------------------------------------------------------------------------
#
# A RecommendationRun is one synthesis pass over a (state-inference
# × config-snapshot × conditional-analysis) tuple. Deterministic
# eligibility rules decide which recommendations to emit and which
# class each belongs to; the LLM only drafts prose. Every recommendation
# preserves provenance to the exact evidence sources it stands on.
#
# Not a delivery mechanism — recommendations are stored + reported;
# whether Spotless (or any tenant) acts on them is a separate decision.


class RecommendationRun(BaseModel):
    """One end-to-end synthesis of BehaviorRecommendations from an
    aligned set of evidence sources."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE,
        related_name='recommendation_runs',
    )
    corpus = models.ForeignKey(
        LearningCorpus, on_delete=models.CASCADE,
        related_name='recommendation_runs',
    )
    state_inference_run = models.ForeignKey(
        CustomerStateInferenceRun, on_delete=models.CASCADE,
        related_name='recommendation_runs',
    )
    config_snapshot = models.ForeignKey(
        TenantConfigSnapshot, on_delete=models.CASCADE,
        related_name='recommendation_runs',
    )
    conditional_analysis_run = models.ForeignKey(
        ConditionalAnalysisRun, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='recommendation_runs',
        help_text='Optional — used for outcome-statistic provenance if present',
    )
    synthesizer_version = models.CharField(max_length=64)
    llm_model = models.CharField(max_length=64, blank=True, default='')
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    recommendations_generated = models.PositiveIntegerField(default=0)
    llm_input_tokens = models.PositiveIntegerField(default=0)
    llm_output_tokens = models.PositiveIntegerField(default=0)
    llm_cost_usd = models.DecimalField(max_digits=10, decimal_places=4,
                                        default=0)
    error_summary = models.TextField(blank=True, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['corpus', 'state_inference_run', 'config_snapshot',
                        'synthesizer_version'],
                name='recommendation_run_unique',
            ),
        ]
        indexes = [models.Index(fields=['corpus', 'status'])]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return (f'RecommendationRun {self.corpus.name}@{self.corpus.version} '
                f'{self.synthesizer_version}')


class BehaviorRecommendation(BaseModel):
    """One deterministic-class-gated, LLM-drafted recommendation from a
    RecommendationRun. Every field is either set by the eligibility
    engine (deterministic) or drafted by the LLM (prose only). The
    class + confidence + evidence chain are NEVER LLM-decided."""

    class RecClass(models.TextChoices):
        STATE_COVERAGE_GAP = 'STATE_COVERAGE_GAP', 'Validated state has uncovered contributing signals'
        STATE_PARTIAL_COVERAGE = 'STATE_PARTIAL_COVERAGE', 'State recognized but some paths uncovered'
        CONFIG_ALIGNMENT = 'CONFIG_ALIGNMENT', 'Configuration addresses validated state/signal'
        OBSERVED_STATE_INSIGHT = 'OBSERVED_STATE_INSIGHT', 'Useful state/transition insight; no config change justified'
        INSUFFICIENT_EVIDENCE = 'INSUFFICIENT_EVIDENCE', 'Interesting issue but MUST NOT become an action recommendation'

    class Confidence(models.TextChoices):
        HIGH = 'HIGH', 'High — reproduced on holdout, material lift, low ambiguity'
        MEDIUM = 'MEDIUM', 'Medium — reproduced but small n or noisy'
        LOW = 'LOW', 'Low — directional only'
        INSUFFICIENT = 'INSUFFICIENT', 'Not enough evidence for confidence'

    class ProposedActionScope(models.TextChoices):
        CONFIG_ADDITION = 'config_addition', 'Add new policy/rule to LB config'
        CONFIG_REVIEW = 'config_review', 'Human review of existing policy'
        MONITORING_ONLY = 'monitoring_only', 'Watch this state — no config change yet'
        NO_ACTION_RECOMMENDED = 'no_action_recommended', 'Explicitly no action'

    run = models.ForeignKey(
        RecommendationRun, on_delete=models.CASCADE,
        related_name='recommendations',
    )
    recommendation_id = models.CharField(
        max_length=32,
        help_text='Human-readable id like R0001 within the run',
    )
    rec_class = models.CharField(max_length=32, choices=RecClass.choices)
    confidence = models.CharField(max_length=16, choices=Confidence.choices)

    # Deterministic — set by the eligibility engine
    subject_state = models.CharField(
        max_length=32, blank=True, default='',
        help_text='CustomerState this rec is about, if any',
    )
    subject_signals = models.JSONField(
        default=list, blank=True,
        help_text='Ontology signal event_types this rec references',
    )
    linked_policy_ids = models.JSONField(
        default=list, blank=True,
        help_text='BehavioralPolicy IDs (from config snapshot) related to this rec',
    )
    linked_transition = models.JSONField(
        default=dict, blank=True,
        help_text='{"previous_state": ..., "state": ...} when this rec is about a state transition',
    )

    # Structured evidence chain — deterministic, machine-readable
    evidence = models.JSONField(
        default=dict, blank=True,
        help_text=(
            'Structured evidence chain. Example keys: state_lift, '
            'state_holdout_status, state_n, covered_signals, '
            'uncovered_signals, corpus_baseline_positive_rate.'
        ),
    )
    supporting_conversation_ids = models.JSONField(
        default=list, blank=True,
        help_text='Up to 20 evidence conversation IDs',
    )

    # LLM-drafted prose (never decides class or eligibility)
    observation = models.TextField(
        blank=True, default='',
        help_text='One-sentence factual observation, LLM-drafted',
    )
    interpretation = models.TextField(
        blank=True, default='',
        help_text='One-sentence interpretation of what the observation means, LLM-drafted',
    )
    proposed_action_scope = models.CharField(
        max_length=32, choices=ProposedActionScope.choices,
        default=ProposedActionScope.NO_ACTION_RECOMMENDED,
    )
    proposed_action = models.TextField(
        blank=True, default='',
        help_text='Optional proposed action, LLM-drafted; blank when '
                   'proposed_action_scope=NO_ACTION_RECOMMENDED',
    )
    limitations = models.TextField(
        blank=True, default='',
        help_text='REQUIRED: what we do not know; caveats on the claim',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'recommendation_id'],
                name='recommendation_id_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['run', 'rec_class']),
            models.Index(fields=['run', 'confidence']),
        ]
        ordering = ['run', 'recommendation_id']

    def __str__(self) -> str:
        return (f'{self.recommendation_id} [{self.rec_class}] '
                f'{self.subject_state or self.subject_signals}')


class RecommendationLifecycleState(BaseModel):
    """Mutable lifecycle state for a BehaviorRecommendation.

    Kept SEPARATE from the immutable BehaviorRecommendation itself so
    the analytical record never mutates — recommendations are frozen
    at synthesis time; user reactions to them are recorded here.

    One row per recommendation (created lazily on first state change
    or on first read via get_or_create). `history` is an append-only
    log of state transitions for later feedback learning.
    """

    class State(models.TextChoices):
        NEW = 'new', 'New — never viewed'
        VIEWED = 'viewed', 'Viewed — surfaced to user'
        ACCEPTED = 'accepted', 'Accepted — user endorses (does NOT apply changes in v1)'
        DISMISSED = 'dismissed', 'Dismissed — user rejected'

    recommendation = models.OneToOneField(
        BehaviorRecommendation, on_delete=models.CASCADE,
        related_name='lifecycle',
    )
    state = models.CharField(max_length=16, choices=State.choices,
                              default=State.NEW)
    dismissal_reason = models.CharField(
        max_length=64, blank=True, default='',
        help_text='Optional short tag like "not_applicable", '
                   '"already_doing_this", "insufficient_evidence"',
    )
    dismissal_note = models.TextField(
        blank=True, default='',
        help_text='Optional free-text explanation',
    )
    state_changed_at = models.DateTimeField(auto_now=True)
    # Who changed it — for LB integration this is typically the LB
    # user's email. Free string so we don't couple to a specific
    # auth model.
    state_changed_by = models.CharField(max_length=255, blank=True, default='')
    # Append-only log of state transitions:
    #   [{"from": "new", "to": "viewed", "at": "...", "by": "..."}]
    history = models.JSONField(default=list, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['recommendation', 'state']),
        ]
        ordering = ['-state_changed_at']

    def __str__(self) -> str:
        return f'{self.recommendation.recommendation_id}: {self.state}'

    def transition_to(self, new_state: str, *,
                       actor: str = '', reason: str = '',
                       note: str = '') -> None:
        """Record a state transition + append to history. Call `.save()`
        afterwards. Idempotent: transitioning to the current state is a
        no-op except for updating the actor/timestamp."""
        from django.utils import timezone
        prev = self.state
        self.state = new_state
        if reason:
            self.dismissal_reason = reason
        if note:
            self.dismissal_note = note
        if actor:
            self.state_changed_by = actor
        entry = {
            'from': prev, 'to': new_state,
            'at': timezone.now().isoformat(),
        }
        if actor:
            entry['by'] = actor
        if reason:
            entry['reason'] = reason
        self.history.append(entry)


class RecommendationProposal(BaseModel):
    """Structured intent that BehaviorOS produces from an ACCEPTED
    recommendation. The proposal describes WHAT should change (the
    condition + prescribed behavior + evidence) without knowing HOW
    the target system will implement it.

    LeadBridge owns the config; BehaviorOS never mutates it. The
    consumer (LB) takes this proposal, computes an actual config diff
    against its own current state, and requires a second explicit
    Apply authorization from the operator.

    v1 supports exactly ONE change_type: `add_behavior_rule` for
    STATE_COVERAGE_GAP / STATE_PARTIAL_COVERAGE recommendations
    (specifically the R0002-shape "uncovered CUSTOMER_SIGNAL under a
    validated state"). Other rec classes intentionally cannot become
    proposals — proving one path end-to-end before generalizing.

    Drift detection lives on the config_snapshot_hash: the LB
    consumer compares this against its own current-config hash at
    Apply time. Mismatch → STALE, do not apply.
    """

    class TargetSystem(models.TextChoices):
        LEADBRIDGE = 'leadbridge', 'LeadBridge'

    class ChangeType(models.TextChoices):
        ADD_BEHAVIOR_RULE = 'add_behavior_rule', 'Add a behavior rule for a customer signal'

    class Status(models.TextChoices):
        PROPOSED = 'proposed', 'Proposed — ready for consumer preview/apply'
        APPLIED = 'applied', 'Applied — consumer reported successful write'
        STALE = 'stale', 'Stale — consumer reported config drift; regenerate'
        FAILED = 'failed', 'Failed — consumer reported an apply error'

    recommendation = models.OneToOneField(
        BehaviorRecommendation, on_delete=models.CASCADE,
        related_name='proposal',
    )
    target_system = models.CharField(
        max_length=32, choices=TargetSystem.choices,
        default=TargetSystem.LEADBRIDGE,
    )
    change_type = models.CharField(
        max_length=32, choices=ChangeType.choices,
    )
    # Ontology CUSTOMER_SIGNAL that this rule addresses.
    condition = models.CharField(max_length=64)
    # Scope inside the tenant — e.g. 'house_cleaning' service group.
    scope = models.CharField(max_length=64, blank=True, default='')

    # LLM-drafted, one-sentence description the operator will see in
    # the "Preview change" summary.
    proposed_behavior_summary = models.TextField()
    # LLM-drafted, paragraph-length rule text the consumer can use as
    # the raw config content it writes. Consumer decides where to put
    # it inside its own config structure.
    proposed_behavior_detail = models.TextField()

    # Provenance for drift detection + audit trail
    config_snapshot = models.ForeignKey(
        TenantConfigSnapshot, on_delete=models.CASCADE,
        related_name='proposals',
        help_text='Snapshot BehaviorOS analyzed when generating the proposal',
    )
    config_snapshot_hash = models.CharField(
        max_length=64,
        help_text='Copy of snapshot.raw_config_sha256 — consumer compares to '
                   'its own current-config hash at apply time',
    )

    # Lifecycle — updated by consumer via /proposal/status endpoint
    status = models.CharField(
        max_length=16, choices=Status.choices,
        default=Status.PROPOSED,
    )
    consumer_applied_at = models.DateTimeField(null=True, blank=True)
    consumer_error = models.TextField(blank=True, default='')

    # Synthesis metadata
    generator_version = models.CharField(max_length=64)
    llm_model = models.CharField(max_length=64, blank=True, default='')
    llm_input_tokens = models.PositiveIntegerField(default=0)
    llm_output_tokens = models.PositiveIntegerField(default=0)
    llm_cost_usd = models.DecimalField(
        max_digits=10, decimal_places=4, default=0,
    )

    class Meta:
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['config_snapshot', 'status']),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return (f'Proposal({self.recommendation.recommendation_id} → '
                f'{self.change_type}/{self.condition})')


class RecommendationOutcomeMeasurement(BaseModel):
    """One BehaviorOS-side measurement of an APPLIED recommendation.

    Freezes the experimental contract at creation:
      - MeasurementSpec (frozen snapshot of spec_key + version + fields)
      - baseline cohort (conversation IDs from the pre-application
        window that satisfied cohort_entry — never recomputed)
      - baseline outcome rate (deterministic scoring of the frozen ids)
      - config-provenance treatment hashes (pre + post + managed)

    Post-application observations accumulate. Verdict is deterministic
    from the frozen contract + accumulated counters — re-running the
    evaluator over the same row must produce identical status.

    v1 is 1:1 with a LeadBridge `RecommendationApplication.id`. Later
    consumers (Callio, etc.) would use a different external_application_id
    but share this model.

    STATUS SEMANTICS:
      BASELINE_FROZEN — measurement created; awaiting post-app observations
      COLLECTING      — accumulating post-observations
      READY           — min-observation thresholds met; terminal
                        evaluation can legitimately be attempted
                        (READY DOES NOT MEAN STATISTICALLY SIGNIFICANT)
      IMPROVED        — terminal: verdict passed all three gates positive
      NO_MATERIAL_CHANGE — terminal: effect below min_effect_size_pp gate
      WORSE           — terminal: verdict passed all three gates negative
      INCONCLUSIVE    — terminal: deadline reached without conclusive
                        verdict, or coverage/sample floors never met
    """

    class Status(models.TextChoices):
        BASELINE_FROZEN = 'baseline_frozen', 'Baseline frozen — awaiting post-observations'
        COLLECTING = 'collecting', 'Collecting post-treatment observations'
        READY = 'ready', 'Min observation thresholds met — terminal eval can be attempted'
        IMPROVED = 'improved', 'Terminal — improved'
        NO_MATERIAL_CHANGE = 'no_material_change', 'Terminal — no material change'
        WORSE = 'worse', 'Terminal — worse'
        INCONCLUSIVE = 'inconclusive', 'Terminal — inconclusive'

    TERMINAL_STATUSES = frozenset({
        Status.IMPROVED, Status.NO_MATERIAL_CHANGE,
        Status.WORSE, Status.INCONCLUSIVE,
    })

    # -- identity --
    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE,
        related_name='recommendation_outcome_measurements',
    )
    recommendation = models.ForeignKey(
        BehaviorRecommendation, on_delete=models.CASCADE,
        related_name='outcome_measurements',
    )
    # LB's `RecommendationApplication.id` (uuid string). Unique per
    # BehaviorOS row — one measurement per apply. BehaviorOS does not
    # own this table; the id is a reference, not a FK.
    lb_recommendation_application_id = models.CharField(max_length=64)
    # Tenant scoping — copied from the source system's tenant_external_id
    # (LB `userId`) so cohort queries don't need to join up through
    # recommendation.run.config_snapshot every time.
    tenant_external_id = models.CharField(max_length=128)

    # -- frozen contract --
    measurement_spec_key = models.CharField(max_length=64)
    measurement_spec_version = models.CharField(max_length=32)
    # Full FrozenMeasurementSpec.to_dict() — the persistence contract
    # from measurement/specs.py. Historical measurements always score
    # against this snapshot, never a live code-registry lookup.
    frozen_spec_json = models.JSONField()

    # Applied context — mirrored from the application so the row is
    # self-contained.
    applied_at = models.DateTimeField()
    subject_state = models.CharField(max_length=32, blank=True, default='')
    subject_signals = models.JSONField(default=list, blank=True)
    target_signal = models.CharField(
        max_length=64,
        help_text='The specific signal from subject_signals that '
                   'cohort_entry was instantiated with',
    )

    # -- config provenance (treatment side) --
    # Reported by LB when the measurement is created.
    pre_effective_config_hash = models.CharField(
        max_length=64, blank=True, default='',
        help_text='LB config hash BEFORE the apply was written',
    )
    treatment_effective_config_hash = models.CharField(
        max_length=64,
        help_text='LB config hash AFTER the apply was written; '
                   'defines the "clean" post cohort membership',
    )
    treatment_managed_hash = models.CharField(
        max_length=64,
        help_text='LB behavior_os_managed subtree hash after apply; '
                   'used to distinguish treatment-changed vs '
                   'environment-contamination on post conversations',
    )
    effective_config_schema_version = models.CharField(
        max_length=32,
        help_text='Producer schema version, e.g. lb-effective-config-v1; '
                   'evaluator refuses to cross-compare mismatched versions',
    )

    # -- baseline (FROZEN — never recomputed) --
    pre_cohort_conversation_ids = models.JSONField(
        default=list, blank=True,
        help_text='Immutable list of Conversation ids that matched '
                   'cohort_entry within the baseline_window_days lookback '
                   'from applied_at',
    )
    pre_cohort_frozen_at = models.DateTimeField(null=True, blank=True)
    # ROM v1 attribution semantic = terminal_known_after_maturity_v1.
    # A conversation MATURES `attribution_window_days` after
    # `started_at`. Only matured conversations are score-eligible.
    # Of the matured subset, "resolved" = has a known terminal;
    # "unresolved" = matured but no terminal known.
    pre_matured_n = models.PositiveIntegerField(
        default=0,
        help_text='Baseline cohort members that have finished maturing '
                   '(started_at + attribution_window_days <= '
                   'pre_cohort_frozen_at)',
    )
    pre_n = models.PositiveIntegerField(
        default=0,
        help_text='Baseline arm size — resolved outcomes only '
                   '(positive + negative, unresolved excluded)',
    )
    pre_positive_n = models.PositiveIntegerField(default=0)
    pre_negative_n = models.PositiveIntegerField(default=0)
    pre_unresolved_n = models.PositiveIntegerField(
        default=0,
        help_text='Matured baseline members whose latest '
                   'OutcomeSnapshot showed no terminal',
    )
    pre_rate = models.FloatField(
        null=True, blank=True,
        help_text='pre_positive_n / pre_n. Null when pre_n == 0.',
    )

    # -- post (ACCUMULATES) --
    post_matured_n = models.PositiveIntegerField(default=0)
    post_n = models.PositiveIntegerField(default=0)
    post_positive_n = models.PositiveIntegerField(default=0)
    post_negative_n = models.PositiveIntegerField(default=0)
    post_unresolved_n = models.PositiveIntegerField(default=0)
    post_rate = models.FloatField(null=True, blank=True)

    # -- provenance breakdown for post conversations --
    # Per the ROM v1 provenance invariants (see project memory).
    # target_signal_conversations_n = full denominator regardless of
    # provenance state; provenance_eligible_n = the clean cohort.
    target_signal_conversations_n = models.PositiveIntegerField(default=0)
    provenance_eligible_n = models.PositiveIntegerField(default=0)
    provenance_pending_n = models.PositiveIntegerField(default=0)
    provenance_hash_failed_n = models.PositiveIntegerField(default=0)
    provenance_schema_mismatch_n = models.PositiveIntegerField(default=0)
    contaminated_n = models.PositiveIntegerField(
        default=0,
        help_text='post conversations with matching managed hash but '
                   'differing full hash — environment contamination',
    )
    treatment_moved_n = models.PositiveIntegerField(
        default=0,
        help_text='post conversations whose managed hash differs from '
                   'treatment — the BehaviorOS-managed rule itself '
                   'changed after apply, so these belong to a '
                   'different measurement cohort',
    )

    # -- verdict (deterministic, recomputable) --
    effect_size_pp = models.FloatField(
        null=True, blank=True,
        help_text='(post_rate - pre_rate) in percentage points',
    )
    ci_low_pp = models.FloatField(null=True, blank=True)
    ci_high_pp = models.FloatField(null=True, blank=True)
    p_value = models.FloatField(null=True, blank=True)

    # -- status --
    status = models.CharField(
        max_length=32, choices=Status.choices,
        default=Status.BASELINE_FROZEN,
    )
    status_reason = models.CharField(
        max_length=255, blank=True, default='',
        help_text='One-line explanation of why status is what it is '
                   '(e.g. "sample_below_floor", "provenance_coverage_'
                   'below_floor", "improved: +12.5pp p=0.03")',
    )
    evaluation_version = models.CharField(
        max_length=32, blank=True, default='',
        help_text='Version tag of the evaluator that produced the '
                   'current status/verdict — bumps when scoring logic '
                   'changes so historical rows are traceable',
    )

    # -- timing --
    measurement_started_at = models.DateTimeField()
    measurement_deadline_at = models.DateTimeField(
        help_text='applied_at + max_window_days_for_inconclusive; '
                   'evaluator transitions to INCONCLUSIVE at deadline '
                   'if no other terminal has fired',
    )
    last_evaluated_at = models.DateTimeField(null=True, blank=True)
    finalized_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Set when status enters a TERMINAL_STATUSES value; '
                   'evaluator refuses to update the row after this',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['lb_recommendation_application_id'],
                name='rom_v1_lb_app_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['org', 'status']),
            models.Index(fields=['tenant_external_id', 'status']),
            models.Index(fields=['recommendation']),
            models.Index(fields=['measurement_deadline_at']),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return (f'ROM({self.recommendation.recommendation_id} '
                f'{self.target_signal}) → {self.status}')

    def provenance_coverage(self) -> float | None:
        """The metric flagged as required in the ROM v1 provenance
        invariants (see project memory). Returns None when the
        denominator is 0 — no target-signal conversations observed
        yet, so coverage is undefined rather than 0.

        eligible / target_signal_conversations
        """
        if self.target_signal_conversations_n == 0:
            return None
        return (
            self.provenance_eligible_n
            / self.target_signal_conversations_n
        )

    def post_outcome_resolution_coverage(self) -> float | None:
        """Fraction of MATURED post-cohort members with a known
        terminal. Complements provenance_coverage: even if provenance
        is clean, slow / partial outcome-resolver coverage would bias
        the rate. None when no matured post members yet."""
        if self.post_matured_n == 0:
            return None
        return self.post_n / self.post_matured_n

    def pre_outcome_resolution_coverage(self) -> float | None:
        """Same idea for the baseline arm (frozen at creation)."""
        if self.pre_matured_n == 0:
            return None
        return self.pre_n / self.pre_matured_n

    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Pipeline 1D: Observed Business Configuration Extraction
# ---------------------------------------------------------------------------
#
# 1B / 1C / 1B-3 answered "what behavioral pattern did agents follow?"
# 1D answers a different, less-causal question: "what business content
# (prices, qualification questions, FAQ topics, service scope rules)
# appears in real conversations, and does it match what the LB config
# says?"
#
# Design constraints (from the milestone spec):
# - Observed facts come from Conversations ONLY. Never from LB config.
# - Configured facts come from TenantConfigSnapshot ONLY. Never inferred
#   from observations.
# - The diff is deterministic. LLM is used to STRUCTURE evidence into
#   normalized facts; it never emits the diff verdict.
# - Partial-key observations (e.g. "deep clean = $259" with no bedroom
#   count) cannot MATCH or CONFLICT with an exact-key configured entry.
#   They land in PARTIAL_KEY_COMPATIBLE / INSUFFICIENT_EVIDENCE.
# - Semantic anomalies discovered by 1D (e.g. QUESTION_FAQ events that
#   are actually payment/verification) become OntologyReviewCandidate
#   records. These do NOT auto-modify the extractor.


class ObservedBusinessFact(BaseModel):
    """One normalized fact derived from conversation evidence.

    Domain-neutral shape: `domain` + `fact_type` classify the row;
    `subject_key_json` is the canonical join key against
    ConfiguredBusinessFact; `value_json` holds the domain-specific
    payload (distribution for pricing, presence for qualification,
    cluster centroid for FAQ, etc.).

    Aggregated across an ObservedFactExtractionRun. The aggregator
    computes distribution stats (support_n, percentiles) and stores
    evidence_conversation_ids / evidence_turn_ids for provenance.

    A single (extraction_run, domain, fact_type, subject_key_hash)
    row is unique — reruns produce NEW extraction_runs, not in-place
    edits.
    """

    class Domain(models.TextChoices):
        PRICING = 'pricing', 'Pricing / quoted amounts'
        QUALIFICATION = 'qualification', 'Qualification questions'
        FAQ = 'faq', 'FAQ / customer question topics'
        SERVICE_SCOPE = 'service_scope', 'Service scope and boundaries'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE,
        related_name='observed_business_facts',
    )
    corpus = models.ForeignKey(
        LearningCorpus, on_delete=models.CASCADE,
        related_name='observed_business_facts',
    )
    extraction_run = models.ForeignKey(
        'ObservedFactExtractionRun', on_delete=models.CASCADE,
        related_name='facts',
    )

    domain = models.CharField(max_length=32, choices=Domain.choices)
    fact_type = models.CharField(
        max_length=64,
        help_text='Domain-specific token, e.g. quoted_price / '
                   'price_range / question_topic / scope_rule',
    )

    # Canonical join key against ConfiguredBusinessFact. Sorted-key
    # JSON so comparisons are deterministic.
    subject_key_json = models.JSONField(default=dict)
    # Which key dimensions are actually PRESENT — the specificity
    # gate for MATCH/CONFLICT eligibility. E.g. for pricing:
    # ['service'] (only service known) vs
    # ['service', 'bedrooms', 'bathrooms'] (full key).
    subject_key_dimensions = models.JSONField(default=list)
    # Content-hash of the canonical key for cheap indexing / dedup.
    subject_key_hash = models.CharField(max_length=64, db_index=True)

    # Domain-specific value payload — e.g. pricing distribution stats;
    # qualification presence + frequency; FAQ cluster centroid.
    value_json = models.JSONField(default=dict)

    support_n = models.PositiveIntegerField(
        default=0,
        help_text='Independent conversations contributing evidence',
    )
    aggregate_confidence = models.FloatField(
        default=0.0,
        help_text='Mean or min of underlying event confidences',
    )
    evidence_conversation_ids = models.JSONField(
        default=list, blank=True,
        help_text='Up to 20 conversation UUIDs',
    )
    evidence_turn_ids = models.JSONField(
        default=list, blank=True,
        help_text='Up to 20 {conversation_id, turn_id} pairs',
    )
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['extraction_run', 'domain', 'fact_type',
                        'subject_key_hash'],
                name='observed_business_fact_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['org', 'domain']),
            models.Index(fields=['extraction_run', 'domain']),
            models.Index(fields=['domain', 'fact_type']),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.domain}/{self.fact_type} n={self.support_n}'


class ConfiguredBusinessFact(BaseModel):
    """One normalized fact parsed from the LB TenantConfigSnapshot.

    Kept in a SEPARATE table from ObservedBusinessFact so the
    provenance boundary is explicit — a query cannot accidentally
    treat configured intent as observed reality. Same shape
    otherwise so the diff can join.

    Unique per (snapshot, domain, fact_type, subject_key_hash).
    """

    snapshot = models.ForeignKey(
        TenantConfigSnapshot, on_delete=models.CASCADE,
        related_name='configured_business_facts',
    )
    parser_run = models.ForeignKey(
        'ConfiguredFactParserRun', on_delete=models.CASCADE,
        related_name='facts',
    )

    domain = models.CharField(
        max_length=32,
        choices=ObservedBusinessFact.Domain.choices,
    )
    fact_type = models.CharField(max_length=64)

    subject_key_json = models.JSONField(default=dict)
    subject_key_dimensions = models.JSONField(default=list)
    subject_key_hash = models.CharField(max_length=64, db_index=True)

    value_json = models.JSONField(default=dict)

    # Pointer back into the raw_config JSON for provenance.
    source_pointer = models.JSONField(default=dict, blank=True)
    parser_confidence = models.FloatField(default=1.0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['parser_run', 'domain', 'fact_type',
                        'subject_key_hash'],
                name='configured_business_fact_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['snapshot', 'domain']),
            models.Index(fields=['parser_run', 'domain']),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.domain}/{self.fact_type} (configured)'


class ObservedFactExtractionRun(BaseModel):
    """One run of the observed-fact extractor over a corpus for a
    specific domain. Idempotent per (org, corpus, domain,
    extractor_version)."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE,
        related_name='observed_fact_extraction_runs',
    )
    corpus = models.ForeignKey(
        LearningCorpus, on_delete=models.CASCADE,
        related_name='observed_fact_extraction_runs',
    )
    domain = models.CharField(
        max_length=32,
        choices=ObservedBusinessFact.Domain.choices,
    )
    extractor_version = models.CharField(max_length=64)
    model = models.CharField(max_length=64, blank=True, default='')

    status = models.CharField(
        max_length=16, choices=Status.choices,
        default=Status.PENDING,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    conversations_processed = models.PositiveIntegerField(default=0)
    facts_emitted = models.PositiveIntegerField(default=0)
    ontology_review_candidates_emitted = models.PositiveIntegerField(
        default=0,
    )
    llm_input_tokens = models.PositiveIntegerField(default=0)
    llm_output_tokens = models.PositiveIntegerField(default=0)
    llm_cost_usd = models.DecimalField(
        max_digits=10, decimal_places=4, default=0,
    )
    stats_json = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        indexes = [
            models.Index(fields=['org', 'domain', '-created_at']),
            models.Index(fields=['corpus', 'domain', '-created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return (f'ObservedFactRun({self.domain}) '
                f'{self.extractor_version} → {self.status}')


class ConfiguredFactParserRun(BaseModel):
    """One run of the LB config parser over a snapshot for a specific
    domain. Idempotent per (snapshot, domain, parser_version)."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    snapshot = models.ForeignKey(
        TenantConfigSnapshot, on_delete=models.CASCADE,
        related_name='configured_fact_parser_runs',
    )
    domain = models.CharField(
        max_length=32,
        choices=ObservedBusinessFact.Domain.choices,
    )
    parser_version = models.CharField(max_length=64)
    model = models.CharField(max_length=64, blank=True, default='')

    status = models.CharField(
        max_length=16, choices=Status.choices,
        default=Status.PENDING,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    facts_emitted = models.PositiveIntegerField(default=0)
    llm_input_tokens = models.PositiveIntegerField(default=0)
    llm_output_tokens = models.PositiveIntegerField(default=0)
    llm_cost_usd = models.DecimalField(
        max_digits=10, decimal_places=4, default=0,
    )
    stats_json = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        indexes = [
            models.Index(fields=['snapshot', 'domain', '-created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return (f'ConfiguredFactRun({self.domain}) '
                f'{self.parser_version} → {self.status}')


class OntologyReviewCandidate(BaseModel):
    """Evidence for a future ontology / extractor-prompt reliability
    pass. Emitted by 1D extractors when they encounter events whose
    current ontology classification looks wrong (e.g. QUESTION_FAQ
    events that are actually payment / verification / operations).

    Never auto-alters the ontology or extractor. Reviewed manually.
    """

    class Kind(models.TextChoices):
        EVENT_MIS_CLASSIFIED = (
            'event_mis_classified',
            'Event type appears misapplied to this evidence',
        )
        OTHER_CLUSTER = (
            'other_cluster',
            'FAQ/qualification cluster labeled OTHER; may need taxonomy expansion',
        )

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE,
        related_name='ontology_review_candidates',
    )
    extraction_run = models.ForeignKey(
        ObservedFactExtractionRun, on_delete=models.CASCADE,
        related_name='ontology_review_candidates',
    )
    kind = models.CharField(max_length=32, choices=Kind.choices)
    original_event_type = models.CharField(
        max_length=64, blank=True, default='',
        help_text='For EVENT_MIS_CLASSIFIED: the event_type on the '
                   'ConversationSemanticEvent',
    )
    proposed_scope = models.CharField(
        max_length=64, blank=True, default='',
        help_text='E.g. operational, payment_flow, marketing_scope',
    )
    proposed_topic = models.CharField(max_length=128, blank=True, default='')

    evidence_conversation_id = models.CharField(max_length=64, blank=True)
    evidence_turn_id = models.CharField(max_length=64, blank=True)
    evidence_text = models.CharField(max_length=1000, blank=True)
    confidence = models.FloatField(default=0.0)
    reviewed = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=['org', 'kind', 'reviewed']),
            models.Index(
                fields=['extraction_run', 'kind'],
            ),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'OntologyReview({self.kind}) {self.original_event_type}'


# ---------------------------------------------------------------------------
# Pipeline 1D Hardening: Unified Business Reconstruction
# ---------------------------------------------------------------------------
#
# The four 1D verticals each produce their own diff. Reconstruction
# consolidates them into ONE canonical object per business fact, with
# harmonized vocabulary (relationship_to_config with 8 values) and an
# onboarding classification (SAFE_TO_PROPOSE / NEEDS_OWNER_CONFIRMATION /
# DO_NOT_PROPOSE).
#
# CRITICAL invariants:
# - Contradictory observed behavior (e.g. deep_cleaning + baseboards
#   with 19 EXTRA_CHARGE + 7 INCLUDED + 1 EXCLUDED) must be preserved
#   as CONTRADICTORY_OBSERVED_BEHAVIOR — never silently majority-voted.
# - Hardening happens at the RECONSTRUCTION BOUNDARY. Do not
#   re-extract to cosmetically clean old rows. Normalize / reject
#   at reconstruction time and record the reason in quality_flags.
# - Cross-vertical dedup: a "$50 late cancellation fee" observed
#   by both scope and pricing extractors becomes ONE canonical
#   ReconstructedBusinessFact with combined provenance.


class UnifiedBusinessReconstructionRun(BaseModel):
    """One reconstruction run for a tenant. Reads the latest completed
    ObservedFactExtractionRun per domain + latest ConfiguredFactParserRun
    per domain, produces ReconstructedBusinessFact rows.

    Idempotent per (org, tenant_external_id, reconstruction_version).
    Reruns create a new row so historical runs stay traceable.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE,
        related_name='reconstruction_runs',
    )
    tenant_external_id = models.CharField(max_length=128)
    snapshot = models.ForeignKey(
        TenantConfigSnapshot, on_delete=models.CASCADE,
        related_name='reconstruction_runs',
    )
    reconstruction_version = models.CharField(max_length=64)
    input_pricing_run = models.ForeignKey(
        'ObservedFactExtractionRun', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    input_qualification_run = models.ForeignKey(
        'ObservedFactExtractionRun', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    input_faq_run = models.ForeignKey(
        'ObservedFactExtractionRun', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    input_service_scope_run = models.ForeignKey(
        'ObservedFactExtractionRun', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    input_pricing_parser = models.ForeignKey(
        'ConfiguredFactParserRun', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    input_qualification_parser = models.ForeignKey(
        'ConfiguredFactParserRun', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    input_faq_parser = models.ForeignKey(
        'ConfiguredFactParserRun', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    input_service_scope_parser = models.ForeignKey(
        'ConfiguredFactParserRun', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    status = models.CharField(
        max_length=16, choices=Status.choices,
        default=Status.PENDING,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    facts_emitted = models.PositiveIntegerField(default=0)
    stats_json = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        indexes = [
            models.Index(
                fields=['org', 'tenant_external_id', '-created_at'],
            ),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return (
            f'Reconstruction({self.tenant_external_id[:12]}) '
            f'{self.reconstruction_version} → {self.status}'
        )


class ReconstructedBusinessFact(BaseModel):
    """ONE canonical business fact assembled from observed evidence +
    configured equivalent + quality signals.

    domain     — pricing | qualification | faq | service_scope |
                  cross_vertical
    canonical_subject_json — normalized subject key (may combine
                             partial info from multiple verticals)
    observed_value_json — aggregate distribution / rate / phrasing
                          from ObservedBusinessFact input(s)
    configured_equivalent_json — matching ConfiguredBusinessFact
                                  value(s) if any
    relationship_to_config — 8-value enum (see class):
        CONFIRMED_BY_BEHAVIOR / OBSERVED_NOT_CONFIGURED /
        CONFIGURED_NOT_OBSERVED / CONTRADICTORY_OBSERVED_BEHAVIOR /
        CONTEXT_DEPENDENT / LIKELY_LEAD_SOURCE_PROVIDED /
        ONTOLOGY_OR_EXTRACTION_ISSUE / INSUFFICIENT_EVIDENCE
    consistency — consistent | contradictory | context_dependent
    quality_flags — [tokens] flagged at reconstruction time
    onboarding_class — SAFE_TO_PROPOSE / NEEDS_OWNER_CONFIRMATION /
                        DO_NOT_PROPOSE
    """

    class RelationshipToConfig(models.TextChoices):
        CONFIRMED_BY_BEHAVIOR = (
            'CONFIRMED_BY_BEHAVIOR',
            'Configured rule + observed evidence supports it',
        )
        OBSERVED_NOT_CONFIGURED = (
            'OBSERVED_NOT_CONFIGURED',
            'Real observed rule with no configured entry',
        )
        CONFIGURED_NOT_OBSERVED = (
            'CONFIGURED_NOT_OBSERVED',
            'Configured rule with no supporting observation',
        )
        CONTRADICTORY_OBSERVED_BEHAVIOR = (
            'CONTRADICTORY_OBSERVED_BEHAVIOR',
            'Agents contradict each other on the same rule',
        )
        CONTEXT_DEPENDENT = (
            'CONTEXT_DEPENDENT',
            'Rule varies by service/frequency/condition/promo',
        )
        LIKELY_LEAD_SOURCE_PROVIDED = (
            'LIKELY_LEAD_SOURCE_PROVIDED',
            'Configured field likely pre-populated by Thumbtack/Yelp',
        )
        ONTOLOGY_OR_EXTRACTION_ISSUE = (
            'ONTOLOGY_OR_EXTRACTION_ISSUE',
            'Fact has a known extraction / ontology quality issue',
        )
        INSUFFICIENT_EVIDENCE = (
            'INSUFFICIENT_EVIDENCE',
            'Not enough support to classify either way',
        )

    class Consistency(models.TextChoices):
        CONSISTENT = 'consistent', 'Consistent across support'
        CONTRADICTORY = 'contradictory', 'Contradictory across support'
        CONTEXT_DEPENDENT = 'context_dependent', 'Varies by context'
        UNDETERMINED = 'undetermined', 'Not enough evidence'

    class OnboardingClass(models.TextChoices):
        SAFE_TO_PROPOSE = (
            'SAFE_TO_PROPOSE',
            'High support + consistent + no quality flags',
        )
        NEEDS_OWNER_CONFIRMATION = (
            'NEEDS_OWNER_CONFIRMATION',
            'Contradictory, context-dependent, or medium quality',
        )
        DO_NOT_PROPOSE = (
            'DO_NOT_PROPOSE',
            'Low support / malformed / extraction issue',
        )

    reconstruction_run = models.ForeignKey(
        UnifiedBusinessReconstructionRun, on_delete=models.CASCADE,
        related_name='facts',
    )
    domain = models.CharField(max_length=32)
    canonical_subject_json = models.JSONField(default=dict)
    canonical_subject_hash = models.CharField(
        max_length=64, db_index=True,
    )
    observed_value_json = models.JSONField(default=dict, blank=True)
    context_json = models.JSONField(default=dict, blank=True)
    configured_equivalent_json = models.JSONField(
        default=dict, blank=True,
    )
    support_n = models.PositiveIntegerField(default=0)
    aggregate_confidence = models.FloatField(default=0.0)
    consistency = models.CharField(
        max_length=32, choices=Consistency.choices,
        default=Consistency.UNDETERMINED,
    )
    relationship_to_config = models.CharField(
        max_length=48, choices=RelationshipToConfig.choices,
    )
    quality_flags = models.JSONField(default=list, blank=True)
    onboarding_class = models.CharField(
        max_length=32, choices=OnboardingClass.choices,
    )
    onboarding_rationale = models.TextField(blank=True, default='')
    evidence_conversation_ids = models.JSONField(
        default=list, blank=True,
    )
    evidence_turn_ids = models.JSONField(default=list, blank=True)
    # Provenance — which observed / configured facts contributed.
    # Cross-vertical dedup can attach IDs from multiple verticals.
    source_observed_fact_ids = models.JSONField(
        default=list, blank=True,
    )
    source_configured_fact_ids = models.JSONField(
        default=list, blank=True,
    )

    class Meta:
        indexes = [
            models.Index(
                fields=['reconstruction_run', 'domain'],
            ),
            models.Index(
                fields=[
                    'reconstruction_run',
                    'relationship_to_config',
                ],
            ),
            models.Index(
                fields=['reconstruction_run', 'onboarding_class'],
            ),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return (
            f'ReconFact({self.domain}) '
            f'{self.relationship_to_config} / '
            f'{self.onboarding_class}'
        )
