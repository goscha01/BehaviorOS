"""Deterministic pricing matcher acceptance tests — CONFIG-ANCHORED.

Per the 2026-08-21 reviewer correction: the LB pricing table is the
ontology, observed quotes are attempts to instantiate table cells.
Every test asserts a verdict PER CONFIGURED CELL after
match_by_cell runs, not per observed subject.

Verdict coverage (one test each):
  - MATCH — cell has >= MIN_UNIQUE unique observed quotes and
    their median aligns with the cell's amount.
  - DIFFERS_FROM_CONFIG — same shape, median outside tolerance.
  - INSUFFICIENT_CONTEXT_TO_COMPARE — cell has only partial-evidence
    quotes (compatible with this cell AND other cells).
  - VARIABLE_CONTEXT_DEPENDENT — cell has enough unique quotes but
    their IQR/median is above threshold.
  - CONFIGURED_NOT_OBSERVED — cell has zero compatible quotes.
  - OBSERVED_NOT_CONFIGURED (residual) — observed quote whose
    subject fits no cell.

Plus the reviewer refinements:
  - A sample with raw square_footage is placed in the cell whose
    [sqft_min, sqft_max] contains it — never in the bucket enum.
  - A sample missing sqft is partial evidence spread across every
    sqft-banded cell with matching bed/bath — never forces
    INSUFFICIENT_CONTEXT_TO_COMPARE on cells it can't disambiguate.
"""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.models import Organization
from apps.conversations.models import (
    ConfiguredBusinessFact, ConfiguredFactParserRun,
    LearningCorpus, ObservedBusinessFact,
    ObservedFactExtractionRun, ReconstructedBusinessFact,
    TenantConfigSnapshot,
)
from apps.conversations.observed_config.base import canonical_subject_key
from apps.conversations.reconstruction.pricing_matcher import (
    MatchInputs, match_by_cell,
)


RTC = ReconstructedBusinessFact.RelationshipToConfig


