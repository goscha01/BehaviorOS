from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class LearningJob(BaseModel):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        PARTIAL = 'partial', 'Partial (budget exceeded, will resume)'
        FAILED = 'failed', 'Failed'

    class TriggeredBy(models.TextChoices):
        SCHEDULE = 'schedule', 'Schedule'
        MANUAL = 'manual', 'Manual'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='learning_jobs'
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    triggered_by = models.CharField(
        max_length=20, choices=TriggeredBy.choices, default=TriggeredBy.SCHEDULE
    )
    window_start = models.DateTimeField(null=True, blank=True)
    window_end = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    evidence_processed = models.PositiveIntegerField(default=0)
    evidence_skipped = models.PositiveIntegerField(default=0)
    suggestions_created = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-started_at']),
            models.Index(fields=['org', '-started_at']),
        ]

    def __str__(self):
        return f"LearningJob {self.id} ({self.status})"


class EvidenceInsight(BaseModel):
    """One record per piece of analyzed evidence (conversation, call, outcome, etc.).

    Generic on purpose — the source system decides what kind of evidence this
    represents. `payload` carries whatever the adapter normalized. Idempotency
    is enforced by (source_system, source_evidence_id).
    """

    class EvidenceType(models.TextChoices):
        CONVERSATION = 'conversation', 'Conversation'
        CALL = 'call', 'Call'
        OUTCOME = 'outcome', 'Outcome'
        OTHER = 'other', 'Other'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='evidence_insights'
    )
    job = models.ForeignKey(
        LearningJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='insights',
        help_text='Job that created this insight; may differ from the job that analyzed it.',
    )
    source_system = models.CharField(
        max_length=64,
        help_text='Free-form source key, e.g. "leadbridge", "callio", "serviceflow".',
    )
    external_id = models.CharField(
        max_length=255,
        help_text='Evidence ID at the source system. Composite unique with source_system.',
    )
    evidence_type = models.CharField(
        max_length=32, choices=EvidenceType.choices, default=EvidenceType.CONVERSATION
    )
    occurred_at = models.DateTimeField(
        null=True, blank=True, help_text='When the real-world event happened at the source.'
    )
    outcome = models.CharField(
        max_length=64,
        blank=True,
        help_text='Business outcome from the source (booked, cancelled, won, lost, etc.). Never inferred.',
    )
    outcome_metadata = models.JSONField(default=dict, blank=True)
    source_business_rules_version = models.CharField(
        max_length=64,
        blank=True,
        help_text='Playbook/business-rules version at the source at the time of the event. '
                  'Lets us compare recommendations before/after rule changes.',
    )
    source_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text='Raw evidence payload from the adapter (transcript, call data, outcome record, etc.).',
    )
    ingest_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='Adapter-level metadata about the fetch itself (cursor, source URL, fetch time, etc.).',
    )
    ai_summary = models.TextField(
        blank=True,
        help_text='Short human-readable summary extracted from analysis_json.summary. '
                  'For display only — not analyzed downstream.',
    )
    analysis_json = models.JSONField(
        default=dict,
        blank=True,
        help_text='Structured business learning from the analyzer: category, subcategory, '
                  'customer_intent, outcome_analysis, candidate_playbook_rules, candidate_faq, '
                  'signals, confidence. Schema versioned via analysis_prompt_version.',
    )
    raw_response = models.TextField(
        blank=True,
        help_text='Raw model output before JSON parsing. Kept so we can inspect exactly '
                  'what the model returned when a recommendation looks wrong.',
    )
    analysis_model = models.CharField(
        max_length=64, blank=True, help_text='Model used, e.g. "claude-haiku-4-5-20251001".'
    )
    analysis_prompt_version = models.CharField(
        max_length=32, blank=True,
        help_text='Analyzer prompt version at analysis time, e.g. "conversation:v1". '
                  'Bump when prompt changes so we can re-queue insights.',
    )
    analysis_cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    analyzed_at = models.DateTimeField(
        null=True, blank=True, help_text='Null = still queued for analysis (resume queue).'
    )

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['source_system', 'external_id'],
                name='learning_evidence_source_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['analyzed_at']),
            models.Index(fields=['source_system', '-occurred_at']),
            models.Index(fields=['org', '-occurred_at']),
        ]

    def __str__(self):
        return f"{self.source_system}:{self.external_id} ({self.evidence_type})"


class LearningSuggestion(BaseModel):
    class Category(models.TextChoices):
        PRICING = 'pricing', 'Pricing'
        FAQ = 'faq', 'FAQ'
        QUALIFICATION = 'qualification', 'Qualification flow'
        PLAYBOOK = 'playbook', 'Playbook rule'
        MISSING_INFO = 'missing_info', 'Missing information'
        TONE = 'tone', 'Tone'
        OTHER = 'other', 'Other'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        IMPLEMENTED = 'implemented', 'Implemented'
        WATCHLIST = 'watchlist', 'Watchlist (below surface threshold)'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='learning_suggestions'
    )
    job = models.ForeignKey(
        LearningJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='suggestions',
    )
    category = models.CharField(max_length=32, choices=Category.choices)
    title = models.CharField(max_length=200)
    description = models.TextField()
    confidence = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    confidence_breakdown = models.JSONField(
        default=dict,
        blank=True,
        help_text='Component scores (avg confidence, support count factor, etc.) explaining the score.',
    )
    supporting_evidence = models.ManyToManyField(
        EvidenceInsight,
        through='SuggestionEvidence',
        related_name='suggestions',
        blank=True,
    )
    supporting_count = models.PositiveIntegerField(
        default=0, help_text='Denormalized count of linked evidence, for fast list rendering.'
    )
    representative_examples = models.JSONField(
        default=list, blank=True, help_text='Up to 3 verbatim snippets for the dashboard card.'
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_suggestions',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['category', 'status']),
            models.Index(fields=['org', 'status']),
        ]

    def __str__(self):
        return f"[{self.category}] {self.title}"


class SuggestionEvidence(BaseModel):
    """Through-table linking a suggestion to the evidence that supports it."""

    suggestion = models.ForeignKey(
        LearningSuggestion, on_delete=models.CASCADE, related_name='evidence_links'
    )
    evidence = models.ForeignKey(
        EvidenceInsight, on_delete=models.CASCADE, related_name='suggestion_links'
    )
    similarity_score = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        null=True,
        blank=True,
        help_text='Reserved for future semantic clustering; null in Phase 1 keyword grouping.',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['suggestion', 'evidence'],
                name='learning_suggestion_evidence_unique',
            ),
        ]

    def __str__(self):
        return f"{self.suggestion_id} ← {self.evidence_id}"


class RejectedSuggestionSignature(BaseModel):
    """Fingerprint of a rejected cluster so it doesn't re-surface every night.

    Phase 1 signature: deterministic hash of (category + normalized keyword set).
    When semantic clustering ships later, add a signature variant based on
    centroid-embedding buckets.
    """

    org = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='rejected_suggestion_signatures',
    )
    category = models.CharField(max_length=32, choices=LearningSuggestion.Category.choices)
    signature = models.CharField(max_length=128)
    rejected_suggestion = models.ForeignKey(
        LearningSuggestion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rejection_signatures',
    )
    expires_at = models.DateTimeField(help_text='90 days from rejection by default.')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['org', 'category', 'signature'],
                name='learning_rejected_signature_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['org', 'category', 'signature']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f"rejected[{self.category}]:{self.signature[:12]}"
