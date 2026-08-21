"""Deterministic pricing matcher acceptance tests (Phase 5).

Every verdict from the 2026-08-21 reviewer directive gets at least
one test that constructs the minimum observed + configured facts
required to produce that verdict, calls the matcher directly, and
asserts the emitted MatchOutcome.

Also covers the specific refinements from the directive:

  - OBSERVED_NOT_CONFIGURED requires *no compatible rule*, not just
    a missing service name.
  - INSUFFICIENT_CONTEXT_TO_COMPARE requires ≥1 plausible candidate.
  - Raw square_footage on the observed side is compared to the
    configured sqft_min/sqft_max INTERVAL, not to a bucket enum.

No LLM. No database round-trip either — the matcher operates on
ObservedBusinessFact / ConfiguredBusinessFact instances that we
construct in-memory (via .save() so the models are fully hydrated
including UUIDs, but no reconstruction pipeline is exercised).
"""

from __future__ import annotations

import uuid

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Organization
from apps.conversations.models import (
    ConfiguredBusinessFact, ConfiguredFactParserRun,
    LearningCorpus, ObservedBusinessFact,
    ObservedFactExtractionRun, ReconstructedBusinessFact,
    TenantConfigSnapshot,
)
from apps.conversations.observed_config.base import canonical_subject_key
from apps.conversations.reconstruction.pricing_matcher import (
    MatchInputs, match_all, match_one,
)


RTC = ReconstructedBusinessFact.RelationshipToConfig


