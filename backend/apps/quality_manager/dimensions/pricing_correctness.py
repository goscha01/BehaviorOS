"""Pricing Correctness dimension — QM V1's only shipped dimension.

Reuses the existing deterministic Pricing 1D matcher output. Does
NOT re-implement the ±10% tolerance logic. Reads
`ReconstructedBusinessFact.relationship_to_config` verdicts and maps
them onto QM's four-state model.

State mapping:
  MATCH                          → PASS
  DIFFERS_FROM_CONFIG            → FAIL (severity from |delta_pct|)
  INSUFFICIENT_CONTEXT_TO_COMPARE → UNKNOWN_NOT_EVALUABLE
                                    (reason=insufficient_context)
  VARIABLE_CONTEXT_DEPENDENT     → UNKNOWN_NOT_EVALUABLE
                                    (reason=variable_context)
  OBSERVED_NOT_CONFIGURED        → UNKNOWN_NOT_EVALUABLE
                                    (reason=no_configured_rule_for_subject)
  ONTOLOGY_OR_EXTRACTION_ISSUE   → UNKNOWN_NOT_EVALUABLE
                                    (reason=extraction_quality_issue)

Per-conversation logic:
  * If conversation contributed to NO pricing reconstructed_fact →
    NOT_APPLICABLE (no price quoted for pricing correctness to
    evaluate against).
  * Otherwise, one QualityEvaluation per (conversation × contributing
    ReconstructedBusinessFact).

Corpus-level logic (evaluate_corpus):
  * One QualityEvaluation per DIFFERS_FROM_CONFIG aggregate — the
    tenant-facing "pattern finding" list. Evidence includes all
    supporting conversation_ids so drill-down can show every
    contributing conversation.
"""

from __future__ import annotations

from typing import Iterable

from apps.quality_manager.dimensions import register
from apps.quality_manager.dimensions.base import (
    BaseDimension,
    DimensionResult,
    EvidenceRef,
    State,
)


VERSION = 'qm-v1-pricing.1'


# Verdict → (State, reason_code) for UNKNOWN cases + severity mapping for FAIL.
_MATCH_VERDICTS = frozenset({'MATCH'})
_FAIL_VERDICTS = frozenset({'DIFFERS_FROM_CONFIG'})
_UNKNOWN_MAP: dict[str, str] = {
    'INSUFFICIENT_CONTEXT_TO_COMPARE': 'insufficient_context',
    'VARIABLE_CONTEXT_DEPENDENT': 'variable_context',
    'OBSERVED_NOT_CONFIGURED': 'no_configured_rule_for_subject',
    'ONTOLOGY_OR_EXTRACTION_ISSUE': 'extraction_quality_issue',
    # Legacy generic verdicts — non-pricing verticals shouldn't reach
    # this dimension, but if a pricing fact ever emits one of these
    # we surface it as unknown rather than crashing.
    'CONFIRMED_BY_BEHAVIOR': 'legacy_verdict_confirmed',
    'CONTRADICTORY_OBSERVED_BEHAVIOR': 'contradictory_observed',
    'CONTEXT_DEPENDENT': 'context_dependent_legacy',
    'LIKELY_LEAD_SOURCE_PROVIDED': 'lead_source_provided',
    'INSUFFICIENT_EVIDENCE': 'insufficient_evidence',
}
# CONFIGURED_NOT_OBSERVED is intentionally excluded — that's a config
# with no matching observation, so no conversation-level evaluation
# applies (nothing to compare against). Corpus-level surfacing of
# CONFIGURED_NOT_OBSERVED is a BehaviorOS-recommendations concern,
# not a compliance concern.


def _severity_from_delta_pct(delta_pct: float | None) -> str:
    """Map |delta_pct| onto QM's info/warning/critical bucket.

    Cutoffs picked for V1 to keep the pricing-correctness signal
    interpretable without over-inflating "critical" counts:
      < 15%  → info    (matcher already only fires >10%, so info means "just over")
      < 25%  → warning
      >=25%  → critical
    """
    if delta_pct is None:
        return 'warning'
    a = abs(float(delta_pct))
    if a < 0.15:
        return 'info'
    if a < 0.25:
        return 'warning'
    return 'critical'


def _describe_delta(comparison: dict) -> str:
    """Human-readable one-liner for the FAIL rationale."""
    observed = comparison.get('observed_median')
    configured = comparison.get('configured')
    delta_pct = comparison.get('delta_pct')
    sample_n = comparison.get('sample_n')
    if observed is None or configured is None or delta_pct is None:
        return 'observed price differs from configured (details unavailable)'
    direction = 'below' if delta_pct < 0 else 'above'
    return (
        f'observed median ${observed:.2f} is {abs(delta_pct)*100:.1f}% '
        f'{direction} configured ${configured:.2f} '
        f'(n={sample_n})'
    )


