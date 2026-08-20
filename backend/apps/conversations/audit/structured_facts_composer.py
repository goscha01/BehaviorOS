"""Compose Pipeline 1D structured-facts sections for the audit response.

Reads the latest ObservedFactExtractionRun + ConfiguredFactParserRun
per domain for the tenant, runs the deterministic diff, and returns
the sectioned payload the audit endpoint attaches.

Never triggers extraction — that's the operator's responsibility via
the trigger endpoints. This composer only reads what's already
persisted.
"""

from __future__ import annotations

from apps.conversations.models import (
    ConfiguredFactParserRun, ObservedBusinessFact,
    ObservedFactExtractionRun, OntologyReviewCandidate,
    TenantConfigSnapshot,
)


def build_structured_facts_section(tenant_external_id: str) -> dict:
    """Return the `structured_facts` payload for the audit response.
    Per-domain: whichever domains have both an observed run and a
    configured parser run get a full section with diff + samples.
    Domains without runs get an empty section with an explanation.
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
        return {'available_domains': [], 'note': (
            'no TenantConfigSnapshot for tenant'
        )}
    out: dict = {}
    for domain, builder in [
        ('pricing', _build_pricing_section),
        ('qualification', _build_qualification_section),
    ]:
        try:
            out[domain] = builder(snap)
        except Exception as exc:
            out[domain] = {
                'error': f'{type(exc).__name__}: {exc}',
            }
    out['ontology_review_candidates'] = _build_ontology_reviews(snap)
    return out


def _build_pricing_section(snap) -> dict:
    from apps.conversations.observed_config.pricing.diff import (
        build_pricing_diff,
    )
    extraction_run = (
        ObservedFactExtractionRun.objects
        .filter(
            org=snap.org, domain='pricing',
            status='completed',
        )
        .order_by('-created_at').first()
    )
    parser_run = (
        ConfiguredFactParserRun.objects
        .filter(
            snapshot=snap, domain='pricing',
            status='completed',
        )
        .order_by('-created_at').first()
    )
    if extraction_run is None and parser_run is None:
        return {
            'status': 'not_run',
            'note': (
                'No ObservedFactExtractionRun or ConfiguredFactParserRun '
                'for pricing yet. Trigger via '
                'POST /audit/observed-pricing/run and '
                'POST /audit/configured-pricing/run.'
            ),
        }
    if extraction_run is None:
        return {
            'status': 'observed_side_missing',
            'note': 'Configured pricing parsed but no observed extraction yet.',
            'configured_run_id': str(parser_run.id),
        }
    if parser_run is None:
        return {
            'status': 'configured_side_missing',
            'note': 'Observed pricing extracted but no configured parser run yet.',
            'observed_run_id': str(extraction_run.id),
        }
    diff = build_pricing_diff(
        extraction_run=extraction_run,
        parser_run=parser_run,
    )
    return {
        'status': 'ok',
        'observed_run': {
            'id': str(extraction_run.id),
            'extractor_version': extraction_run.extractor_version,
            'model': extraction_run.model,
            'conversations_processed': (
                extraction_run.conversations_processed
            ),
            'facts_emitted': extraction_run.facts_emitted,
            'llm_cost_usd': str(extraction_run.llm_cost_usd),
            'completed_at': (
                extraction_run.completed_at.isoformat()
                if extraction_run.completed_at else None
            ),
        },
        'configured_run': {
            'id': str(parser_run.id),
            'parser_version': parser_run.parser_version,
            'facts_emitted': parser_run.facts_emitted,
            'llm_cost_usd': str(parser_run.llm_cost_usd),
            'completed_at': (
                parser_run.completed_at.isoformat()
                if parser_run.completed_at else None
            ),
        },
        'diff': diff,
    }


def _build_qualification_section(snap) -> dict:
    from apps.conversations.observed_config.qualification.diff import (
        build_qualification_diff,
    )
    extraction_run = (
        ObservedFactExtractionRun.objects.filter(
            org=snap.org, domain='qualification',
            status='completed',
        ).order_by('-created_at').first()
    )
    parser_run = (
        ConfiguredFactParserRun.objects.filter(
            snapshot=snap, domain='qualification',
            status='completed',
        ).order_by('-created_at').first()
    )
    if extraction_run is None and parser_run is None:
        return {
            'status': 'not_run',
            'note': (
                'No ObservedFactExtractionRun or ConfiguredFactParserRun '
                'for qualification yet. Trigger via '
                'POST /audit/observed-qualification/run and '
                'POST /audit/configured-qualification/run.'
            ),
        }
    if extraction_run is None:
        return {
            'status': 'observed_side_missing',
            'note': (
                'Configured qualification parsed but no observed '
                'extraction yet.'
            ),
            'configured_run_id': str(parser_run.id),
        }
    if parser_run is None:
        return {
            'status': 'configured_side_missing',
            'note': (
                'Observed qualification extracted but no configured '
                'parser run yet.'
            ),
            'observed_run_id': str(extraction_run.id),
        }
    diff = build_qualification_diff(
        extraction_run=extraction_run, parser_run=parser_run,
    )
    return {
        'status': 'ok',
        'observed_run': {
            'id': str(extraction_run.id),
            'extractor_version': extraction_run.extractor_version,
            'model': extraction_run.model,
            'conversations_processed': (
                extraction_run.conversations_processed
            ),
            'facts_emitted': extraction_run.facts_emitted,
            'llm_cost_usd': str(extraction_run.llm_cost_usd),
            'completed_at': (
                extraction_run.completed_at.isoformat()
                if extraction_run.completed_at else None
            ),
        },
        'configured_run': {
            'id': str(parser_run.id),
            'parser_version': parser_run.parser_version,
            'facts_emitted': parser_run.facts_emitted,
            'llm_cost_usd': str(parser_run.llm_cost_usd),
            'completed_at': (
                parser_run.completed_at.isoformat()
                if parser_run.completed_at else None
            ),
        },
        'diff': diff,
    }


def _build_ontology_reviews(snap) -> dict:
    """Surface top OntologyReviewCandidate records for the org so
    the operator can see what the extractor flagged as
    mis-classified. Grouped by (original_event_type, proposed_scope)."""
    qs = (
        OntologyReviewCandidate.objects
        .filter(org=snap.org, reviewed=False)
        .order_by('-confidence')[:50]
    )
    by_key: dict = {}
    for r in qs:
        key = (r.original_event_type, r.proposed_scope, r.proposed_topic)
        b = by_key.setdefault(key, {
            'original_event_type': r.original_event_type,
            'proposed_scope': r.proposed_scope,
            'proposed_topic': r.proposed_topic,
            'count': 0,
            'samples': [],
        })
        b['count'] += 1
        if len(b['samples']) < 3:
            b['samples'].append({
                'evidence_conversation_id': r.evidence_conversation_id,
                'evidence_turn_id': r.evidence_turn_id,
                'evidence_text': r.evidence_text[:200],
                'confidence': r.confidence,
            })
    return {
        'clusters': list(by_key.values()),
        'total_unreviewed': qs.count(),
    }
