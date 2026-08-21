"""Pipeline 1D pricing comparison acceptance report.

Emits a human-readable + machine-readable report for the latest
completed pricing pipeline on a tenant:

  1. Distribution of ReconstructedBusinessFact verdicts (all 6 new
     pricing verdicts + legacy generic values seen in the corpus).
  2. Up to N example rows per required category, each showing:
       conversation context → observed quote → normalized
       configured rule → deterministic verdict + rationale.
  3. Coverage of the six pricing-shape categories the reviewer
     directive requires be exercised on Spotless:
       * Regular Cleaning
       * Deep Cleaning
       * recurring pricing (frequency != once)
       * sqft-banded pricing (configured has sqft_min/sqft_max)
       * hourly / additional-time pricing (pricing_basis=hourly_*)
       * oven / add-on pricing (pricing_basis=addon_flat|addon_hourly)

Usage:
    python manage.py verify_pricing_1d_acceptance \\
        --tenant <lb-user-uuid>                     \\
        [--per-category 3]                           \\
        [--json /tmp/pricing_acceptance.json]

The manual-verify step is out of scope for this command — it produces
the examples; the operator opens LB Settings and checks each example
against the real pricing_table row.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.conversations.models import (
    ConfiguredBusinessFact, Conversation, ConversationTurn,
    ReconstructedBusinessFact, UnifiedBusinessReconstructionRun,
)


REQUIRED_CATEGORIES = (
    'regular_cleaning',
    'deep_cleaning',
    'recurring',
    'sqft_banded',
    'hourly',
    'addon',
)


class Command(BaseCommand):
    help = 'Pipeline 1D pricing comparison acceptance report (P7).'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', required=True,
                             help='LB User UUID (tenant_external_id)')
        parser.add_argument('--per-category', type=int, default=3,
                             help='Max example rows per category')
        parser.add_argument('--json', default=None,
                             help='Optional path to dump machine-readable JSON')

    def handle(self, *args, **options):
        tenant = options['tenant']
        per_cat = options['per_category']

        run = (
            UnifiedBusinessReconstructionRun.objects
            .filter(tenant_external_id=tenant, status='completed')
            .order_by('-created_at').first()
        )
        if run is None:
            raise CommandError(
                f'No completed UnifiedBusinessReconstructionRun for '
                f'tenant {tenant}. Run the pipeline first.'
            )

        facts = list(
            ReconstructedBusinessFact.objects
            .filter(reconstruction_run=run, domain='pricing')
        )
        cfg_facts = list(
            ConfiguredBusinessFact.objects
            .filter(snapshot__tenant_external_id=tenant, domain='pricing')
        )
        cfg_by_id = {str(c.id): c for c in cfg_facts}

        self.stdout.write(self.style.NOTICE(
            f'\n=== Pipeline 1D Pricing Acceptance — tenant={tenant} ==='
        ))
        self.stdout.write(
            f'Reconstruction run: {run.id} ({run.created_at.isoformat()})'
        )
        self.stdout.write(
            f'Pricing facts: {len(facts)}   '
            f'Configured pricing facts: {len(cfg_facts)}'
        )

        # 1) verdict distribution
        by_verdict: dict[str, int] = defaultdict(int)
        for f in facts:
            by_verdict[f.relationship_to_config] += 1
        self.stdout.write('\n--- Verdict distribution ---')
        for verdict, n in sorted(by_verdict.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  {verdict:38s}  {n}')

        # 2) coverage of required categories
        by_cat: dict[str, list[ReconstructedBusinessFact]] = defaultdict(list)
        for f in facts:
            for cat in _categorize(f, cfg_by_id):
                by_cat[cat].append(f)

        self.stdout.write('\n--- Required-category coverage ---')
        for cat in REQUIRED_CATEGORIES:
            n = len(by_cat.get(cat, []))
            mark = '✓' if n > 0 else '✗'
            self.stdout.write(f'  {mark} {cat:20s} facts={n}')

        # 3) examples per category
        report: dict[str, Any] = {
            'tenant_external_id': tenant,
            'reconstruction_run_id': str(run.id),
            'verdict_distribution': dict(by_verdict),
            'category_coverage': {c: len(by_cat.get(c, [])) for c in REQUIRED_CATEGORIES},
            'examples': {},
        }

        self.stdout.write('\n--- Examples ---')
        for cat in REQUIRED_CATEGORIES:
            examples = by_cat.get(cat, [])[:per_cat]
            report['examples'][cat] = []
            if not examples:
                continue
            self.stdout.write(self.style.HTTP_INFO(f'\n[{cat}]'))
            for f in examples:
                example = _describe_example(f, cfg_by_id)
                report['examples'][cat].append(example)
                _print_example(self, example)

        if options.get('json'):
            with open(options['json'], 'w') as fp:
                json.dump(report, fp, indent=2, default=str)
            self.stdout.write(self.style.SUCCESS(
                f'\nWrote machine-readable report to {options["json"]}'
            ))


# ─── categorization ────────────────────────────────────────────────

def _categorize(f: ReconstructedBusinessFact, cfg_by_id: dict) -> list[str]:
    """Return the list of REQUIRED_CATEGORIES this fact belongs to."""
    cats: list[str] = []
    obs_subj = f.canonical_subject_json or {}
    obs_val = f.observed_value_json or {}
    service = (obs_subj.get('service') or '').lower()
    tier = (obs_subj.get('service_tier') or '').lower()
    freq = (obs_subj.get('frequency') or '').lower()
    basis = (obs_subj.get('pricing_basis') or '').lower()
    addons = obs_subj.get('addons') or []

    if service in ('cleaning', 'house_cleaning') and tier == 'regular':
        cats.append('regular_cleaning')
    if service in ('cleaning', 'house_cleaning') and tier in ('deep', 'move_in', 'move_out'):
        cats.append('deep_cleaning')
    if freq and freq not in ('once', 'one-time', ''):
        cats.append('recurring')
    if basis in ('hourly_per_cleaner', 'hourly_team', 'addon_hourly'):
        cats.append('hourly')
    if basis in ('addon_flat', 'addon_hourly') or addons:
        cats.append('addon')

    # sqft-banded: was the matched configured rule's subject_key
    # sqft-banded?
    matched_cfg_id = ((obs_val.get('matcher') or {}).get('matched_configured_fact_id'))
    if matched_cfg_id:
        cfg = cfg_by_id.get(matched_cfg_id)
        if cfg:
            csubj = cfg.subject_key_json or {}
            if csubj.get('sqft_min') is not None and csubj.get('sqft_max') is not None:
                cats.append('sqft_banded')
    # Even without a match, a candidate list containing an sqft-banded
    # rule counts as sqft-banded exposure.
    for cid in ((obs_val.get('matcher') or {}).get('candidate_configured_fact_ids') or []):
        cfg = cfg_by_id.get(cid)
        if cfg:
            csubj = cfg.subject_key_json or {}
            if csubj.get('sqft_min') is not None and csubj.get('sqft_max') is not None:
                if 'sqft_banded' not in cats:
                    cats.append('sqft_banded')
                break
    return cats


# ─── example rendering ─────────────────────────────────────────────

def _describe_example(f: ReconstructedBusinessFact, cfg_by_id: dict) -> dict:
    obs_val = f.observed_value_json or {}
    matcher = obs_val.get('matcher') or {}
    matched_cfg = cfg_by_id.get(matcher.get('matched_configured_fact_id') or '')
    candidates = [
        cfg_by_id.get(cid) for cid in (matcher.get('candidate_configured_fact_ids') or [])
    ]
    candidates = [c for c in candidates if c is not None]

    # Pull a sample conversation quote from dimension_samples for
    # the "conversation context" column.
    samples = obs_val.get('dimension_samples') or []
    sample = samples[0] if samples else {}
    conv_snippet = _pull_conversation_snippet(
        conv_id=sample.get('conversation_id'),
        turn_id=sample.get('turn_id'),
    )

    return {
        'fact_id': str(f.id),
        'verdict': f.relationship_to_config,
        'onboarding_class': f.onboarding_class,
        'observed_subject': f.canonical_subject_json,
        'observed_stats': obs_val.get('amount_stats'),
        'observed_sample_quote': {
            'amount': sample.get('amount'),
            'square_footage': sample.get('square_footage'),
            'bedrooms': sample.get('bedrooms'),
            'bathrooms': sample.get('bathrooms'),
            'conversation_id': sample.get('conversation_id'),
            'turn_id': sample.get('turn_id'),
        },
        'conversation_context_snippet': conv_snippet,
        'matched_configured_rule': (
            {
                'fact_id': str(matched_cfg.id),
                'subject': matched_cfg.subject_key_json,
                'value': matched_cfg.value_json,
                'source_pointer': matched_cfg.source_pointer,
            } if matched_cfg else None
        ),
        'candidate_count': len(candidates),
        'candidate_subjects': [c.subject_key_json for c in candidates[:5]],
        'missing_observed_dimensions': matcher.get('missing_observed_dimensions') or [],
        'rationale': matcher.get('rationale', ''),
        'price_comparison': matcher.get('price_comparison'),
    }


def _pull_conversation_snippet(*, conv_id, turn_id) -> list[dict]:
    """Return the 6 turns surrounding the quote turn (3 before, quote,
    2 after) so the operator can eyeball the conversation context that
    the extractor saw. Empty list when we can't resolve the ids."""
    if not conv_id or not turn_id:
        return []
    try:
        conv = Conversation.objects.get(pk=conv_id)
    except Conversation.DoesNotExist:
        return []
    turns = list(
        ConversationTurn.objects
        .filter(conversation=conv).order_by('turn_index')
        .values('turn_index', 'speaker', 'text_content')
    )
    if not turns:
        return []
    # LLM turn_id is opaque ("t0044") — we tag by turn_index. Try
    # both stringified and parsed forms.
    q_idx = None
    for i, t in enumerate(turns):
        tag = f't{t["turn_index"]:04d}'
        if tag == str(turn_id):
            q_idx = i
            break
    if q_idx is None:
        return turns[:6]
    lo = max(0, q_idx - 3)
    hi = min(len(turns), q_idx + 3)
    return turns[lo:hi]


