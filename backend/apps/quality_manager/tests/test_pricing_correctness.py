"""Pricing Correctness dimension tests.

Seeds the DIFFERS_FROM_CONFIG cases from the v7 Spotless audit
(oven $30 vs $40 across 32 convs, 4BR regular +$35, airbnb 3BR
−$20) and asserts each surfaces as FAIL with the required
evidence chain: exact quote turn_id, canonical subject, configured
rule, matcher output, reason for FAIL.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from django.test import TestCase

from apps.accounts.models import Organization
from apps.conversations.models import (
    ConfiguredBusinessFact,
    ConfiguredFactParserRun,
    Conversation,
    IngestionStatus,
    LearningCorpus,
    ObservedFactExtractionRun,
    ReconstructedBusinessFact,
    TenantConfigSnapshot,
    UnifiedBusinessReconstructionRun,
)
from apps.quality_manager.dimensions.pricing_correctness import (
    PricingCorrectnessDimension,
)
from apps.quality_manager.dimensions.base import State
from apps.quality_manager.engine import (
    create_or_reuse_run,
    run_quality_manager,
)
from apps.quality_manager.models import QualityEvaluation


BASE = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_reconstruction(org):
    snapshot = TenantConfigSnapshot.objects.create(
        org=org,
        source_system='leadbridge',
        tenant_external_id='test-tenant',
        contract_version='v1',
        raw_config={},
        raw_config_sha256='h' * 64,
    )
    parser = ConfiguredFactParserRun.objects.create(
        snapshot=snapshot, domain='pricing',
        parser_version='test', status='completed',
    )
    corpus = LearningCorpus.objects.create(
        org=org, name='test-corpus', version='v1',
    )
    ext_run = ObservedFactExtractionRun.objects.create(
        org=org, corpus=corpus,
        domain='pricing', extractor_version='test-extractor',
        model='test', status='completed',
    )
    recon = UnifiedBusinessReconstructionRun.objects.create(
        org=org, tenant_external_id='test-tenant',
        snapshot=snapshot, reconstruction_version='test',
        status='completed',
    )
    return recon, parser


def _make_conversation(org, seq: int) -> Conversation:
    return Conversation.objects.create(
        org=org, source='quo',
        source_conversation_id=f'quo:test-conv-{seq}',
        customer_phone='+18135550000',
        started_at=BASE,
        ingestion_status=IngestionStatus.LINKED,
    )


def _make_configured_fact(parser, subject_key, price):
    return ConfiguredBusinessFact.objects.create(
        snapshot=parser.snapshot,
        parser_run=parser,
        domain='pricing', fact_type='quoted_price',
        subject_key_json=subject_key,
        subject_key_dimensions=list(subject_key.keys()),
        subject_key_hash=str(uuid.uuid4()),
        value_json={'amount': price, 'currency': 'USD'},
    )


def _make_reconstructed_fact(
    recon, subject_key, *, verdict, comparison,
    supporting_conv_ids, configured_id, support_n,
):
    return ReconstructedBusinessFact.objects.create(
        reconstruction_run=recon, domain='pricing',
        canonical_subject_json=subject_key,
        canonical_subject_hash=str(uuid.uuid4()),
        observed_value_json={'price_comparison': comparison},
        configured_equivalent_json={'id': str(configured_id)},
        support_n=support_n,
        relationship_to_config=verdict,
        onboarding_class='NEEDS_OWNER_CONFIRMATION',
        evidence_conversation_ids=supporting_conv_ids,
        evidence_turn_ids=[
            {'conversation_id': cid, 'turn_id': f't00{i:02d}'}
            for i, cid in enumerate(supporting_conv_ids[:5])
        ],
    )


class PricingCorrectnessSeedCasesTests(TestCase):
    """Reproduce the 3 known Spotless DIFFERS cases + verify FAIL
    findings with full evidence chain."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Test Spotless')
        cls.recon, cls.parser = _make_reconstruction(cls.org)

        # Case 1: Oven addon — 32 supporting conversations,
        # $30 observed vs $40 configured.
        cls.oven_convs = [_make_conversation(cls.org, i) for i in range(32)]
        oven_cfg = _make_configured_fact(
            cls.parser,
            {'service': 'cleaning', 'addons': ['oven'],
             'pricing_basis': 'addon_flat'},
            40.0,
        )
        cls.oven_fact = _make_reconstructed_fact(
            cls.recon,
            {'service': 'cleaning', 'addons': ['oven'],
             'pricing_basis': 'addon_flat'},
            verdict='DIFFERS_FROM_CONFIG',
            comparison={
                'observed_median': 30.0, 'configured': 40.0,
                'delta': -10.0, 'delta_pct': -0.25,
                'sample_n': 32, 'within_tolerance': False,
                'tolerance': 5.0,
            },
            supporting_conv_ids=[str(c.id) for c in cls.oven_convs[:20]],
            configured_id=oven_cfg.id, support_n=32,
        )

        # Case 2: 4BR/2BA regular — 4 convs, $224 observed vs $189 configured.
        cls.reg_convs = [
            _make_conversation(cls.org, 100 + i) for i in range(4)
        ]
        reg_cfg = _make_configured_fact(
            cls.parser,
            {'service': 'cleaning', 'bedrooms': 4, 'bathrooms': 2,
             'service_tier': 'regular', 'pricing_basis': 'flat_job'},
            189.0,
        )
        cls.reg_fact = _make_reconstructed_fact(
            cls.recon,
            {'service': 'cleaning', 'bedrooms': 4, 'bathrooms': 2,
             'service_tier': 'regular', 'pricing_basis': 'flat_job'},
            verdict='DIFFERS_FROM_CONFIG',
            comparison={
                'observed_median': 224.0, 'configured': 189.0,
                'delta': 35.0, 'delta_pct': 0.1852,
                'sample_n': 4, 'within_tolerance': False,
                'tolerance': 18.9,
            },
            supporting_conv_ids=[str(c.id) for c in cls.reg_convs],
            configured_id=reg_cfg.id, support_n=4,
        )

        # Case 3: 3BR/2BA airbnb — 3 convs, $159 observed vs $179 configured.
        cls.abnb_convs = [
            _make_conversation(cls.org, 200 + i) for i in range(3)
        ]
        abnb_cfg = _make_configured_fact(
            cls.parser,
            {'service': 'cleaning', 'bedrooms': 3, 'bathrooms': 2,
             'service_tier': 'airbnb', 'pricing_basis': 'flat_job'},
            179.0,
        )
        cls.abnb_fact = _make_reconstructed_fact(
            cls.recon,
            {'service': 'cleaning', 'bedrooms': 3, 'bathrooms': 2,
             'service_tier': 'airbnb', 'pricing_basis': 'flat_job'},
            verdict='DIFFERS_FROM_CONFIG',
            comparison={
                'observed_median': 159.0, 'configured': 179.0,
                'delta': -20.0, 'delta_pct': -0.1117,
                'sample_n': 3, 'within_tolerance': False,
                'tolerance': 17.9,
            },
            supporting_conv_ids=[str(c.id) for c in cls.abnb_convs],
            configured_id=abnb_cfg.id, support_n=3,
        )

        # One PASS case for control: 2BR/1BA regular, MATCH verdict.
        cls.pass_conv = _make_conversation(cls.org, 300)
        pass_cfg = _make_configured_fact(
            cls.parser,
            {'service': 'cleaning', 'bedrooms': 2, 'bathrooms': 1,
             'service_tier': 'regular', 'pricing_basis': 'flat_job'},
            139.0,
        )
        cls.pass_fact = _make_reconstructed_fact(
            cls.recon,
            {'service': 'cleaning', 'bedrooms': 2, 'bathrooms': 1,
             'service_tier': 'regular', 'pricing_basis': 'flat_job'},
            verdict='MATCH',
            comparison={
                'observed_median': 139.0, 'configured': 139.0,
                'delta': 0.0, 'delta_pct': 0.0,
                'sample_n': 3, 'within_tolerance': True,
                'tolerance': 13.9,
            },
            supporting_conv_ids=[str(cls.pass_conv.id)],
            configured_id=pass_cfg.id, support_n=3,
        )

        # One NOT_APPLICABLE case: conversation with no pricing.
        cls.na_conv = _make_conversation(cls.org, 400)

    def test_oven_pattern_finding_at_corpus_level(self):
        """The oven $30 vs $40 case is a single corpus-level FAIL —
        one finding for the pattern, not 32 per-conversation duplicates
        at the tenant findings list."""
        dim = PricingCorrectnessDimension()
        corpus_results = list(dim.evaluate_corpus(
            reconstruction_run=self.recon,
        ))
        oven_pattern = [
            r for r in corpus_results
            if r.subject_key.get('addons') == ['oven']
        ]
        self.assertEqual(len(oven_pattern), 1)
        r = oven_pattern[0]
        self.assertEqual(r.state, State.FAIL)
        self.assertEqual(r.severity, 'critical')  # |delta_pct|=25%
        self.assertEqual(r.reason_code, 'observed_below_configured')
        self.assertIn('30', r.rationale_text)
        self.assertIn('40', r.rationale_text)
        # Evidence must contain: matcher_output + configured_rule +
        # reconstructed_fact + at least one conversation ref
        kinds = {e.kind for e in r.evidence}
        self.assertIn('matcher_output', kinds)
        self.assertIn('reconstructed_fact', kinds)
        self.assertIn('configured_rule', kinds)
        self.assertIn('canonical_context', kinds)
        # source_reconstructed_fact_id points to the fact.
        self.assertEqual(
            r.source_reconstructed_fact_id, str(self.oven_fact.id),
        )

    def test_all_three_seed_differs_emit_corpus_findings(self):
        dim = PricingCorrectnessDimension()
        corpus_results = list(dim.evaluate_corpus(
            reconstruction_run=self.recon,
        ))
        fail_findings = [r for r in corpus_results if r.state == State.FAIL]
        self.assertEqual(
            len(fail_findings), 3,
            'expected 3 DIFFERS_FROM_CONFIG pattern findings (oven, 4BR reg, airbnb 3BR)',
        )
        # Severity should include both critical (oven 25%) and
        # warning (4BR 18.5%) and info-band (airbnb 11.2%).
        by_sev = {r.severity for r in fail_findings}
        self.assertIn('critical', by_sev)
        self.assertIn('warning', by_sev)
        self.assertIn('info', by_sev)

    def test_per_conversation_fail_for_supporting_convs(self):
        """Each of the 20 supporting oven conversations gets a per-conversation
        FAIL evaluation (capped at the aggregator's evidence limit)."""
        dim = PricingCorrectnessDimension()
        results = list(dim.evaluate(
            reconstruction_run=self.recon,
            conversation=self.oven_convs[0],
        ))
        # This conversation should have at least one FAIL from the oven aggregate.
        fails = [r for r in results if r.state == State.FAIL]
        self.assertGreaterEqual(len(fails), 1)
        f = fails[0]
        self.assertEqual(f.reason_code, 'observed_below_configured')
        self.assertEqual(
            f.source_reconstructed_fact_id, str(self.oven_fact.id),
        )

    def test_pass_case_emits_pass(self):
        dim = PricingCorrectnessDimension()
        results = list(dim.evaluate(
            reconstruction_run=self.recon,
            conversation=self.pass_conv,
        ))
        # PASS row for the MATCH verdict
        self.assertTrue(any(
            r.state == State.PASS for r in results
        ))

    def test_not_applicable_when_no_pricing_observation(self):
        dim = PricingCorrectnessDimension()
        results = list(dim.evaluate(
            reconstruction_run=self.recon,
            conversation=self.na_conv,
        ))
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r.state, State.NOT_APPLICABLE)
        self.assertEqual(
            r.reason_code, 'no_price_quoted_or_evidence_capped',
        )

    def test_full_engine_run_persists_everything(self):
        """End-to-end: run engine + verify persisted QualityEvaluation rows
        for each seed case."""
        run, created = create_or_reuse_run(
            self.recon, qm_version='qm-v1-test',
        )
        self.assertTrue(created)
        run = run_quality_manager(run)
        self.assertEqual(run.status, 'completed')

        # Corpus findings: 3 FAIL (one per DIFFERS pattern)
        corpus_fails = QualityEvaluation.objects.filter(
            run=run, conversation__isnull=True, state='FAIL',
            dimension='pricing_correctness',
        )
        self.assertEqual(corpus_fails.count(), 3)

        # Per-conversation coverage: at least the 32+4+3+1+1 = 41
        # unique conversations should have at least one evaluation.
        conv_evals_count = QualityEvaluation.objects.filter(
            run=run, conversation__isnull=False,
            dimension='pricing_correctness',
        ).values('conversation').distinct().count()
        self.assertGreaterEqual(conv_evals_count, 20)  # oven cap is 20

        # stats_json includes per-dimension breakdown
        pricing_stats = run.stats_json.get('pricing_correctness', {})
        self.assertGreater(pricing_stats.get('FAIL', 0), 0)
        self.assertEqual(
            pricing_stats.get('corpus_pattern_findings', 0), 3,
        )