class PricingCellMatcherAcceptanceTests(TestCase):

    def setUp(self):
        self.org = Organization.objects.create(name='Pricing Cell Test Org')
        self.corpus = LearningCorpus.objects.create(
            org=self.org, name='test-corpus', version='v1',
            selection_criteria={}, member_count=1,
        )
        self.obs_run = ObservedFactExtractionRun.objects.create(
            org=self.org, corpus=self.corpus,
            domain=ObservedBusinessFact.Domain.PRICING,
            extractor_version='test-extractor-v3',
            model='test-model',
            status=ObservedFactExtractionRun.Status.COMPLETED,
        )
        self.snapshot = TenantConfigSnapshot.objects.create(
            org=self.org,
            source_system=TenantConfigSnapshot.SourceSystem.LEADBRIDGE,
            tenant_external_id='test-tenant',
            service_group='',
            contract_version='test',
            raw_config={},
            raw_config_sha256='0' * 64,
            fetched_from_url='',
        )
        self.cfg_run = ConfiguredFactParserRun.objects.create(
            snapshot=self.snapshot,
            domain=ObservedBusinessFact.Domain.PRICING,
            parser_version='test-cfg-parser-v3',
            model='test-model',
            status=ConfiguredFactParserRun.Status.COMPLETED,
        )

    # ─── Fact builders ──────────────────────────────────────────

    def _cell(self, *, subject: dict, amount: float) -> ConfiguredBusinessFact:
        _, sha, dims = canonical_subject_key(subject)
        return ConfiguredBusinessFact.objects.create(
            snapshot=self.snapshot, parser_run=self.cfg_run,
            domain=ObservedBusinessFact.Domain.PRICING,
            fact_type='quoted_price',
            subject_key_json=subject,
            subject_key_dimensions=dims,
            subject_key_hash=sha,
            value_json={'amount': amount, 'currency': 'USD'},
            source_pointer={'ref': 'test'},
            parser_confidence=1.0,
        )

    def _observed(self, *, subject: dict, samples: list[dict],
                   support_n: int | None = None) -> ObservedBusinessFact:
        _, sha, dims = canonical_subject_key(subject)
        return ObservedBusinessFact.objects.create(
            org=self.org, corpus=self.corpus, extraction_run=self.obs_run,
            domain=ObservedBusinessFact.Domain.PRICING,
            fact_type='quoted_price',
            subject_key_json=subject,
            subject_key_dimensions=dims,
            subject_key_hash=sha,
            value_json={
                'fact_type': 'quoted_price', 'currency': 'USD',
                'dimension_samples': samples,
            },
            support_n=support_n if support_n is not None else len(samples),
        )

    # ─── MATCH ──────────────────────────────────────────────────

    def test_cell_matches_when_unique_observed_median_aligns(self):
        """3+ unique observations pinned to a specific cell whose
        median is within ±10% of the cell's amount → MATCH for
        THAT cell."""
        cell_small = self._cell(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 2, 'bathrooms': 1,
                'sqft_min': 800, 'sqft_max': 1000,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=149,
        )
        cell_target = self._cell(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1601, 'sqft_max': 2000,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=209,
        )
        obs = self._observed(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            samples=[
                {'amount': 209, 'square_footage': 1800, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 209, 'square_footage': 1750, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 219, 'square_footage': 1900, 'bedrooms': 3, 'bathrooms': 2},
            ],
        )
        verdicts, orphans = match_by_cell(MatchInputs(
            observed_facts=[obs],
            configured_facts=[cell_small, cell_target],
        ))
        self.assertEqual(orphans, [])
        by_id = {str(v.cell.id): v for v in verdicts}
        self.assertEqual(by_id[str(cell_target.id)].verdict, RTC.MATCH)
        self.assertEqual(by_id[str(cell_small.id)].verdict, RTC.CONFIGURED_NOT_OBSERVED)
        pc = by_id[str(cell_target.id)].price_comparison
        self.assertTrue(pc['within_tolerance'])
        self.assertEqual(pc['sample_n'], 3)

    # ─── DIFFERS_FROM_CONFIG ───────────────────────────────────

    def test_cell_differs_from_config_when_unique_median_outside_tolerance(self):
        cell = self._cell(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1601, 'sqft_max': 2000,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=189,
        )
        obs = self._observed(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            samples=[
                {'amount': 249, 'square_footage': 1800, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 259, 'square_footage': 1750, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 249, 'square_footage': 1900, 'bedrooms': 3, 'bathrooms': 2},
            ],
        )
        verdicts, _ = match_by_cell(MatchInputs(
            observed_facts=[obs], configured_facts=[cell],
        ))
        v = verdicts[0]
        self.assertEqual(v.verdict, RTC.DIFFERS_FROM_CONFIG)
        self.assertFalse(v.price_comparison['within_tolerance'])

    # ─── MATCH tentative (asymmetric threshold) ────────────────

    def test_cell_matches_tentative_when_single_unique_quote_aligns(self):
        """Asymmetric threshold (2026-08-22): 1 unique quote at the
        cell's exact configured amount → MATCH (tentative), not
        INSUFFICIENT. Rationale mentions 'tentative (n<3)'."""
        cell = self._cell(
            subject={
                'service': 'cleaning', 'service_tier': 'deep',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1300, 'sqft_max': 1600,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=219,
        )
        obs = self._observed(
            subject={
                'service': 'cleaning', 'service_tier': 'deep',
                'bedrooms': 3, 'bathrooms': 2,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            samples=[
                {'amount': 219, 'square_footage': 1500, 'bedrooms': 3, 'bathrooms': 2},
            ],
        )
        verdicts, _ = match_by_cell(MatchInputs(
            observed_facts=[obs], configured_facts=[cell],
        ))
        v = verdicts[0]
        self.assertEqual(v.verdict, RTC.MATCH)
        self.assertIn('tentative', v.rationale)

    def test_cell_insufficient_when_single_unique_quote_disagrees(self):
        """Asymmetric threshold: a single OFF-tolerance quote is
        NOT enough to emit DIFFERS_FROM_CONFIG. Fall through to
        INSUFFICIENT_CONTEXT_TO_COMPARE — one outlier isn't a
        real conflict."""
        cell = self._cell(
            subject={
                'service': 'cleaning', 'service_tier': 'deep',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1300, 'sqft_max': 1600,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=219,
        )
        obs = self._observed(
            subject={
                'service': 'cleaning', 'service_tier': 'deep',
                'bedrooms': 3, 'bathrooms': 2,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            samples=[
                {'amount': 349, 'square_footage': 1500, 'bedrooms': 3, 'bathrooms': 2},
            ],
        )
        verdicts, _ = match_by_cell(MatchInputs(
            observed_facts=[obs], configured_facts=[cell],
        ))
        v = verdicts[0]
        self.assertEqual(v.verdict, RTC.INSUFFICIENT_CONTEXT_TO_COMPARE)
        self.assertIn('need >= 3', v.rationale)

    # ─── INSUFFICIENT_CONTEXT_TO_COMPARE (partial only) ────────

    def test_cell_insufficient_when_only_partial_evidence(self):
        """Two cells differ only by sqft-band. An observed quote
        without square_footage is compatible with BOTH → partial
        evidence to each cell, neither reaches MATCH/DIFFERS."""
        cell_a = self._cell(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1000, 'sqft_max': 1200,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=169,
        )
        cell_b = self._cell(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1601, 'sqft_max': 2000,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=209,
        )
        # 3 samples with bed/bath but NO sqft — partial evidence
        # shared across both cells.
        obs = self._observed(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            samples=[
                {'amount': 199, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 189, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 209, 'bedrooms': 3, 'bathrooms': 2},
            ],
        )
        verdicts, _ = match_by_cell(MatchInputs(
            observed_facts=[obs], configured_facts=[cell_a, cell_b],
        ))
        for v in verdicts:
            self.assertEqual(v.verdict, RTC.INSUFFICIENT_CONTEXT_TO_COMPARE)
            self.assertEqual(len(v.unique_samples), 0)
            self.assertGreater(len(v.partial_samples), 0)

    # ─── VARIABLE_CONTEXT_DEPENDENT ────────────────────────────

    def test_cell_variable_when_unique_samples_have_wide_iqr(self):
        cell = self._cell(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1601, 'sqft_max': 2000,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=189,
        )
        obs = self._observed(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            samples=[
                {'amount': 149, 'square_footage': 1700, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 189, 'square_footage': 1800, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 259, 'square_footage': 1900, 'bedrooms': 3, 'bathrooms': 2},
            ],
        )
        verdicts, _ = match_by_cell(MatchInputs(
            observed_facts=[obs], configured_facts=[cell],
        ))
        self.assertEqual(verdicts[0].verdict, RTC.VARIABLE_CONTEXT_DEPENDENT)

    # ─── CONFIGURED_NOT_OBSERVED ───────────────────────────────

    def test_cell_configured_not_observed_when_no_compatible_sample(self):
        cell = self._cell(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 5, 'bathrooms': 4,
                'sqft_min': 3000, 'sqft_max': 5000,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=349,
        )
        # Sample explicitly says bed=3 — incompatible with bed=5 cell.
        obs = self._observed(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            samples=[{'amount': 209, 'bedrooms': 3, 'bathrooms': 2}],
        )
        verdicts, _ = match_by_cell(MatchInputs(
            observed_facts=[obs], configured_facts=[cell],
        ))
        self.assertEqual(verdicts[0].verdict, RTC.CONFIGURED_NOT_OBSERVED)
        self.assertEqual(len(verdicts[0].unique_samples), 0)
        self.assertEqual(len(verdicts[0].partial_samples), 0)

    # ─── OBSERVED_NOT_CONFIGURED (residual) ────────────────────

    def test_orphaned_observed_subject_when_no_cell_compatible(self):
        """Observed subject has an addon that isn't in ANY configured
        cell → residual orphan bucket."""
        # Configured has regular cleaning only.
        self._cell(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1000, 'sqft_max': 1200,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=169,
        )
        # Observed is oven cleaning addon — no compatible configured.
        obs = self._observed(
            subject={
                'service': 'cleaning', 'service_tier': 'oven cleaning',
                'addons': ['oven'],
                'pricing_basis': 'discount_price',
            },
            samples=[
                {'amount': 15, 'service_tier': 'oven cleaning', 'addons': ['oven']},
                {'amount': 15, 'service_tier': 'oven cleaning', 'addons': ['oven']},
                {'amount': 15, 'service_tier': 'oven cleaning', 'addons': ['oven']},
            ],
        )
        _, orphans = match_by_cell(MatchInputs(
            observed_facts=[obs],
            configured_facts=list(ConfiguredBusinessFact.objects.filter(
                parser_run=self.cfg_run,
            )),
        ))
        self.assertEqual(len(orphans), 1)
        self.assertEqual(str(orphans[0].observed_fact.id), str(obs.id))
        self.assertIn('service_tier', orphans[0].reason.lower() + orphans[0].reason)

    # ─── Refinement: sqft interval containment ─────────────────

    def test_sample_placed_in_correct_sqft_band_by_interval(self):
        """Observed sample with raw sqft=1800 uniquely belongs to
        the [1601, 2000] cell — the other cells' sqft intervals
        rule it out."""
        cell_small = self._cell(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1000, 'sqft_max': 1200,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=169,
        )
        cell_target = self._cell(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1601, 'sqft_max': 2000,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=209,
        )
        obs = self._observed(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            samples=[
                {'amount': 209, 'square_footage': 1800, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 209, 'square_footage': 1850, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 219, 'square_footage': 1750, 'bedrooms': 3, 'bathrooms': 2},
            ],
        )
        verdicts, _ = match_by_cell(MatchInputs(
            observed_facts=[obs],
            configured_facts=[cell_small, cell_target],
        ))
        by_id = {str(v.cell.id): v for v in verdicts}
        self.assertEqual(by_id[str(cell_target.id)].verdict, RTC.MATCH)
        self.assertEqual(by_id[str(cell_small.id)].verdict, RTC.CONFIGURED_NOT_OBSERVED)

    # ─── Refinement: partial evidence spreads across cells ─────

    def test_partial_evidence_reaches_all_compatible_cells_but_elevates_none(self):
        """A sample missing sqft is partial evidence to EVERY
        sqft-banded cell of the same bed/bath — never a MATCH
        driver alone, but its price still summarizes into the
        partial-evidence description."""
        cells = [
            self._cell(
                subject={
                    'service': 'cleaning', 'service_tier': 'regular',
                    'bedrooms': 3, 'bathrooms': 2,
                    'sqft_min': 1000, 'sqft_max': 1200,
                    'frequency': 'once', 'pricing_basis': 'flat_job',
                },
                amount=169,
            ),
            self._cell(
                subject={
                    'service': 'cleaning', 'service_tier': 'regular',
                    'bedrooms': 3, 'bathrooms': 2,
                    'sqft_min': 1601, 'sqft_max': 2000,
                    'frequency': 'once', 'pricing_basis': 'flat_job',
                },
                amount=209,
            ),
        ]
        obs = self._observed(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            samples=[
                {'amount': 189, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 199, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 209, 'bedrooms': 3, 'bathrooms': 2},
            ],
        )
        verdicts, _ = match_by_cell(MatchInputs(
            observed_facts=[obs], configured_facts=cells,
        ))
        # Both cells receive all 3 samples as partial evidence.
        for v in verdicts:
            self.assertEqual(v.verdict, RTC.INSUFFICIENT_CONTEXT_TO_COMPARE)
            self.assertEqual(len(v.partial_samples), 3)
            self.assertEqual(len(v.unique_samples), 0)
            # Rationale should mention "partial-evidence".
            self.assertIn('partial-evidence', v.rationale)


class DeterministicConfiguredParserTests(TestCase):
    """Smoke test: the P2 parser produces the fine-grained subject
    shape the matcher expects (sqft_min / sqft_max intervals, per
    service_tier × frequency)."""

    def setUp(self):
        self.org = Organization.objects.create(name='Det Parser Test Org')
        self.snapshot = TenantConfigSnapshot.objects.create(
            org=self.org,
            source_system=TenantConfigSnapshot.SourceSystem.LEADBRIDGE,
            tenant_external_id='det-parser-tenant',
            service_group='',
            contract_version='test',
            raw_config={},
            raw_config_sha256='1' * 64,
            fetched_from_url='',
        )
        self.run = ConfiguredFactParserRun.objects.create(
            snapshot=self.snapshot,
            domain=ObservedBusinessFact.Domain.PRICING,
            parser_version='test-cfg-parser-v3',
            model='test-model',
            status=ConfiguredFactParserRun.Status.RUNNING,
        )

    def test_pricetable_expands_to_row_x_tier_x_frequency_facts(self):
        from apps.conversations.observed_config.pricing.config_parser_deterministic import (
            parse_service_profile,
        )
        pricing_json = {
            'serviceType': 'cleaning',
            'cleaningTypes': [
                {'key': 'regular', 'label': 'Regular Cleaning', 'enabled': True},
                {'key': 'deep', 'label': 'Deep Cleaning', 'enabled': True},
            ],
            'priceTable': [
                {'bed': 2, 'bath': 2, 'sqftMin': 1000, 'sqftMax': 1200,
                 'regular': 169, 'deep': 229},
                {'bed': 3, 'bath': 2, 'sqftMin': 1601, 'sqftMax': 2000,
                 'regular': 209, 'deep': 279},
            ],
            'frequencyDiscounts': [
                {'key': 'once', 'label': 'One Time', 'discount': 0},
                {'key': 'biweekly', 'label': 'Every 2 Weeks', 'discount': 10},
            ],
            'sqftAdjustEnabled': True,
            'extras': [
                {'key': 'oven', 'label': 'Inside Oven', 'price': 35},
            ],
            'hourlyRate': 50, 'minimumHours': 3,
        }
        n = parse_service_profile(
            run=self.run, snapshot=self.snapshot,
            service_profile={
                'id': 'sp-1', 'name': 'House Cleaning',
                'slug': 'cleaning', 'service_group': 'house_cleaning',
            },
            pricing_json=pricing_json,
        )
        # 2 rows × 2 tiers × 2 frequencies = 8 grid + 1 addon + 1 hourly = 10
        self.assertEqual(n, 10)
        facts = ConfiguredBusinessFact.objects.filter(parser_run=self.run)
        self.assertEqual(facts.count(), 10)
        biweekly = facts.filter(
            subject_key_json__contains={'bedrooms': 3, 'service_tier': 'regular', 'frequency': 'biweekly'},
        ).first()
        self.assertIsNotNone(biweekly)
        self.assertEqual(biweekly.value_json['amount'], 190.0)
        self.assertEqual(biweekly.subject_key_json['sqft_min'], 1601)
        self.assertEqual(biweekly.subject_key_json['sqft_max'], 2000)