def _print_example(cmd: BaseCommand, ex: dict) -> None:
    cmd.stdout.write(
        f'  fact={ex["fact_id"][:8]}  verdict={ex["verdict"]}  '
        f'onboarding={ex["onboarding_class"]}'
    )
    cmd.stdout.write(f'    observed subject: {json.dumps(ex["observed_subject"], sort_keys=True)}')
    stats = ex['observed_stats'] or {}
    if stats:
        cmd.stdout.write(
            f'    observed stats: median=${stats.get("median")} '
            f'p25=${stats.get("p25")} p75=${stats.get("p75")} '
            f'n={stats.get("support_n")}'
        )
    sq = ex['observed_sample_quote']
    if sq.get('amount') is not None:
        cmd.stdout.write(
            f'    sample quote: ${sq["amount"]} '
            f'(sqft={sq.get("square_footage")}, '
            f'bed={sq.get("bedrooms")}, bath={sq.get("bathrooms")}, '
            f'conv={str(sq.get("conversation_id"))[:8]}, turn={sq.get("turn_id")})'
        )
    for t in ex['conversation_context_snippet']:
        cmd.stdout.write(
            f'      [t{t["turn_index"]:04d}][{t["speaker"]}] '
            f'{(t["text_content"] or "")[:120]}'
        )
    matched = ex.get('matched_configured_rule')
    if matched:
        cmd.stdout.write(f'    matched configured rule:')
        cmd.stdout.write(f'      subject: {json.dumps(matched["subject"], sort_keys=True)}')
        cmd.stdout.write(f'      value:   {json.dumps(matched["value"], sort_keys=True)}')
        cmd.stdout.write(f'      source:  {json.dumps(matched["source_pointer"], sort_keys=True)}')
    elif ex['candidate_count']:
        cmd.stdout.write(
            f'    {ex["candidate_count"]} plausible configured candidates '
            f'(missing dims: {ex["missing_observed_dimensions"] or "—"})'
        )
    else:
        cmd.stdout.write('    no compatible configured rule found')
    if ex.get('price_comparison'):
        pc = ex['price_comparison']
        cmd.stdout.write(
            f'    price comparison: observed_median=${pc["observed_median"]:.2f} '
            f'vs configured=${pc["configured"]:.2f} '
            f'(delta {pc["delta_pct"]:+.1%}, within=${pc["tolerance"]:.2f})'
        )
    cmd.stdout.write(f'    rationale: {ex["rationale"]}')
    cmd.stdout.write('')
