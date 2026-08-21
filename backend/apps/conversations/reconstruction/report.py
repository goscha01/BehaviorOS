"""Product-style report over the latest UnifiedBusinessReconstructionRun.

Not a raw dump of ReconstructedBusinessFact rows — a structured shape
designed to answer:

  Here is how <tenant> appears to operate based on N real conversations.
  Here is what LeadBridge currently says the business does.
  Here are the differences.
  Here are the areas where the business itself behaves inconsistently.
  Here is what we're confident about.
  Here is what we cannot infer safely.

Ends with an onboarding candidate set bucketed as SAFE_TO_PROPOSE /
NEEDS_OWNER_CONFIRMATION / DO_NOT_PROPOSE — the classification is
the real gate for whether 1D succeeds as an onboarding primitive.
"""

from __future__ import annotations

from typing import Optional

from apps.conversations.models import (
    ReconstructedBusinessFact, UnifiedBusinessReconstructionRun,
)


def build_report(*, tenant_external_id: str) -> Optional[dict]:
    run = (
        UnifiedBusinessReconstructionRun.objects
        .filter(
            tenant_external_id=tenant_external_id,
            status='completed',
        ).order_by('-created_at').first()
    )
    if run is None:
        return None
    facts = list(
        ReconstructedBusinessFact.objects.filter(
            reconstruction_run=run,
        )
    )
    by_domain: dict[str, list[ReconstructedBusinessFact]] = {}
    for f in facts:
        by_domain.setdefault(f.domain, []).append(f)

    corpus_size = _corpus_size_hint(facts)

    return {
        'reconstruction_run_id': str(run.id),
        'tenant_external_id': run.tenant_external_id,
        'snapshot_id': str(run.snapshot_id),
        'reconstruction_version': run.reconstruction_version,
        'completed_at': (
            run.completed_at.isoformat() if run.completed_at else None
        ),
        'headline': _build_headline(run, facts, corpus_size),
        'pricing': _section_for_domain(
            by_domain.get('pricing', []), 'pricing',
        ),
        'qualification': _section_for_domain(
            by_domain.get('qualification', []), 'qualification',
        ),
        'faq': _section_for_domain(
            by_domain.get('faq', []), 'faq',
        ),
        'service_scope': _section_for_domain(
            by_domain.get('service_scope', []), 'service_scope',
        ),
        'onboarding_candidate_set': _onboarding_set(facts),
        'summary_counts': {
            'total_reconstructed_facts': len(facts),
            'by_domain': {
                d: len(v) for d, v in by_domain.items()
            },
            'by_relationship_to_config': _counts(
                facts, 'relationship_to_config',
            ),
            'by_onboarding_class': _counts(facts, 'onboarding_class'),
            'by_consistency': _counts(facts, 'consistency'),
        },
    }


def _corpus_size_hint(facts) -> Optional[int]:
    """Peek at a fact's observed_value.eligible_conversations if any."""
    for f in facts:
        v = f.observed_value_json or {}
        n = v.get('eligible_conversations')
        if n is not None:
            return int(n)
        stats = v.get('amount_stats') or {}
        n2 = stats.get('support_n')
        if n2 is not None and n2 > 0:
            # Not exactly corpus size but a proxy
            continue
    return None


def _build_headline(run, facts, corpus_size) -> str:
    total = len(facts)
    safe = sum(
        1 for f in facts
        if f.onboarding_class == 'SAFE_TO_PROPOSE'
    )
    contradictory = sum(
        1 for f in facts
        if f.consistency == 'contradictory'
    )
    confirmed = sum(
        1 for f in facts
        if f.relationship_to_config == 'CONFIRMED_BY_BEHAVIOR'
    )
    gap = sum(
        1 for f in facts
        if f.relationship_to_config == 'OBSERVED_NOT_CONFIGURED'
    )
    corpus_hint = (
        f' from a corpus of ~{corpus_size} conversations'
        if corpus_size else ''
    )
    return (
        f'Reconstructed {total} business facts{corpus_hint} across 4 domains. '
        f'{safe} are ready to propose as-is; '
        f'{contradictory} show contradictory agent behavior that needs '
        f'owner reconciliation; '
        f'{confirmed} are already confirmed by both configuration and '
        f'behavior; '
        f'{gap} are undocumented rules agents follow that the config '
        f'does not describe.'
    )


