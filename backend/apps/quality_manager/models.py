"""Quality Manager V1 data model.

Two tables:
  * `QualityRun` — one per (tenant, reconstruction_run, qm_version)
    idempotency key.
  * `QualityEvaluation` — one per (run, conversation | corpus-level,
    dimension, subject) tuple.

Four states per evaluation: PASS / FAIL / UNKNOWN_NOT_EVALUABLE /
NOT_APPLICABLE. FAIL rows carry severity + evidence pointers.

Corpus-level evaluations (patterns across many conversations, e.g.
"oven addon $30 vs $40 across 32 convs") have `conversation=NULL`
and reference the driving ReconstructedBusinessFact via
`source_reconstructed_fact_id`.
"""

from django.db import models

from apps.common.models import BaseModel


class QualityRun(BaseModel):
    """One QM invocation over one reconstruction run.

    Idempotency key: (org, reconstruction_run, qm_version). Re-runs
    with the SAME version reuse the existing row. A version bump
    creates a fresh run so historical comparisons stay clean.

    Not tied to a corpus directly — reconstruction_run already carries
    the org + tenant + snapshot lineage, so QM inherits it transitively.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE,
        related_name='quality_runs',
    )
    reconstruction_run = models.ForeignKey(
        'conversations.UnifiedBusinessReconstructionRun',
        on_delete=models.CASCADE, related_name='quality_runs',
    )
    qm_version = models.CharField(max_length=64, default='qm-v1')

    # Which dimensions were enabled for this run. Kept explicit so a
    # later run with more dimensions doesn't need to re-invalidate
    # prior evaluations for dimensions that already ran.
    dimensions_enabled_json = models.JSONField(
        default=list, blank=True,
        help_text='List of dimension names, e.g. ["pricing_correctness"]',
    )

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Aggregated counts per dimension:
    #   {
    #     "pricing_correctness": {
    #         "PASS": N, "FAIL": N, "UNKNOWN_NOT_EVALUABLE": N,
    #         "NOT_APPLICABLE": N,
    #         "by_severity": {"info": N, "warning": N, "critical": N},
    #         "by_unknown_reason": {"insufficient_context": N, ...}
    #     }
    #   }
    stats_json = models.JSONField(default=dict, blank=True)

    conversations_evaluated = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['org', 'reconstruction_run', 'qm_version'],
                name='qm_run_org_recon_version_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['org', '-created_at']),
            models.Index(fields=['status']),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'QualityRun({self.qm_version}) org={self.org_id} status={self.status}'


class QualityEvaluation(BaseModel):
    """One evaluation for one (run, conversation, dimension, subject) tuple.

    `conversation` is nullable so the same table can hold both:
      * Per-conversation evaluations (drill-down surface).
      * Corpus-level pattern evaluations (tenant findings list —
        e.g. "oven addon differs across 32 conversations" as ONE
        finding, not 32 duplicates).

    State enum is CANONICAL — do not add values without a design review:
      PASS                  — evaluated, matched configured expectation
      FAIL                  — evaluated, did NOT match; severity set
      UNKNOWN_NOT_EVALUABLE — inputs insufficient to decide;
                              reason_code identifies what was missing
      NOT_APPLICABLE        — dimension doesn't apply (e.g. Pricing
                              Correctness on a conversation with no
                              price quote)
    """

    class State(models.TextChoices):
        PASS = 'PASS', 'Pass'
        FAIL = 'FAIL', 'Fail'
        UNKNOWN_NOT_EVALUABLE = 'UNKNOWN_NOT_EVALUABLE', 'Unknown / not evaluable'
        NOT_APPLICABLE = 'NOT_APPLICABLE', 'Not applicable'

    class Severity(models.TextChoices):
        INFO = 'info', 'Info'
        WARNING = 'warning', 'Warning'
        CRITICAL = 'critical', 'Critical'

    run = models.ForeignKey(
        QualityRun, on_delete=models.CASCADE,
        related_name='evaluations',
    )
    conversation = models.ForeignKey(
        'conversations.Conversation',
        on_delete=models.CASCADE, null=True, blank=True,
        related_name='quality_evaluations',
        help_text=(
            'Null for corpus-level pattern evaluations. Non-null for '
            'per-conversation drill-down.'
        ),
    )

    dimension = models.CharField(
        max_length=64,
        help_text='Dimension name, e.g. "pricing_correctness"',
    )
    subject_key_json = models.JSONField(
        default=dict, blank=True,
        help_text='Canonical subject the evaluation is about',
    )

    state = models.CharField(max_length=32, choices=State.choices)
    severity = models.CharField(
        max_length=16, choices=Severity.choices,
        blank=True, default='',
        help_text='Only set when state=FAIL',
    )
    # Machine-readable grouping key. Examples for pricing:
    #   observed_below_configured, observed_above_configured,
    #   no_price_quoted, missing_canonical_bedrooms,
    #   no_configured_rule_for_subject, insufficient_context.
    reason_code = models.CharField(max_length=64, blank=True, default='')
    rationale_text = models.TextField(blank=True, default='')

    # Evidence pointers — a list of typed refs so the drill-down UI can
    # render the full chain: conversation turn → canonical context →
    # configured rule → matcher output.
    # Shape: [{"kind": "canonical_context|conversation_turn|configured_rule|
    #          reconstructed_fact|observed_fact", "ref": <str>, "description": "..."}, ...]
    evidence_json = models.JSONField(default=list, blank=True)

    # Optional link to the reconstruction fact that drove this evaluation
    # (present for corpus-level pattern findings and per-conversation
    # evaluations that consulted the aggregate).
    source_reconstructed_fact_id = models.UUIDField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['run', 'dimension', 'state']),
            models.Index(fields=['conversation', 'dimension']),
            models.Index(fields=['run', 'state', 'severity']),
        ]
        ordering = ['-created_at']

    def __str__(self) -> str:
        target = (
            f'conv={self.conversation_id}' if self.conversation_id
            else '(corpus)'
        )
        return f'{self.dimension}/{self.state} {target}'
