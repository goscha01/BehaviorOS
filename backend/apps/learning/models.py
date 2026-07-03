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
    represents. `source_payload` carries whatever the adapter normalized.
    Idempotency is enforced by (source_system, external_id).
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
    """One aggregated business recommendation, synthesized from a cluster of
    similar `CandidateRecommendation` rows.

    BehaviorOS's product output. Never created per-conversation — every
    suggestion represents a recurring pattern across evidence.
    """

    class Category(models.TextChoices):
        PRICING = 'pricing', 'Pricing'
        FAQ = 'faq', 'FAQ'
        QUALIFICATION = 'qualification', 'Qualification flow'
        PLAYBOOK = 'playbook', 'Playbook rule'
        MISSING_INFO = 'missing_info', 'Missing information'
        TONE = 'tone', 'Tone'
        OTHER = 'other', 'Other'

    class Status(models.TextChoices):
        # Review lifecycle. Ordered by typical progression, but the API
        # enforces which transitions are valid per current state.
        NEW = 'new', 'New (freshly synthesized)'
        UNDER_REVIEW = 'under_review', 'Under review'
        APPROVED = 'approved', 'Approved'
        IMPLEMENTED = 'implemented', 'Implemented (published to target channel)'
        MEASURED = 'measured', 'Measured (impact quantified)'
        ARCHIVED = 'archived', 'Archived'
        REJECTED = 'rejected', 'Rejected'
        WATCHLIST = 'watchlist', 'Watchlist (below surface threshold)'

    # Statuses that indicate the suggestion is still "in play" — used by
    # the clustering merge pass to decide which suggestions can absorb
    # new candidates.
    ACTIVE_STATUSES = (
        'new', 'under_review', 'approved', 'implemented', 'measured', 'watchlist',
    )

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
    fingerprint = models.CharField(
        max_length=512,
        db_index=True,
        help_text='Normalized token union from supporting candidates; used for merging '
                  'new candidates and rejection checks (Jaccard-based comparison).',
    )
    confidence = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    confidence_breakdown = models.JSONField(
        default=dict,
        blank=True,
        help_text='{llm, support, outcome_consistency, final} — components explaining the score.',
    )
    representative_examples = models.JSONField(
        default=dict,
        blank=True,
        help_text='{top: [3 examples], highest_confidence: {...}, newest: {...}} — each '
                  'example = {candidate_id, evidence_id, snippet, source_system}.',
    )
    supporting_count = models.PositiveIntegerField(
        default=0,
        help_text='Denormalized count of supporting CandidateRecommendation rows.',
    )
    synthesis_json = models.JSONField(
        default=dict,
        blank=True,
        help_text='Full synthesis output: why_this_matters, supporting_evidence_summary, '
                  'suggested_playbook_change, suggested_faq_addition.',
    )
    synthesis_model = models.CharField(max_length=64, blank=True)
    synthesis_prompt_version = models.CharField(max_length=32, blank=True)
    synthesis_cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.NEW
    )
    publish_targets = models.JSONField(
        default=list,
        blank=True,
        help_text='Channel keys this suggestion should be published to when implemented '
                  '(e.g. ["leadbridge_playbook", "callio_voice_rules"]). Empty in Phase 1; '
                  'the approve endpoint persists this so a future publisher can pick it up '
                  'without breaking the API contract.',
    )
    impact_json = models.JSONField(
        default=dict,
        blank=True,
        help_text='Before/after metrics captured when the suggestion transitions to MEASURED '
                  '(e.g. {"win_rate_before": 0.42, "win_rate_after": 0.51, "sample_size": 320}).',
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


class CandidateRecommendation(BaseModel):
    """One candidate recommendation extracted from an EvidenceInsight's analysis.

    Each analysis emits zero or more candidate playbook rules and FAQ entries.
    Each becomes one CandidateRecommendation row. Clustering operates on these,
    not on evidence directly — one improvement idea can span many conversations,
    and one conversation can suggest many improvements.
    """

    class Kind(models.TextChoices):
        PLAYBOOK_RULE = 'playbook_rule', 'Playbook rule'
        FAQ = 'faq', 'FAQ'

    class OutcomeSignal(models.TextChoices):
        POSITIVE = 'positive', 'Positive (booked / won / recurring)'
        NEGATIVE = 'negative', 'Negative (cancelled / lost / no_show)'
        NEUTRAL = 'neutral', 'Neutral / unknown'

    org = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='candidate_recommendations',
    )
    evidence = models.ForeignKey(
        EvidenceInsight,
        on_delete=models.CASCADE,
        related_name='candidate_recommendations',
    )
    job = models.ForeignKey(
        LearningJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='candidate_recommendations',
        help_text='Job that produced this candidate.',
    )
    suggestion = models.ForeignKey(
        LearningSuggestion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='candidate_recommendations',
        help_text='Cluster this candidate was merged into. Null = unclustered (resume queue).',
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    category = models.CharField(
        max_length=32,
        choices=LearningSuggestion.Category.choices,
        help_text='Denormalized from analysis_json.category for filtering.',
    )
    title = models.CharField(
        max_length=400,
        help_text='For playbook_rule: rule title. For faq: the question.',
    )
    description = models.TextField(
        help_text='For playbook_rule: rule body. For faq: the answer.',
    )
    llm_confidence = models.DecimalField(
        max_digits=3, decimal_places=2, default=0,
        help_text='Per-candidate confidence from the analyzer LLM.',
    )
    outcome_signal = models.CharField(
        max_length=20, choices=OutcomeSignal.choices, default=OutcomeSignal.NEUTRAL,
        db_index=True,
    )
    fingerprint = models.CharField(
        max_length=512, db_index=True,
        help_text='Normalized token string; used for Jaccard clustering.',
    )
    tokens = models.JSONField(
        default=list, blank=True,
        help_text='Deduplicated token list matching fingerprint. Denormalized for fast set ops.',
    )
    clustered_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Null = unclustered (resume queue for clustering).',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['clustered_at']),
            models.Index(fields=['org', 'category', 'clustered_at']),
            models.Index(fields=['suggestion']),
        ]

    def __str__(self):
        return f"[{self.kind}/{self.category}] {self.title[:60]}"


class RejectedSuggestionSignature(BaseModel):
    """Fingerprint of a rejected cluster so it doesn't re-surface every night.

    Populated when a LearningSuggestion.status transitions to REJECTED.
    The clustering pass checks new candidate clusters against these
    signatures via Jaccard similarity; matches are silently dropped.
    """

    org = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='rejected_suggestion_signatures',
    )
    category = models.CharField(max_length=32, choices=LearningSuggestion.Category.choices)
    signature = models.CharField(
        max_length=512,
        help_text='Fingerprint of the rejected suggestion (token union of its candidates).',
    )
    tokens = models.JSONField(
        default=list, blank=True,
        help_text='Deduplicated tokens matching signature. Used for Jaccard comparison.',
    )
    rejected_suggestion = models.ForeignKey(
        LearningSuggestion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rejection_signatures',
    )
    rejection_reason = models.TextField(
        help_text='Required at rejection time. Persisted for future analyzer prompting '
                  '("customers rejected X for reason Y — do not re-recommend").',
    )
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rejected_suggestion_signatures',
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
            models.Index(fields=['org', 'category']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f"rejected[{self.category}]:{self.signature[:40]}"