def _describe_subject(subject: dict) -> str:
    """Compact human-readable subject descriptor for rationales."""
    parts = []
    if 'bedrooms' in subject and subject.get('bedrooms') is not None:
        parts.append(f'{subject["bedrooms"]}BR')
    if 'bathrooms' in subject and subject.get('bathrooms') is not None:
        parts.append(f'{subject["bathrooms"]}BA')
    if 'service_tier' in subject and subject.get('service_tier'):
        parts.append(str(subject['service_tier']))
    if 'frequency' in subject and subject.get('frequency'):
        parts.append(str(subject['frequency']))
    if 'addons' in subject and subject.get('addons'):
        parts.append(f'addons={subject["addons"]}')
    if not parts:
        parts.append(subject.get('service') or 'cleaning')
    return ' / '.join(parts)


@register
class PricingCorrectnessDimension(BaseDimension):
    name = 'pricing_correctness'
    version = VERSION

    def evaluate(
        self, *,
        reconstruction_run,
        conversation,
    ) -> Iterable[DimensionResult]:
        from apps.conversations.models import (
            ReconstructedBusinessFact as _RBF,
        )
        conv_id = str(conversation.id)

        # All pricing reconstructed facts that name this conversation
        # as evidence. The __contains lookup on Django JSONField uses
        # Postgres @> containment on JSONB arrays.
        facts = list(
            _RBF.objects
            .filter(
                reconstruction_run=reconstruction_run,
                domain='pricing',
                evidence_conversation_ids__contains=[conv_id],
            )
        )

        if not facts:
            # No pricing observation from this conversation was
            # aggregated into any reconstructed fact — pricing
            # correctness is NOT_APPLICABLE for this conversation.
            #
            # Note: a conversation might contribute observations that
            # got trimmed by the aggregator's 20-conversation
            # evidence cap; in that case we correctly miss it here
            # but the corpus-level pattern finding still surfaces the
            # aggregate. This is a known trade-off documented in the
            # V1 GO/NO-GO.
            yield DimensionResult(
                dimension=self.name,
                state=State.NOT_APPLICABLE,
                conversation_id=conv_id,
                reason_code='no_price_quoted_or_evidence_capped',
                rationale_text=(
                    'This conversation did not contribute to any pricing '
                    'reconstructed fact (either no price quoted, or the '
                    'aggregator did not include it in the evidence cap).'
                ),
                evidence=[
                    EvidenceRef(
                        kind='canonical_context',
                        ref=conv_id,
                        description='Per-conversation canonical context',
                    ),
                ],
            )
            return

        for fact in facts:
            verdict = fact.relationship_to_config
            subject = fact.canonical_subject_json or {}
            fact_id = str(fact.id)
            subject_desc = _describe_subject(subject)

            base_evidence = [
                EvidenceRef(
                    kind='canonical_context',
                    ref=conv_id,
                    description='Per-conversation canonical context',
                ),
                EvidenceRef(
                    kind='reconstructed_fact',
                    ref=fact_id,
                    description=(
                        f'{verdict} verdict for {subject_desc} '
                        f'(support_n={fact.support_n})'
                    ),
                ),
            ]

            # Add turn-level pointers when available.
            for turn_ref in (fact.evidence_turn_ids or [])[:5]:
                if isinstance(turn_ref, dict):
                    tref = turn_ref.get('turn_id') or ''
                    tconv = turn_ref.get('conversation_id') or ''
                    if tconv == conv_id and tref:
                        base_evidence.append(EvidenceRef(
                            kind='conversation_turn',
                            ref=tref,
                            description=f'Pricing evidence turn for {subject_desc}',
                        ))

            if verdict in _MATCH_VERDICTS:
                yield DimensionResult(
                    dimension=self.name,
                    state=State.PASS,
                    subject_key=subject,
                    conversation_id=conv_id,
                    reason_code='observed_within_tolerance',
                    rationale_text=(
                        f'Observed pricing for {subject_desc} matches '
                        f'configured within tolerance.'
                    ),
                    evidence=base_evidence,
                    source_reconstructed_fact_id=fact_id,
                )
                continue

            if verdict in _FAIL_VERDICTS:
                obs_val = fact.observed_value_json or {}
                comparison = obs_val.get('price_comparison') or {}
                delta_pct = comparison.get('delta_pct')
                severity = _severity_from_delta_pct(delta_pct)
                direction = (
                    'below' if (delta_pct or 0) < 0 else 'above'
                )
                reason_code = f'observed_{direction}_configured'
                fail_evidence = list(base_evidence)
                fail_evidence.append(EvidenceRef(
                    kind='matcher_output',
                    ref=fact_id,
                    description=(
                        f'observed_median=${comparison.get("observed_median")} '
                        f'configured=${comparison.get("configured")} '
                        f'delta_pct={delta_pct}'
                    ),
                ))
                cfg_id = (fact.configured_equivalent_json or {}).get('id')
                if cfg_id:
                    fail_evidence.append(EvidenceRef(
                        kind='configured_rule',
                        ref=str(cfg_id),
                        description=(
                            f'Configured pricing_table entry for {subject_desc}'
                        ),
                    ))
                yield DimensionResult(
                    dimension=self.name,
                    state=State.FAIL,
                    subject_key=subject,
                    conversation_id=conv_id,
                    severity=severity,
                    reason_code=reason_code,
                    rationale_text=(
                        f'For {subject_desc}: {_describe_delta(comparison)}.'
                    ),
                    evidence=fail_evidence,
                    source_reconstructed_fact_id=fact_id,
                )
                continue

            unknown_reason = _UNKNOWN_MAP.get(
                verdict, 'unrecognized_verdict',
            )
            yield DimensionResult(
                dimension=self.name,
                state=State.UNKNOWN_NOT_EVALUABLE,
                subject_key=subject,
                conversation_id=conv_id,
                reason_code=unknown_reason,
                rationale_text=(
                    f'For {subject_desc}: verdict={verdict}, insufficient '
                    f'basis for a pass/fail judgment.'
                ),
                evidence=base_evidence,
                source_reconstructed_fact_id=fact_id,
            )

    def evaluate_corpus(
        self, *, reconstruction_run,
    ) -> Iterable[DimensionResult]:
        """One corpus-level FAIL finding per DIFFERS_FROM_CONFIG pattern
        (e.g. "oven addon $30 vs $40 across 32 conversations"). These
        surface in the tenant findings list as a single actionable
        item per pattern, not once per contributing conversation.
        """
        from apps.conversations.models import (
            ReconstructedBusinessFact as _RBF,
        )
        differs = _RBF.objects.filter(
            reconstruction_run=reconstruction_run,
            domain='pricing',
            relationship_to_config='DIFFERS_FROM_CONFIG',
        )
        for fact in differs:
            subject = fact.canonical_subject_json or {}
            subject_desc = _describe_subject(subject)
            fact_id = str(fact.id)
            obs_val = fact.observed_value_json or {}
            comparison = obs_val.get('price_comparison') or {}
            delta_pct = comparison.get('delta_pct')
            severity = _severity_from_delta_pct(delta_pct)
            direction = 'below' if (delta_pct or 0) < 0 else 'above'
            reason_code = f'observed_{direction}_configured'

            supporting_convs = fact.evidence_conversation_ids or []
            evidence = [
                EvidenceRef(
                    kind='reconstructed_fact',
                    ref=fact_id,
                    description=(
                        f'DIFFERS pattern for {subject_desc} '
                        f'(support_n={fact.support_n}, '
                        f'evidence_conversation_ids={len(supporting_convs)})'
                    ),
                ),
                EvidenceRef(
                    kind='matcher_output',
                    ref=fact_id,
                    description=(
                        f'observed_median=${comparison.get("observed_median")} '
                        f'configured=${comparison.get("configured")} '
                        f'delta_pct={delta_pct} n={comparison.get("sample_n")}'
                    ),
                ),
            ]
            cfg_id = (fact.configured_equivalent_json or {}).get('id')
            if cfg_id:
                evidence.append(EvidenceRef(
                    kind='configured_rule',
                    ref=str(cfg_id),
                    description=(
                        f'Configured pricing_table entry for {subject_desc}'
                    ),
                ))
            # Attach up to first 10 supporting conversation refs so a UI
            # can list "affected conversations" without paging.
            for cid in supporting_convs[:10]:
                evidence.append(EvidenceRef(
                    kind='canonical_context',
                    ref=str(cid),
                    description='Contributing conversation',
                ))
            yield DimensionResult(
                dimension=self.name,
                state=State.FAIL,
                subject_key=subject,
                conversation_id=None,     # corpus-level
                severity=severity,
                reason_code=reason_code,
                rationale_text=(
                    f'PATTERN: {subject_desc}: {_describe_delta(comparison)}.'
                ),
                evidence=evidence,
                source_reconstructed_fact_id=fact_id,
            )