class PricingMatcherAcceptanceTests(TestCase):
    """One test per verdict + the refinements the reviewer called out."""

    def setUp(self):
        self.org = Organization.objects.create(name='Pricing Matcher Test Org')
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

    def _observed(self, *, subject: dict, samples: list[dict],
                   median: float | None = None, p25: float | None = None,
                   p75: float | None = None, support_n: int = 10) -> ObservedBusinessFact:
        _, sha, dims = canonical_subject_key(subject)
        value: dict = {
            'fact_type': 'quoted_price',
            'currency': 'USD',
            'dimension_samples': samples,
        }
        if median is not None:
            value['amount_stats'] = {
                'support_n': support_n,
                'median': median,
                'p25': p25 if p25 is not None else median,
                'p75': p75 if p75 is not None else median,
                'min': p25 if p25 is not None else median,
                'max': p75 if p75 is not None else median,
                'mean': median,
            }
        return ObservedBusinessFact.objects.create(
            org=self.org, corpus=self.corpus, extraction_run=self.obs_run,
            domain=ObservedBusinessFact.Domain.PRICING,
            fact_type='quoted_price',
            subject_key_json=subject,
            subject_key_dimensions=dims,
            subject_key_hash=sha,
            value_json=value,
            support_n=support_n,
        )

    def _configured(self, *, subject: dict, amount: float,
                     source_ref: str = 'test') -> ConfiguredBusinessFact:
        _, sha, dims = canonical_subject_key(subject)
        return ConfiguredBusinessFact.objects.create(
            snapshot=self.snapshot, parser_run=self.cfg_run,
            domain=ObservedBusinessFact.Domain.PRICING,
            fact_type='quoted_price',
            subject_key_json=subject,
            subject_key_dimensions=dims,
            subject_key_hash=sha,
            value_json={'amount': amount, 'currency': 'USD'},
            source_pointer={'ref': source_ref},
            parser_confidence=1.0,
        )

    # ─── MATCH ──────────────────────────────────────────────────

    def test_match_when_observed_median_within_tolerance(self):
        """Fully-resolved observed context → one configured candidate
        → observed median within ±10% → MATCH."""
        cfg = self._configured(
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
                {'amount': 189, 'square_footage': 1800, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 195, 'square_footage': 1750, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 189, 'square_footage': 1900, 'bedrooms': 3, 'bathrooms': 2},
            ],
            median=189, p25=189, p75=195, support_n=8,
        )
        outcome = match_one(obs, [cfg])
        self.assertEqual(outcome.verdict, RTC.MATCH)
        self.assertEqual(outcome.matched_configured_fact_id, str(cfg.id))
        self.assertTrue(outcome.price_comparison['within_tolerance'])

    # ─── DIFFERS_FROM_CONFIG ───────────────────────────────────

    def test_differs_from_config_when_median_outside_tolerance(self):
        cfg = self._configured(
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
            median=249, p25=249, p75=259, support_n=8,
        )
        outcome = match_one(obs, [cfg])
        self.assertEqual(outcome.verdict, RTC.DIFFERS_FROM_CONFIG)
        self.assertEqual(outcome.matched_configured_fact_id, str(cfg.id))
        self.assertFalse(outcome.price_comparison['within_tolerance'])

    # ─── OBSERVED_NOT_CONFIGURED ───────────────────────────────

    def test_observed_not_configured_when_no_service_in_config(self):
        """No configured rule at all → OBSERVED_NOT_CONFIGURED."""
        obs = self._observed(
            subject={
                'service': 'cleaning', 'pricing_basis': 'flat_job',
            },
            samples=[{'amount': 169} for _ in range(5)],
            median=169, p25=169, p75=189, support_n=5,
        )
        outcome = match_one(obs, [])
        self.assertEqual(outcome.verdict, RTC.OBSERVED_NOT_CONFIGURED)

    def test_observed_not_configured_when_service_exists_but_no_compatible_rule(self):
        """Per the reviewer refinement: OBSERVED_NOT_CONFIGURED must
        fire when the observed CONTEXT has no compatible configured
        rule — not merely when the service name is missing.

        Configured has regular cleaning at bed=3/bath=2 flat_job only.
        Observed is `regular_cleaning + biweekly + addon=oven`, which
        is a valid same-service quote but no configured rule covers
        it."""
        self._configured(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1000, 'sqft_max': 1200,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=189,
        )
        obs = self._observed(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'frequency': 'biweekly',
                'addons': ['oven'],
                'pricing_basis': 'addon_flat',
            },
            samples=[{'amount': 35} for _ in range(5)],
            median=35, p25=35, p75=35, support_n=5,
        )
        outcome = match_one(obs, [
            c for c in ConfiguredBusinessFact.objects.filter(parser_run=self.cfg_run)
        ])
        self.assertEqual(outcome.verdict, RTC.OBSERVED_NOT_CONFIGURED)
        # And the rationale should mention the incompatible dims.
        self.assertIn('cleaning', outcome.rationale)

    # ─── INSUFFICIENT_CONTEXT_TO_COMPARE ───────────────────────

    def test_insufficient_context_when_multiple_candidates_and_observed_lacks_sqft(self):
        """Per the reviewer refinement: fires only when >=1 plausible
        candidate exists AND observed context can't choose."""
        # Two configured rules for regular cleaning, different sqft
        # bands — same bed/bath.
        self._configured(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1000, 'sqft_max': 1200,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=169,
        )
        self._configured(
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
                {'amount': 189, 'bedrooms': 3, 'bathrooms': 2},   # no sqft
                {'amount': 209, 'bedrooms': 3, 'bathrooms': 2},
            ],
            median=199, p25=189, p75=209, support_n=6,
        )
        cfg_list = list(ConfiguredBusinessFact.objects.filter(parser_run=self.cfg_run))
        outcome = match_one(obs, cfg_list)
        self.assertEqual(outcome.verdict, RTC.INSUFFICIENT_CONTEXT_TO_COMPARE)
        self.assertGreaterEqual(len(outcome.candidate_configured_fact_ids), 2)
        self.assertIn('square_footage', outcome.missing_observed_dimensions)

    # ─── VARIABLE_CONTEXT_DEPENDENT ────────────────────────────

    def test_variable_context_dependent_when_observed_iqr_wide(self):
        """Even with a single configured candidate, if the observed
        distribution has IQR/median >= 25% the matcher refuses a
        clean comparison."""
        cfg = self._configured(
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
            median=189, p25=149, p75=259, support_n=8,
        )
        outcome = match_one(obs, [cfg])
        self.assertEqual(outcome.verdict, RTC.VARIABLE_CONTEXT_DEPENDENT)

    # ─── CONFIGURED_NOT_OBSERVED ───────────────────────────────

    def test_configured_not_observed_via_match_all(self):
        """A configured rule that no observed fact claims → returned
        in the orphaned bucket, ready for the reconstructor to emit
        as CONFIGURED_NOT_OBSERVED."""
        cfg = self._configured(
            subject={
                'service': 'carpet', 'pricing_basis': 'flat_job',
            },
            amount=99,
        )
        obs = self._observed(
            subject={'service': 'cleaning', 'pricing_basis': 'flat_job'},
            samples=[{'amount': 189} for _ in range(5)],
            median=189, p25=189, p75=189, support_n=5,
        )
        _, orphaned = match_all(MatchInputs(
            observed_facts=[obs],
            configured_facts=[cfg],
        ))
        self.assertEqual(len(orphaned), 1)
        self.assertEqual(str(orphaned[0].id), str(cfg.id))

    # ─── Refinement: sqft interval containment ─────────────────

    def test_sqft_interval_containment_narrows_to_one_candidate(self):
        """Observed samples carry raw sqft — matcher does
        `sqft ∈ [sqft_min, sqft_max]` interval check against each
        configured band and resolves to the one band that fits."""
        cfg_small = self._configured(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1000, 'sqft_max': 1200,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=169, source_ref='small',
        )
        cfg_large = self._configured(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'sqft_min': 1601, 'sqft_max': 2000,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            amount=209, source_ref='large',
        )
        obs = self._observed(
            subject={
                'service': 'cleaning', 'service_tier': 'regular',
                'bedrooms': 3, 'bathrooms': 2,
                'frequency': 'once', 'pricing_basis': 'flat_job',
            },
            samples=[
                {'amount': 209, 'square_footage': 1800, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 199, 'square_footage': 1750, 'bedrooms': 3, 'bathrooms': 2},
                {'amount': 219, 'square_footage': 1900, 'bedrooms': 3, 'bathrooms': 2},
            ],
            median=209, p25=199, p75=219, support_n=6,
        )
        outcome = match_one(obs, [cfg_small, cfg_large])
        self.assertEqual(outcome.verdict, RTC.MATCH)
        self.assertEqual(
            outcome.matched_configured_fact_id, str(cfg_large.id),
            'sqft=1800 should resolve to the [1601, 2000] band, not the [1000, 1200] band',
        )

    # ─── INSUFFICIENT_EVIDENCE (legacy) ────────────────────────

    def test_insufficient_evidence_below_min_support(self):
        """support_n < 3 → INSUFFICIENT_EVIDENCE regardless of match."""
        cfg = self._configured(
            subject={'service': 'cleaning', 'pricing_basis': 'flat_job'},
            amount=189,
        )
        obs = self._observed(
            subject={'service': 'cleaning', 'pricing_basis': 'flat_job'},
            samples=[{'amount': 189}],
            median=189, p25=189, p75=189, support_n=1,
        )
        outcome = match_one(obs, [cfg])
        self.assertEqual(outcome.verdict, RTC.INSUFFICIENT_EVIDENCE)


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
        # 2 rows × 2 tiers × 2 frequencies = 8 grid facts;
        # + 1 oven addon fact
        # + 1 hourly fact
        # = 10
        self.assertEqual(n, 10)
        facts = ConfiguredBusinessFact.objects.filter(parser_run=self.run)
        self.assertEqual(facts.count(), 10)

        # Grid fact for bed=3 bath=2 sqft=[1601,2000] regular biweekly
        # should have amount 209 * 0.9 rounded to $5 = 190.
        biweekly = facts.filter(
            subject_key_json__contains={'bedrooms': 3, 'service_tier': 'regular', 'frequency': 'biweekly'},
        ).first()
        self.assertIsNotNone(biweekly)
        self.assertEqual(biweekly.value_json['amount'], 190.0)
        self.assertEqual(biweekly.value_json['base_amount'], 209)
        self.assertEqual(biweekly.subject_key_json['sqft_min'], 1601)
        self.assertEqual(biweekly.subject_key_json['sqft_max'], 2000)

        # Addon fact.
        oven = facts.filter(subject_key_json__contains={'addons': ['oven']}).first()
        self.assertIsNotNone(oven)
        self.assertEqual(oven.value_json['amount'], 35)
        self.assertEqual(oven.subject_key_json['pricing_basis'], 'addon_flat')

        # Hourly fact.
        hourly = facts.filter(subject_key_json__contains={'pricing_basis': 'hourly_per_cleaner'}).first()
        self.assertIsNotNone(hourly)
        self.assertEqual(hourly.value_json['amount'], 50)
        self.assertEqual(hourly.value_json['minimum_hours'], 3)
