"""BehaviorOS Context Engine — persistence.

Two overlapping concerns live here:

1. **Diagnostics** — `ContextRequestLog` records what runtimes asked,
   what sources returned, and how long the call took. Predates Phase 3.

2. **Evidence pipeline state** — `EvidenceEvent` is the durable trail
   of every runtime interaction (LeadBridge/Callio/historical import),
   and the two `*Aggregate` tables are rolling stats derived from it.
   Introduced in Phase 3.

The Evidence tables are deliberately separate from `apps.learning.EvidenceInsight`.
EvidenceInsight is analyst-facing: LLM-annotated, clustered, expensive.
EvidenceEvent is cheap, high-cardinality, and never-analyzed — it captures
the fact that "this thing happened" without opinion. A future job may
promote selected EvidenceEvents into EvidenceInsights; nothing does that yet.
"""

from django.db import models
from django.db.models import Q

from apps.common.models import BaseModel


class ContextRequestLog(BaseModel):
    """One row per POST /v1/context call.

    Persisted whether the response ended up being 'no_context' or
    'context', and whether BEHAVIOR_CONTEXT_ENABLED gated the response
    or not. `returned_to_runtime` distinguishes shadow runs from live
    runs so we can measure lift without corrupting the ratio.
    """

    class Status(models.TextChoices):
        NO_CONTEXT = 'no_context', 'No context'
        CONTEXT = 'context', 'Context returned'

    org = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='context_requests',
        help_text='Resolved from request.tenantId. Null when tenantId did not '
                  'map to a known org — we still log the request so we can '
                  'diagnose misrouted traffic.',
    )
    tenant_id = models.CharField(
        max_length=128, blank=True,
        help_text='Raw tenantId from the request. Kept even when it resolves '
                  'to an org so we can spot mapping drift.',
    )
    runtime = models.CharField(
        max_length=32,
        help_text='"leadbridge" | "callio" | future runtimes.',
    )
    channel = models.CharField(max_length=32, blank=True)
    event_type = models.CharField(max_length=64, blank=True)
    customer_id = models.CharField(max_length=128, blank=True)
    lead_id = models.CharField(max_length=128, blank=True)
    conversation_id = models.CharField(max_length=128, blank=True)
    request_payload = models.JSONField(
        default=dict, blank=True,
        help_text='Full request body. Keeps us honest about what runtimes '
                  'actually send vs. what we assume.',
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.NO_CONTEXT,
    )
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=0)
    context_size_bytes = models.PositiveIntegerField(
        default=0,
        help_text='Byte length of the JSON-encoded response.context object. '
                  'Zero when status = no_context.',
    )
    latency_ms = models.PositiveIntegerField(
        default=0,
        help_text='End-to-end generation time in ms — everything from view '
                  'entry to response body ready.',
    )
    source_results = models.JSONField(
        default=list, blank=True,
        help_text='Per-source breakdown: [{name, priority, contributed, '
                  'confidence, latency_ms, error}, ...]. Powers debugging '
                  'without a DB row per source.',
    )
    returned_to_runtime = models.BooleanField(
        default=False,
        help_text='False in shadow mode (BEHAVIOR_CONTEXT_ENABLED off) — we '
                  'ran the sources and logged the outcome but responded '
                  '{"status":"no_context"} to the caller.',
    )
    context_version = models.CharField(
        max_length=16, blank=True,
        help_text='Engine version at generation time (e.g. "2.0"). Bumped by '
                  'apps.context.engine when the merger algorithm changes.',
    )
    source_count = models.PositiveIntegerField(
        default=0,
        help_text='Count of Sources that contributed at least one fact / '
                  'recommendation / warning to this response.',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['org', '-created_at']),
            models.Index(fields=['runtime', '-created_at']),
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self):
        return f'ContextRequest {self.runtime}:{self.event_type} → {self.status}'


# --- Phase 3: Evidence Pipeline persistence ---------------------------------


