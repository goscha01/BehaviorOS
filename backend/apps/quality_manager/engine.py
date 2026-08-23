"""QM engine — runs registered dimensions against one reconstruction.

Entry point: `run_quality_manager(reconstruction_run, qm_version=..., dimensions=...)`.

Behavior:
  * Idempotent per (org, reconstruction_run, qm_version). Re-invoking
    on the same key returns the existing QualityRun row and does NOT
    re-emit evaluations. Bump `qm_version` for a fresh run.
  * Deterministic — for the same reconstruction_run + dimension set,
    the output row content is the same across invocations (only
    `created_at`/`updated_at` differ).
  * Per-conversation loop iterates every Conversation whose org
    matches the reconstruction's org. Corpus-level dimension.evaluate_corpus
    is called ONCE per run (not per conversation).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Iterable, Optional

from django.db import transaction
from django.utils import timezone

from apps.conversations.models import (
    Conversation,
    UnifiedBusinessReconstructionRun,
)
from apps.quality_manager.dimensions import (
    BaseDimension,
    DimensionResult,
    State,
    get_dimension,
    iter_registered,
)
from apps.quality_manager.models import (
    QualityEvaluation,
    QualityRun,
)


logger = logging.getLogger(__name__)


def create_or_reuse_run(
    reconstruction_run: UnifiedBusinessReconstructionRun,
    *,
    qm_version: str = 'qm-v1',
    dimensions: Optional[list[str]] = None,
) -> tuple[QualityRun, bool]:
    """Look up or create the QualityRun row.

    Returns (run, created). If created, the row is PENDING.
    If reused and already COMPLETED, the caller should NOT re-run —
    just return the existing run.
    """
    enabled = _resolve_dimensions(dimensions)
    enabled_names = [cls.name for cls in enabled]
    with transaction.atomic():
        run, created = QualityRun.objects.get_or_create(
            org=reconstruction_run.org,
            reconstruction_run=reconstruction_run,
            qm_version=qm_version,
            defaults={
                'dimensions_enabled_json': enabled_names,
                'status': QualityRun.Status.PENDING,
            },
        )
    return run, created


def run_quality_manager(
    run: QualityRun,
) -> QualityRun:
    """Execute the QM run. Called by the task after create_or_reuse.

    Marks status=RUNNING at start, COMPLETED at end (or FAILED on
    unrecoverable error). Persists evaluations transactionally per
    conversation so a partial failure preserves earlier progress.
    """
    if run.status == QualityRun.Status.COMPLETED:
        return run

    dimensions = [
        get_dimension(name) for name in run.dimensions_enabled_json
    ]
    dimensions = [cls() for cls in dimensions if cls is not None]
    if not dimensions:
        run.status = QualityRun.Status.FAILED
        run.error_message = 'no dimensions enabled or resolved'
        run.save(update_fields=['status', 'error_message', 'updated_at'])
        return run

    run.status = QualityRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=['status', 'started_at', 'updated_at'])

    stats: dict[str, dict] = defaultdict(lambda: {
        'PASS': 0, 'FAIL': 0,
        'UNKNOWN_NOT_EVALUABLE': 0, 'NOT_APPLICABLE': 0,
        'by_severity': {'info': 0, 'warning': 0, 'critical': 0},
        'by_unknown_reason': {},
        'corpus_pattern_findings': 0,
    })
    conv_count = 0

    try:
        # --- Corpus-level pass (patterns across many conversations) ---
        for dim in dimensions:
            for result in dim.evaluate_corpus(
                reconstruction_run=run.reconstruction_run,
            ):
                _persist(run, result)
                _tally(stats, result)
                if result.conversation_id is None:
                    stats[dim.name]['corpus_pattern_findings'] += 1

        # --- Per-conversation pass ---
        conversations = Conversation.objects.filter(
            org=run.org,
        ).order_by('started_at')
        for conv in conversations:
            conv_count += 1
            for dim in dimensions:
                try:
                    for result in dim.evaluate(
                        reconstruction_run=run.reconstruction_run,
                        conversation=conv,
                    ):
                        _persist(run, result)
                        _tally(stats, result)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        'qm dimension=%s failed on conv=%s: %s',
                        dim.name, conv.id, exc,
                    )
    except Exception as exc:  # noqa: BLE001
        run.status = QualityRun.Status.FAILED
        run.error_message = str(exc)[:2000]
        run.completed_at = timezone.now()
        run.stats_json = _finalize_stats(stats)
        run.conversations_evaluated = conv_count
        run.save()
        return run

    run.status = QualityRun.Status.COMPLETED
    run.completed_at = timezone.now()
    run.stats_json = _finalize_stats(stats)
    run.conversations_evaluated = conv_count
    run.save()
    return run


def _resolve_dimensions(
    dimensions: Optional[list[str]],
) -> list[type[BaseDimension]]:
    if dimensions is None:
        return iter_registered()
    resolved: list[type[BaseDimension]] = []
    for name in dimensions:
        cls = get_dimension(name)
        if cls is None:
            raise ValueError(f'unknown QM dimension: {name}')
        resolved.append(cls)
    return resolved


def _persist(run: QualityRun, result: DimensionResult) -> None:
    QualityEvaluation.objects.create(
        run=run,
        conversation_id=result.conversation_id,
        dimension=result.dimension,
        subject_key_json=result.subject_key,
        state=result.state.value,
        severity=result.severity,
        reason_code=result.reason_code,
        rationale_text=result.rationale_text,
        evidence_json=[e.to_json() for e in result.evidence],
        source_reconstructed_fact_id=result.source_reconstructed_fact_id,
    )


def _tally(stats: dict, result: DimensionResult) -> None:
    per_dim = stats[result.dimension]
    per_dim[result.state.value] += 1
    if result.state == State.FAIL:
        sev = result.severity or 'warning'
        per_dim['by_severity'][sev] = per_dim['by_severity'].get(sev, 0) + 1
    if result.state == State.UNKNOWN_NOT_EVALUABLE and result.reason_code:
        per_dim['by_unknown_reason'][result.reason_code] = (
            per_dim['by_unknown_reason'].get(result.reason_code, 0) + 1
        )


def _finalize_stats(stats: dict) -> dict:
    """Turn defaultdict into a plain dict for JSONField storage."""
    return {k: dict(v) for k, v in stats.items()}
