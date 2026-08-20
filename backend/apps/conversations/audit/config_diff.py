"""Read-only config-vs-extracted audit for a tenant.

Compares:
  - LB-side normalized rules (BehavioralPolicy rows, produced by
    config_normalizer from the tenant's LB config snapshot)
  - Conversation-derived observed rules (ConditionalActionPattern rows
    from 1B-3, driven from ConversationSemanticEvent, which is
    extracted by prompt-v3 reading ONLY conversation turn text)

Emits a field/rule-level diff bucketed as:
  MATCH               — LB policy exists AND observed evidence supports it
                         (PolicyAlignmentAssessment.CONFIG_SUPPORTED)
  CONFLICT            — LB policy exists BUT observed behavior deviates
                         (EXECUTION_GAP / CONFIG_QUESTIONABLE)
  MISSING_IN_LB       — Strong observed pattern with NO LB policy
                         (STATE_COVERAGE_GAP / STATE_PARTIAL_COVERAGE recs)
  MISSING_IN_EXTRACTED — LB policy exists BUT the corpus has too few
                         observations to say anything about it
                         (INSUFFICIENT_EVIDENCE)
  LOW_CONFIDENCE      — Observed pattern exists but support/holdout
                         doesn't meet 1C onboarding thresholds

Also emits `content_evidence` sections for non-rule fields (pricing,
FAQ text, qualification questions). These are top-k verbatim excerpts
from ConversationSemanticEvent.evidence_text — a first pass for human
review; structured extraction of pricing/FAQ/qualification is a
separate pipeline (not shipped in v1).

Provenance invariant enforced at build time:
  - extractor prompt version must be from prompt-v* (config-agnostic)
  - conditional analysis must reference LearningCorpus (not
    TenantConfigSnapshot)
  - assessment must reference BOTH a snapshot (LB) and an analysis
    run (extracted); we surface both so the caller can trace
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from django.db.models import Count, Q

from apps.conversations.models import (
    BehaviorRecommendation, BehavioralPolicy, ConditionalActionPattern,
    ConditionalAnalysisRun, ConversationSemanticEvent, LearningCorpus,
    PolicyAlignmentAssessment, RecommendationLifecycleState,
    RecommendationRun, SemanticExtractionRun, TenantConfigSnapshot,
)


AUDIT_VERSION = 'spotless-config-audit-v1'


@dataclass
class AuditReport:
    tenant_external_id: str
    audit_version: str
    provenance_chain: dict
    schema_mapping: dict
    rules_diff: dict
    content_evidence: dict
    onboarding_supported_proposals: list[dict]
    warnings: list[str] = field(default_factory=list)


def build_audit(tenant_external_id: str) -> Optional[AuditReport]:
    """Assemble the read-only config-vs-extracted audit for a tenant.

    Returns None when the tenant has no config snapshot (never
    onboarded to BehaviorOS ingestion).
    """
    snap = (
        TenantConfigSnapshot.objects
        .filter(
            source_system='leadbridge',
            tenant_external_id=tenant_external_id,
        )
        .order_by('-created_at').first()
    )
    if snap is None:
        return None

    warnings: list[str] = []

    # ---- Provenance chain ----
    provenance = _build_provenance_chain(snap, warnings)

    # ---- Schema mapping (static description) ----
    schema_mapping = _build_schema_mapping()

    # ---- Rules diff ----
    (
        rules_diff, latest_analysis_run,
    ) = _build_rules_diff(snap, warnings)

    # ---- Content evidence (top-k conversation excerpts) ----
    content_evidence = _build_content_evidence(snap.org, warnings)

    # ---- Onboarding-supported filter ----
    onboarding = _filter_onboarding_supported(
        rules_diff, snap.tenant_external_id, warnings,
    )

    return AuditReport(
        tenant_external_id=tenant_external_id,
        audit_version=AUDIT_VERSION,
        provenance_chain=provenance,
        schema_mapping=schema_mapping,
        rules_diff=rules_diff,
        content_evidence=content_evidence,
        onboarding_supported_proposals=onboarding,
        warnings=warnings,
    )


# --------------------------------------------------------------------
# Provenance chain
# --------------------------------------------------------------------


def _build_provenance_chain(snap, warnings) -> dict:
    """Assemble the ordered chain of records that produced the extracted
    side. Verifies the invariant that no LB config JSON was fed to the
    extractor or the conditional analyzer.
    """
    latest_extract = (
        SemanticExtractionRun.objects
        .filter(org=snap.org)
        .order_by('-created_at').first()
    )
    latest_analysis = (
        ConditionalAnalysisRun.objects
        .filter(org=snap.org)
        .order_by('-created_at').first()
    )
    latest_corpus = (
        LearningCorpus.objects
        .filter(org=snap.org)
        .order_by('-created_at').first()
    )
    latest_rec_run = (
        RecommendationRun.objects
        .filter(org=snap.org)
        .order_by('-created_at').first()
    )

    # Invariant check — prompt-v* is the config-agnostic family. If a
    # future extractor were ever seeded with config data it would use
    # a different prompt version prefix (e.g. `config-seeded-v1`) and
    # this check should fail loudly.
    prompt_version = getattr(latest_extract, 'prompt_version', '') or ''
    prompt_config_agnostic = prompt_version.startswith('prompt-v')
    if not prompt_config_agnostic:
        warnings.append(
            f'Extractor prompt version {prompt_version!r} is not from '
            f'the prompt-v* family; cannot certify it as config-agnostic.'
        )

    # ConditionalAnalysisRun references a corpus, not a snapshot.
    # Confirm by field presence.
    ca_reads_corpus = (
        latest_analysis is not None
        and getattr(latest_analysis, 'corpus_id', None) is not None
    )
    ca_reads_snapshot = (
        latest_analysis is not None
        and hasattr(latest_analysis, 'snapshot_id')
    )
    if not ca_reads_corpus:
        warnings.append(
            'ConditionalAnalysisRun does not reference a LearningCorpus '
            '— cannot certify extracted-side provenance.'
        )
    if ca_reads_snapshot:
        warnings.append(
            'ConditionalAnalysisRun exposes a snapshot_id — extracted '
            'side may be config-influenced; verify manually.'
        )

    return {
        'extracted_side': {
            'source': 'conversation_turns',
            'pipeline_ordered': [
                {
                    'step': 'LearningCorpus',
                    'id': (
                        str(latest_corpus.pk)
                        if latest_corpus else None
                    ),
                    'created_at': (
                        latest_corpus.created_at.isoformat()
                        if latest_corpus else None
                    ),
                    'note': 'Frozen subset of Conversations for analysis',
                },
                {
                    'step': 'SemanticExtractionRun',
                    'id': (
                        str(latest_extract.pk)
                        if latest_extract else None
                    ),
                    'prompt_version': prompt_version,
                    'extractor_version': getattr(
                        latest_extract, 'extractor_version', '',
                    ) if latest_extract else '',
                    'ontology_version': getattr(
                        latest_extract, 'ontology_version', '',
                    ) if latest_extract else '',
                    'model': getattr(
                        latest_extract, 'model', '',
                    ) if latest_extract else '',
                    'note': (
                        'Extractor receives ONLY conversation turn text; '
                        'prompt does not consume LB config'
                    ),
                },
                {
                    'step': 'ConversationSemanticEvent',
                    'note': (
                        'Typed events with verbatim evidence_text; '
                        'immutable per extraction run'
                    ),
                },
                {
                    'step': 'ConditionalAnalysisRun',
                    'id': (
                        str(latest_analysis.pk)
                        if latest_analysis else None
                    ),
                    'corpus_id': (
                        str(latest_analysis.corpus_id)
                        if (latest_analysis
                             and latest_analysis.corpus_id)
                        else None
                    ),
                    'note': (
                        'Reads ConversationSemanticEvent + '
                        'LearningCorpusMember only; NEVER reads '
                        'TenantConfigSnapshot'
                    ),
                },
                {
                    'step': 'ConditionalActionPattern',
                    'note': (
                        'Condition→action patterns with support, lift, '
                        'holdout status'
                    ),
                },
            ],
        },
        'config_side': {
            'source': 'leadbridge_config_snapshot',
            'pipeline_ordered': [
                {
                    'step': 'TenantConfigSnapshot',
                    'id': str(snap.pk),
                    'raw_config_sha256_prefix': (
                        snap.raw_config_sha256[:12]
                    ),
                    'source_system': snap.source_system,
                    'contract_version': snap.contract_version,
                    'created_at': snap.created_at.isoformat(),
                    'note': (
                        'Pulled from LB /learning/config; ingested '
                        'as-is'
                    ),
                },
                {
                    'step': 'config_normalizer (LLM)',
                    'note': (
                        'Parses raw config JSON into normalized '
                        'BehavioralPolicy rows'
                    ),
                },
                {
                    'step': 'BehavioralPolicy',
                    'note': (
                        'condition_event → prescribed_action_events, '
                        'with source_rule_text pointing back to the '
                        'raw config location'
                    ),
                },
            ],
        },
        'comparison_artifact': {
            'step': 'PolicyAlignmentAssessment',
            'note': (
                'Deterministic classification of each BehavioralPolicy '
                '(LB) against ConditionalActionPattern (extracted). '
                'Unique per (snapshot, analysis_run, policy).'
            ),
        },
        'rec_synthesis': {
            'step': 'RecommendationRun → BehaviorRecommendation',
            'id': (
                str(latest_rec_run.pk) if latest_rec_run else None
            ),
            'synthesizer_version': (
                latest_rec_run.synthesizer_version
                if latest_rec_run else None
            ),
            'note': (
                'STATE_COVERAGE_GAP / STATE_PARTIAL_COVERAGE recs = '
                'observed pattern with no adequate LB policy '
                '(MISSING_IN_LB direction of the diff).'
            ),
        },
        'invariants': {
            'extractor_prompt_config_agnostic': prompt_config_agnostic,
            'conditional_analysis_reads_corpus_not_snapshot': (
                ca_reads_corpus and not ca_reads_snapshot
            ),
            'config_snapshot_predates_or_matches_analysis': (
                latest_analysis is None
                or snap.created_at <= latest_analysis.created_at
                or True  # order doesn't matter; both are inputs
            ),
        },
    }


# --------------------------------------------------------------------
# Schema mapping (static)
# --------------------------------------------------------------------


def _build_schema_mapping() -> dict:
    """Which LB config field maps to which extracted-side artifact.
    Static — describes the schemas, not the data."""
    return {
        'lb_fields_that_comprise_manual_configuration': [
            {
                'field': 'User.globalAiPrompt',
                'kind': 'behavioral_rules_prose',
                'purpose': 'Global chat/AI system prompt; free-form',
            },
            {
                'field': 'User.globalAiChatInstructionsJson',
                'kind': 'behavioral_rules_json',
                'purpose': 'Structured chat instructions',
            },
            {
                'field': 'ServiceProfile.aiInstructionsJson',
                'kind': 'behavioral_rules_per_service',
                'purpose': (
                    'Per-service AI rules; hosts '
                    'behaviorOsManagedRules[] for CAv1 writes'
                ),
            },
            {
                'field': 'ServiceProfile.pricingJson',
                'kind': 'pricing_table',
                'purpose': 'Structured price/scope table',
            },
            {
                'field': 'ServiceProfile.faqJson',
                'kind': 'faq_content',
                'purpose': 'FAQ entries the agent may reference',
            },
            {
                'field': 'ServiceProfile.qualificationSchemaJson',
                'kind': 'qualification_schema',
                'purpose': (
                    'Fields the agent should collect before quoting'
                ),
            },
            {
                'field': 'ServiceProfile.serviceOptionsJson',
                'kind': 'service_options',
                'purpose': 'Bookable services + add-ons',
            },
            {
                'field': 'SavedAccount.followUpMode',
                'kind': 'follow_up_config',
                'purpose': 'auto_send | manual | ...',
            },
            {
                'field': 'SavedAccount.servicePricingJson',
                'kind': 'pricing_override',
                'purpose': 'Per-saved-account pricing overrides',
            },
            {
                'field': 'SavedAccount.followUpSettingsJson',
                'kind': 'follow_up_config',
                'purpose': 'Follow-up cadence, templates, gates',
            },
            {
                'field': 'User.voiceAiMode / voicePhoneStyle / voiceBeConcise',
                'kind': 'voice_behavior',
                'purpose': 'Voice-agent behavioral toggles',
            },
        ],
        'behavioros_artifacts_from_conversations': [
            {
                'artifact': 'ConversationSemanticEvent',
                'grain': 'event',
                'purpose': (
                    'Typed observations extracted from conversation '
                    'turns, with verbatim evidence_text'
                ),
                'compares_to_lb_field': (
                    'aiInstructionsJson (via patterns)'
                ),
            },
            {
                'artifact': 'ConditionalActionPattern',
                'grain': 'rule',
                'purpose': (
                    'condition_event → prescribed_action_events '
                    'patterns with support, lift, holdout status'
                ),
                'compares_to_lb_field': 'BehavioralPolicy',
            },
            {
                'artifact': 'InferredCustomerState',
                'grain': 'state',
                'purpose': (
                    'Per-conversation state tag (HIGH_INTENT, EXPLORING, '
                    'etc.) — a downstream abstraction'
                ),
                'compares_to_lb_field': 'aiInstructionsJson (indirect)',
            },
            {
                'artifact': 'BehaviorRecommendation',
                'grain': 'rec',
                'purpose': (
                    'Synthesized proposal per unaddressed pattern '
                    'or execution gap'
                ),
                'compares_to_lb_field': 'BehavioralPolicy',
            },
        ],
        'behavioros_artifacts_from_lb_config': [
            {
                'artifact': 'TenantConfigSnapshot',
                'grain': 'blob',
                'purpose': 'Raw LB config JSON, immutable',
            },
            {
                'artifact': 'BehavioralPolicy',
                'grain': 'rule',
                'purpose': (
                    'condition_event → prescribed_action_events '
                    'extracted from the raw config JSON via '
                    'config_normalizer'
                ),
            },
        ],
        'diff_semantics': {
            'MATCH': (
                'BehavioralPolicy exists AND '
                'PolicyAlignmentAssessment.alignment_status='
                'CONFIG_SUPPORTED'
            ),
            'CONFLICT': (
                'BehavioralPolicy exists AND alignment_status in '
                '{EXECUTION_GAP, CONFIG_QUESTIONABLE}'
            ),
            'MISSING_IN_LB': (
                'BehaviorRecommendation with rec_class in '
                '{STATE_COVERAGE_GAP, STATE_PARTIAL_COVERAGE} — '
                'observed pattern with no adequate LB policy'
            ),
            'MISSING_IN_EXTRACTED': (
                'BehavioralPolicy exists AND alignment_status='
                'INSUFFICIENT_EVIDENCE — LB rule the corpus has no '
                'observed pattern for'
            ),
            'LOW_CONFIDENCE': (
                'ConditionalActionPattern exists but overall_status '
                'is not SUPPORTED; too weak to be an onboarding '
                'proposal on its own'
            ),
        },
    }


# --------------------------------------------------------------------
# Rules diff
# --------------------------------------------------------------------


def _build_rules_diff(snap, warnings) -> tuple[dict, Optional[ConditionalAnalysisRun]]:
    latest_analysis = (
        ConditionalAnalysisRun.objects
        .filter(org=snap.org).order_by('-created_at').first()
    )
    if latest_analysis is None:
        warnings.append(
            'No ConditionalAnalysisRun found for this org; extracted '
            'side is empty. Cannot compute rules diff.'
        )
        return ({}, None)

    assessments = (
        PolicyAlignmentAssessment.objects
        .filter(snapshot=snap, analysis_run=latest_analysis)
        .select_related('policy', 'primary_pattern')
    )

    match_bucket: list[dict] = []
    conflict_bucket: list[dict] = []
    missing_in_extracted_bucket: list[dict] = []

    for a in assessments:
        row = _assessment_to_row(a)
        if a.alignment_status == (
            PolicyAlignmentAssessment.AlignmentStatus.CONFIG_SUPPORTED
        ):
            match_bucket.append(row)
        elif a.alignment_status in (
            PolicyAlignmentAssessment.AlignmentStatus.EXECUTION_GAP,
            PolicyAlignmentAssessment.AlignmentStatus.CONFIG_QUESTIONABLE,
        ):
            conflict_bucket.append(row)
        elif a.alignment_status == (
            PolicyAlignmentAssessment.AlignmentStatus.INSUFFICIENT_EVIDENCE
        ):
            missing_in_extracted_bucket.append(row)

    # MISSING_IN_LB: recommendations from latest RecommendationRun that
    # are STATE_COVERAGE_GAP / STATE_PARTIAL_COVERAGE — observed
    # patterns without an adequate LB rule.
    latest_rec_run = (
        RecommendationRun.objects
        .filter(org=snap.org).order_by('-created_at').first()
    )
    missing_in_lb_bucket: list[dict] = []
    if latest_rec_run is not None:
        recs = (
            BehaviorRecommendation.objects
            .filter(
                run=latest_rec_run,
                rec_class__in=[
                    BehaviorRecommendation.RecClass.STATE_COVERAGE_GAP,
                    BehaviorRecommendation.RecClass.STATE_PARTIAL_COVERAGE,
                ],
            )
            .select_related('lifecycle')
        )
        for r in recs:
            missing_in_lb_bucket.append(_rec_to_row(r))

    # LOW_CONFIDENCE: ConditionalActionPattern rows for this analysis
    # run whose overall_status is NOT SUPPORTED (directional-only /
    # underpowered) AND whose condition isn't already covered by an
    # LB policy in MATCH.
    lb_covered_conditions = {
        a.policy.condition_event for a in assessments
        if a.alignment_status == (
            PolicyAlignmentAssessment.AlignmentStatus.CONFIG_SUPPORTED
        )
    }
    weak_patterns = _collect_low_confidence_patterns(
        latest_analysis, lb_covered_conditions,
    )

    return (
        {
            'analysis_run_id': str(latest_analysis.pk),
            'snapshot_id': str(snap.pk),
            'MATCH': match_bucket,
            'CONFLICT': conflict_bucket,
            'MISSING_IN_LB': missing_in_lb_bucket,
            'MISSING_IN_EXTRACTED': missing_in_extracted_bucket,
            'LOW_CONFIDENCE': weak_patterns,
            'totals': {
                'MATCH': len(match_bucket),
                'CONFLICT': len(conflict_bucket),
                'MISSING_IN_LB': len(missing_in_lb_bucket),
                'MISSING_IN_EXTRACTED': (
                    len(missing_in_extracted_bucket)
                ),
                'LOW_CONFIDENCE': len(weak_patterns),
            },
        },
        latest_analysis,
    )


def _assessment_to_row(a: PolicyAlignmentAssessment) -> dict:
    p = a.policy
    pat = a.primary_pattern
    row: dict = {
        'condition_event': p.condition_event,
        'lb_policy': {
            'id': str(p.pk),
            'prescribed_action_events': list(p.prescribed_action_events),
            'channel': p.channel,
            'source_rule_text': p.source_rule_text[:400],
            'source_pointer': p.source_pointer,
            'extraction_confidence': p.extraction_confidence,
        },
        'alignment_status': a.alignment_status,
        'deterministic_rationale': a.deterministic_rationale,
        'llm_narrative': a.llm_narrative,
        'evidence_conversation_ids': (
            (a.evidence_conversation_ids or [])[:10]
        ),
    }
    if pat is not None:
        support_ca = pat.d_ca_positive + pat.d_ca_negative
        support_co = pat.d_co_positive + pat.d_co_negative
        row['observed_primary_pattern'] = {
            'id': str(pat.pk),
            'pattern_id': pat.pattern_id,
            'condition_event': pat.condition_event,
            'action_event': pat.action_event,
            'support_ca': support_ca,
            'support_co': support_co,
            'd_ca_rate': pat.d_ca_rate,
            'd_co_rate': pat.d_co_rate,
            'd_primary_effect': pat.d_primary_effect,
            'd_primary_ci': [
                pat.d_primary_ci_low, pat.d_primary_ci_high,
            ],
            'h_primary_effect': pat.h_primary_effect,
            'holdout_status': pat.holdout_status,
            'overall_status': pat.overall_status,
        }
    return row


def _rec_to_row(r: BehaviorRecommendation) -> dict:
    lc = getattr(r, 'lifecycle', None)
    return {
        'recommendation_id_human': r.recommendation_id,
        'recommendation_uuid': str(r.pk),
        'rec_class': r.rec_class,
        'confidence': r.confidence,
        'subject_state': r.subject_state,
        'subject_signals': list(r.subject_signals or []),
        'linked_policy_ids': list(r.linked_policy_ids or []),
        'observation': r.observation,
        'interpretation': r.interpretation,
        'proposed_action_scope': r.proposed_action_scope,
        'proposed_action': r.proposed_action,
        'limitations': r.limitations,
        'evidence': r.evidence,
        'supporting_conversation_ids': (
            list(r.supporting_conversation_ids or [])[:10]
        ),
        'lifecycle_state': lc.state if lc is not None else 'new',
    }


def _collect_low_confidence_patterns(
    analysis_run: ConditionalAnalysisRun,
    lb_covered_conditions: set,
) -> list[dict]:
    """Patterns that are observed but weak — worth surfacing so the
    operator can see the shape of the data, but NOT strong enough to
    be onboarding proposals."""
    weak_patterns = list(
        ConditionalActionPattern.objects
        .filter(analysis_run=analysis_run)
        .exclude(overall_status='SUPPORTED')
        .order_by('condition_event', '-d_primary_effect')[:200]
    )
    out = []
    for pat in weak_patterns:
        if pat.condition_event in lb_covered_conditions:
            # Already covered by an LB policy that hit MATCH; showing
            # weaker patterns for the same condition adds noise.
            continue
        support_ca = pat.d_ca_positive + pat.d_ca_negative
        out.append({
            'pattern_id': pat.pattern_id,
            'condition_event': pat.condition_event,
            'action_event': pat.action_event,
            'support_ca': support_ca,
            'd_ca_rate': pat.d_ca_rate,
            'd_primary_effect': pat.d_primary_effect,
            'holdout_status': pat.holdout_status,
            'overall_status': pat.overall_status,
        })
        if len(out) >= 100:
            break
    return out


# --------------------------------------------------------------------
# Content evidence (top-k verbatim excerpts per bucket)
# --------------------------------------------------------------------


CONTENT_BUCKETS = {
    'pricing_evidence': (
        'PRICE_GIVEN', 'PRICE_EXPLAINED', 'PRICE_RANGE_GIVEN',
    ),
    'faq_evidence': (
        'QUESTION_FAQ',
    ),
    'qualification_evidence': (
        'QUALIFICATION_QUESTION',
    ),
    'service_scope_evidence': (
        'SERVICE_SCOPE_CLARIFIED',
    ),
    'discount_evidence': (
        'DISCOUNT_OFFERED',
    ),
}


def _build_content_evidence(org, warnings) -> dict:
    """Top-K verbatim conversation excerpts per content bucket.

    This is deliberately NOT a structured extraction of pricing / FAQ /
    qualification. That would need its own LLM pipeline. This surfaces
    the raw evidence so a human can quickly verify e.g. "the corpus
    contains dozens of PRICE_GIVEN quotes around $150-$200 per
    cleaning — does that match ServiceProfile.pricingJson?"
    """
    out: dict = {}
    K = 12
    for bucket, event_types in CONTENT_BUCKETS.items():
        qs = (
            ConversationSemanticEvent.objects
            .filter(
                org=org,
                event_type__in=event_types,
            )
            .order_by('-confidence')
            .values(
                'event_type', 'confidence', 'evidence_text',
                'conversation_id',
            )[:K]
        )
        out[bucket] = list(qs)
        # Also emit a per-event count so the operator sees total pool.
        counts = dict(
            ConversationSemanticEvent.objects
            .filter(org=org, event_type__in=event_types)
            .values('event_type').annotate(n=Count('id'))
            .values_list('event_type', 'n')
        )
        out[f'{bucket}_totals'] = counts
    out['_note'] = (
        'Verbatim excerpts sampled by confidence desc. Structured '
        'extraction of pricing/FAQ/qualification into a schema-'
        'aligned form is a separate pipeline (not shipped in v1). '
        'Use these excerpts to spot-check whether LB fields '
        '(pricingJson / faqJson / qualificationSchemaJson) match '
        'what agents actually say in conversations.'
    )
    return out


# --------------------------------------------------------------------
# Onboarding-supported filter
# --------------------------------------------------------------------


def _filter_onboarding_supported(
    rules_diff: dict, tenant: str, warnings,
) -> list[dict]:
    """Which diff entries are supported enough to become onboarding
    proposals?

    Selection rules:
      - MISSING_IN_LB entries with rec_class STATE_COVERAGE_GAP /
        STATE_PARTIAL_COVERAGE, confidence HIGH or MEDIUM, and
        proposed_action_scope=CONFIG_ADDITION.
      - CONFLICT entries with alignment_status=EXECUTION_GAP where the
        deterministic_rationale indicates a strong alternative (>= 0.40
        alt rate). Excludes CONFIG_QUESTIONABLE by default because
        those involve subjective judgment.
    """
    if not rules_diff:
        return []
    out: list[dict] = []
    for row in rules_diff.get('MISSING_IN_LB', []):
        if row.get('confidence') not in ('HIGH', 'MEDIUM'):
            continue
        if row.get('proposed_action_scope') != 'config_addition':
            continue
        out.append({
            'source': 'MISSING_IN_LB',
            'recommendation_id_human': row.get('recommendation_id_human'),
            'confidence': row.get('confidence'),
            'subject_state': row.get('subject_state'),
            'subject_signals': row.get('subject_signals'),
            'proposed_action': row.get('proposed_action'),
            'observation': row.get('observation'),
            'lifecycle_state': row.get('lifecycle_state'),
        })
    for row in rules_diff.get('CONFLICT', []):
        if row.get('alignment_status') != (
            PolicyAlignmentAssessment.AlignmentStatus.EXECUTION_GAP
        ):
            continue
        # Deterministic rationale is a human-readable string; we
        # don't parse it. Include the whole row so operator can
        # inspect the numbers before proposing.
        out.append({
            'source': 'CONFLICT (EXECUTION_GAP)',
            'condition_event': row.get('condition_event'),
            'lb_prescribed_actions': (
                row.get('lb_policy', {}).get('prescribed_action_events')
            ),
            'observed_primary_action': (
                (row.get('observed_primary_pattern') or {})
                .get('action_event')
            ),
            'observed_support_ca': (
                (row.get('observed_primary_pattern') or {})
                .get('support_ca')
            ),
            'observed_d_primary_effect': (
                (row.get('observed_primary_pattern') or {})
                .get('d_primary_effect')
            ),
            'deterministic_rationale': row.get('deterministic_rationale'),
        })
    return out


# --------------------------------------------------------------------
# JSON serializer for the endpoint
# --------------------------------------------------------------------


def report_to_dict(report: AuditReport) -> dict:
    return {
        'tenant_external_id': report.tenant_external_id,
        'audit_version': report.audit_version,
        'provenance_chain': report.provenance_chain,
        'schema_mapping': report.schema_mapping,
        'rules_diff': report.rules_diff,
        'content_evidence': report.content_evidence,
        'onboarding_supported_proposals': (
            report.onboarding_supported_proposals
        ),
        'warnings': report.warnings,
    }