class EvidenceEvent(BaseModel):
    """One row per runtime interaction or historical import record.

    Cheap and high-cardinality. Every LeadBridge / Callio call to
    `/v1/context` writes one; every historical backfill entry writes one
    through the same code path. No opinion is attached — this is the
    "an event happened" record, not an analysis.

    Distinct from `apps.learning.EvidenceInsight`, which is LLM-annotated
    and clustered. A future job may promote EvidenceEvents into
    EvidenceInsights; nothing does that yet.

    Idempotency: when an importer supplies `external_id`, the (org,
    source_kind, external_id) tuple is unique — re-importing the same
    historical record is a no-op. Runtime events omit `external_id` and
    are always net-new inserts.
    """

    class SourceKind(models.TextChoices):
        RUNTIME = 'runtime', 'Live runtime call'
        HISTORICAL = 'historical', 'Historical backfill'

    org = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='evidence_events',
    )
    source_kind = models.CharField(
        max_length=16, choices=SourceKind.choices, default=SourceKind.RUNTIME,
    )
    runtime = models.CharField(
        max_length=32,
        help_text='"leadbridge" | "callio" | "serviceflow" | future systems. '
                  'For historical imports, use the source system name.',
    )
    channel = models.CharField(max_length=32, blank=True)
    event_type = models.CharField(max_length=64, blank=True)
    customer_id = models.CharField(max_length=128, blank=True)
    lead_id = models.CharField(max_length=128, blank=True)
    conversation_id = models.CharField(max_length=128, blank=True)
    external_id = models.CharField(
        max_length=255, blank=True,
        help_text='Source-system-native ID for historical imports. Empty for '
                  'live runtime events (each request is inherently distinct).',
    )
    occurred_at = models.DateTimeField(
        help_text='When the real-world interaction happened. For runtime '
                  'events this is server-receive time; for historical imports '
                  'the caller supplies it.',
    )
    message_excerpt = models.CharField(
        max_length=500, blank=True,
        help_text='First 500 chars of the customer message for admin browsing. '
                  'Full body lives in `payload`.',
    )
    payload = models.JSONField(
        default=dict, blank=True,
        help_text='Full request body (runtime) or record body (historical). '
                  'Anything the sender wanted us to know.',
    )
    promoted_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Timestamp at which this event was SUCCESSFULLY promoted '
                  'into an `apps.learning.EvidenceInsight` row. Only set when '
                  'promotion_status=PROMOTED. Null otherwise (pending, skipped, '
                  'or failed).',
    )

    class PromotionStatus(models.TextChoices):
        # Never evaluated by the eligibility gate. All EvidenceEvents start here.
        PENDING = 'pending', 'Pending eligibility check'
        # Evaluator said eligible AND ingestion into EvidenceInsight succeeded.
        PROMOTED = 'promoted', 'Promoted into EvidenceInsight'
        # Evaluator said skip — see promotion_reason for the specific category.
        # Skipped events are RETAINED (still useful operational evidence) but
        # do NOT enter the learning corpus. Terminal state.
        SKIPPED = 'skipped', 'Skipped per eligibility rules'
        # Evaluator said eligible but ingestion / persistence raised.
        # promotion_reason carries the exception summary. Retryable — the
        # promoter may reset to PENDING once the root cause is fixed.
        FAILED = 'failed', 'Ingestion failure — retryable'

    promotion_status = models.CharField(
        max_length=16,
        choices=PromotionStatus.choices,
        default=PromotionStatus.PENDING,
        help_text='Terminal state of the promotion pipeline for this event. '
                  'Every event reaches a non-PENDING state after the eligibility '
                  'evaluator runs — no event stays perpetually unprocessed.',
    )
    promotion_reason = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text='Detail on the promotion decision. For SKIPPED: one of '
                  '"skip_diagnostic" | "skip_incomplete" | "skip_synthetic" | '
                  '"skip_duplicate" | "skip_unsupported". For FAILED: '
                  '"failed:<ExceptionName>". Empty for PENDING / PROMOTED.',
    )
    promotion_checked_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the eligibility evaluator last ran against this event. '
                  'Separate from promoted_at (only success time) so operators '
                  'can tell "we looked at this and rejected it" from "we never '
                  'looked at it yet".',
    )

    class Meta:
        ordering = ['-occurred_at']
        constraints = [
            # Only enforce uniqueness when external_id is populated —
            # runtime events routinely omit it.
            models.UniqueConstraint(
                fields=['org', 'source_kind', 'external_id'],
                condition=Q(external_id__gt=''),
                name='context_evidence_event_external_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['org', 'customer_id', '-occurred_at']),
            models.Index(fields=['org', 'runtime', '-occurred_at']),
            models.Index(fields=['org', 'source_kind', '-occurred_at']),
            # Fast lookup for the promotion queue: unpromoted runtime events
            # for a given org, oldest-first. Matches the query pattern in
            # apps.learning.services.promotion.
            models.Index(
                fields=['org', 'promoted_at', 'occurred_at'],
                name='ctx_event_promo_queue_idx',
            ),
            # Fast queue query for pending-status events + fast filtered
            # aggregation of skip-reason distribution. Reason lives on the
            # tail so the (org, status) prefix is still selective when
            # counting reasons irrespective of order.
            models.Index(
                fields=['org', 'promotion_status', 'promotion_reason'],
                name='ctx_event_promo_status_idx',
            ),
            models.Index(fields=['conversation_id']),
        ]

    def __str__(self):
        return f'{self.runtime}:{self.event_type} @ {self.occurred_at:%Y-%m-%d %H:%M}'


class CustomerHistoryAggregate(BaseModel):
    """Rolling per-customer stats derived from EvidenceEvents.

    Updated on every event ingestion via `apps.context.pipeline.aggregates`.
    Never authoritative — always rebuildable by re-folding EvidenceEvents.
    A future source can read this table to answer "how active is this
    customer" without scanning the event table on every request.

    One row per (org, customer_id). No `customer_id=""` rows; anonymous
    events skip aggregate updates.
    """

    org = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='customer_history_aggregates',
    )
    customer_id = models.CharField(max_length=128)
    total_events = models.PositiveIntegerField(default=0)
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    event_type_counts = models.JSONField(
        default=dict, blank=True,
        help_text='{"new_lead": 3, "inbound_call": 7, ...}',
    )
    runtime_counts = models.JSONField(
        default=dict, blank=True,
        help_text='{"leadbridge": 5, "callio": 2}',
    )
    channel_counts = models.JSONField(
        default=dict, blank=True,
        help_text='{"sms": 4, "voice": 3}',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['org', 'customer_id'],
                name='context_customer_history_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['org', '-last_seen_at']),
        ]

    def __str__(self):
        return f'{self.customer_id} ({self.total_events} events)'


class OrgStatistics(BaseModel):
    """One row per org. Rolling stats across ALL EvidenceEvents.

    Same "never authoritative" contract as CustomerHistoryAggregate —
    can always be rebuilt from the event table.
    """

    org = models.OneToOneField(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='context_statistics',
    )
    total_events = models.PositiveBigIntegerField(default=0)
    last_event_at = models.DateTimeField(null=True, blank=True)
    event_type_counts = models.JSONField(default=dict, blank=True)
    runtime_counts = models.JSONField(default=dict, blank=True)
    channel_counts = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f'{self.org} stats ({self.total_events} events)'