def _section_for_domain(rows, domain: str) -> dict:
    """Group rows for one domain into product-style sub-lists."""
    if not rows:
        return {'note': f'no reconstruction data for {domain}'}

    def _summary(f: ReconstructedBusinessFact) -> dict:
        return {
            'canonical_subject': f.canonical_subject_json,
            'support_n': f.support_n,
            'relationship_to_config': f.relationship_to_config,
            'consistency': f.consistency,
            'onboarding_class': f.onboarding_class,
            'rationale': f.onboarding_rationale,
            'observed_value': _abbreviate(f.observed_value_json),
            'configured_equivalent': (
                _abbreviate(f.configured_equivalent_json)
                if f.configured_equivalent_json else None
            ),
            'quality_flags': f.quality_flags,
            'evidence_conversation_ids': (
                (f.evidence_conversation_ids or [])[:5]
            ),
        }

    return {
        'stable_observed_rules': [
            _summary(f) for f in rows
            if f.relationship_to_config in (
                'CONFIRMED_BY_BEHAVIOR', 'OBSERVED_NOT_CONFIGURED',
            ) and f.consistency == 'consistent'
        ],
        'contradictory_rules': [
            _summary(f) for f in rows
            if f.consistency == 'contradictory'
        ],
        'context_dependent': [
            _summary(f) for f in rows
            if f.relationship_to_config == 'CONTEXT_DEPENDENT'
            or f.consistency == 'context_dependent'
        ],
        'likely_lead_source_provided': [
            _summary(f) for f in rows
            if f.relationship_to_config == 'LIKELY_LEAD_SOURCE_PROVIDED'
        ],
        'configured_not_observed': [
            _summary(f) for f in rows
            if f.relationship_to_config == 'CONFIGURED_NOT_OBSERVED'
        ],
        'extraction_or_ontology_issue': [
            _summary(f) for f in rows
            if f.relationship_to_config == 'ONTOLOGY_OR_EXTRACTION_ISSUE'
        ],
        'insufficient_evidence_count': sum(
            1 for f in rows
            if f.relationship_to_config == 'INSUFFICIENT_EVIDENCE'
        ),
    }


def _onboarding_set(facts) -> dict:
    def _pack(f):
        return {
            'domain': f.domain,
            'canonical_subject': f.canonical_subject_json,
            'support_n': f.support_n,
            'relationship_to_config': f.relationship_to_config,
            'consistency': f.consistency,
            'rationale': f.onboarding_rationale,
            'observed_value': _abbreviate(f.observed_value_json),
            'configured_equivalent': (
                _abbreviate(f.configured_equivalent_json)
                if f.configured_equivalent_json else None
            ),
            'quality_flags': f.quality_flags,
        }
    safe = [_pack(f) for f in facts if f.onboarding_class == 'SAFE_TO_PROPOSE']
    needs = [_pack(f) for f in facts if f.onboarding_class == 'NEEDS_OWNER_CONFIRMATION']
    do_not = [_pack(f) for f in facts if f.onboarding_class == 'DO_NOT_PROPOSE']
    # Sort each by support desc so the most important items surface first.
    safe.sort(key=lambda x: -x['support_n'])
    needs.sort(key=lambda x: -x['support_n'])
    do_not.sort(key=lambda x: -x['support_n'])
    return {
        'SAFE_TO_PROPOSE': safe,
        'NEEDS_OWNER_CONFIRMATION': needs,
        'DO_NOT_PROPOSE': do_not,
    }


def _counts(facts, attr: str) -> dict:
    out: dict = {}
    for f in facts:
        v = getattr(f, attr)
        out[v] = out.get(v, 0) + 1
    return out


def _abbreviate(value: dict) -> dict:
    """Trim large value payloads for the report — sample verbatim
    quotes are trimmed to length; keep structural keys."""
    if not isinstance(value, dict):
        return value
    out = {}
    for k, v in value.items():
        if k in ('sample_quotes', 'sample_variants',
                  'sample_phrasings', 'sample_agent_answers',
                  'observed_variants', 'quotes_sample'):
            trimmed = []
            for item in (v or [])[:3]:
                if isinstance(item, dict):
                    t = {kk: vv for kk, vv in item.items()}
                    if 'evidence_text' in t:
                        t['evidence_text'] = (
                            str(t['evidence_text'])[:180]
                        )
                    if 'quote_text' in t:
                        t['quote_text'] = str(t['quote_text'])[:180]
                    trimmed.append(t)
                else:
                    trimmed.append(item)
            out[k] = trimmed
        else:
            out[k] = v
    return out
