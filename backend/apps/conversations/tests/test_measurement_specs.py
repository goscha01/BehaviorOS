"""Tests for the MeasurementSpec v1 registry + resolver + freezing.

MeasurementSpec is deterministic — the LLM never touches it. These
tests exercise:

- The v1 spec applies to STATE_COVERAGE_GAP / STATE_PARTIAL_COVERAGE
  recs whose target signal is HIGH_INTENT (matches proposal_synthesis
  v1 envelope exactly, so every applyable rec has a spec).
- Recs outside that envelope raise NoMeasurementSpec.
- Freezing instantiates the target-signal sentinel from the rec.
- to_dict() output is stable and JSON-serializable (persistence
  contract).
- Existing spec_key semantics never change silently (registry-shape
  guard).
"""

from __future__ import annotations

import json

from django.test import TestCase

from apps.accounts.models import Organization
from apps.conversations.measurement.specs import (
    HIGH_INTENT_SIGNALS, HIGH_INTENT_SIGNAL_COVERAGE_V1,
    MEASUREMENT_SPEC_MODULE_VERSION, MEASUREMENT_SPEC_REGISTRY,
    FrozenMeasurementSpec, MeasurementSpec, NoMeasurementSpec,
    OutcomeTerminal, get_spec, resolve_spec_for_recommendation,
)
from apps.conversations.models import (
    BehaviorRecommendation, CustomerStateInferenceRun, LearningCorpus,
    RecommendationRun, SemanticExtractionRun, TenantConfigSnapshot,
)


def _mk_rec(rec_class='STATE_PARTIAL_COVERAGE',
             signals=('DISCOUNT_REQUESTED',)):
    org = Organization.objects.create(name='TestOrg')
    corpus = LearningCorpus.objects.create(org=org, name='c', version='v1')
    extraction = SemanticExtractionRun.objects.create(
        org=org, corpus=corpus,
        extractor_version='e', ontology_version='o',
        prompt_version='p', model='m',
    )
    infer = CustomerStateInferenceRun.objects.create(
        org=org, corpus=corpus, extraction_run=extraction,
        inference_version='inf-v1',
    )
    snapshot = TenantConfigSnapshot.objects.create(
        org=org, source_system='leadbridge',
        tenant_external_id='tenant-x', service_group='house_cleaning',
        contract_version='v1', raw_config={'x': 'y'},
        raw_config_sha256='a' * 64,
    )
    run = RecommendationRun.objects.create(
        org=org, corpus=corpus,
        state_inference_run=infer,
        config_snapshot=snapshot,
        synthesizer_version='rec-v1',
    )
    return BehaviorRecommendation.objects.create(
        run=run, recommendation_id='R0002',
        rec_class=rec_class,
        confidence='HIGH',
        subject_state='HIGH_INTENT',
        subject_signals=list(signals),
        proposed_action_scope='config_addition',
        observation='obs', interpretation='interp',
        proposed_action='pa', limitations='lim',
        evidence={},
    )


class RegistryShapeTests(TestCase):
    """These tests fail if v1 spec semantics are silently mutated —
    they're intentionally strict to force a NEW spec_key on any
    semantics change (not an in-place edit)."""

    def test_v1_module_version_locked(self):
        # If module version changes, some existing spec_key was mutated.
        # That's forbidden — add a new spec_key instead.
        self.assertEqual(MEASUREMENT_SPEC_MODULE_VERSION,
                          'measurement-spec-v1')

    def test_v1_spec_key_stable(self):
        self.assertIn('high_intent_signal_coverage.v1',
                       MEASUREMENT_SPEC_REGISTRY)
        spec = MEASUREMENT_SPEC_REGISTRY['high_intent_signal_coverage.v1']
        self.assertIs(spec, HIGH_INTENT_SIGNAL_COVERAGE_V1)

    def test_v1_attribution_window_is_days_not_turns(self):
        # Guardrail: user was explicit that windows are elapsed time,
        # never turn counts.
        outcome = HIGH_INTENT_SIGNAL_COVERAGE_V1.primary_outcome
        self.assertEqual(outcome.kind,
                          'reaches_positive_terminal_within_days')
        self.assertEqual(outcome.attribution_window_days, 14)
        self.assertEqual(outcome.baseline_window_days, 90)
        self.assertNotIn('turn', outcome.kind)

    def test_v1_outcome_semantics_locked(self):
        # v1 uses maturity-gated scoring with the latest known
        # terminal. v2 (once business-event timestamps exist) will
        # switch to `terminal_event_occurred_within_window`.
        outcome = HIGH_INTENT_SIGNAL_COVERAGE_V1.primary_outcome
        self.assertEqual(outcome.outcome_semantics,
                          'terminal_known_after_maturity_v1')

    def test_v1_positive_terminals_include_booking(self):
        positive = HIGH_INTENT_SIGNAL_COVERAGE_V1.primary_outcome.positive_terminal_events
        self.assertIn(OutcomeTerminal.LB_BOOKED, positive)
        self.assertIn(OutcomeTerminal.SF_BOOKED, positive)
        self.assertIn(OutcomeTerminal.SF_COMPLETED, positive)

    def test_v1_verdict_gates_have_all_required(self):
        gates = HIGH_INTENT_SIGNAL_COVERAGE_V1.verdict_gates
        # Not just p<.05 — sample floor + effect size + significance +
        # provenance coverage + outcome-resolution coverage.
        self.assertGreaterEqual(gates.min_sample_per_arm, 20)
        self.assertGreaterEqual(gates.min_effect_size_pp, 5.0)
        self.assertGreater(gates.uncertainty_significance_alpha, 0.0)
        self.assertLess(gates.uncertainty_significance_alpha, 0.2)
        self.assertGreaterEqual(gates.min_provenance_coverage, 0.5)
        self.assertLessEqual(gates.min_provenance_coverage, 1.0)
        self.assertGreaterEqual(
            gates.min_outcome_resolution_coverage, 0.5,
        )
        self.assertLessEqual(
            gates.min_outcome_resolution_coverage, 1.0,
        )
        self.assertGreater(gates.max_window_days_for_inconclusive,
                            gates.min_sample_per_arm)


class ResolverTests(TestCase):
    def test_partial_coverage_high_intent_signal_resolves(self):
        rec = _mk_rec(rec_class='STATE_PARTIAL_COVERAGE',
                       signals=('DISCOUNT_REQUESTED',))
        spec = resolve_spec_for_recommendation(rec)
        self.assertEqual(spec.spec_key, 'high_intent_signal_coverage.v1')

    def test_coverage_gap_high_intent_signal_resolves(self):
        rec = _mk_rec(rec_class='STATE_COVERAGE_GAP',
                       signals=('BOOKING_REQUESTED',))
        spec = resolve_spec_for_recommendation(rec)
        self.assertEqual(spec.spec_key, 'high_intent_signal_coverage.v1')

    def test_wrong_rec_class_raises(self):
        rec = _mk_rec(rec_class='OBSERVED_STATE_INSIGHT',
                       signals=('DISCOUNT_REQUESTED',))
        with self.assertRaises(NoMeasurementSpec):
            resolve_spec_for_recommendation(rec)

    def test_non_high_intent_signal_raises(self):
        rec = _mk_rec(rec_class='STATE_PARTIAL_COVERAGE',
                       signals=('SERVICE_DETAILS_PROVIDED',))
        with self.assertRaises(NoMeasurementSpec):
            resolve_spec_for_recommendation(rec)

    def test_no_signals_raises(self):
        rec = _mk_rec(rec_class='STATE_PARTIAL_COVERAGE', signals=())
        with self.assertRaises(NoMeasurementSpec):
            resolve_spec_for_recommendation(rec)

    def test_get_spec_returns_none_for_retired_key(self):
        self.assertIsNone(get_spec('high_intent_signal_coverage.v0'))
        self.assertIsNotNone(get_spec('high_intent_signal_coverage.v1'))


class FreezeTests(TestCase):
    def test_freeze_instantiates_target_signal(self):
        rec = _mk_rec(signals=('AVAILABILITY_REQUESTED',))
        spec = resolve_spec_for_recommendation(rec)
        frozen = spec.freeze_for_recommendation(rec)
        self.assertIsInstance(frozen, FrozenMeasurementSpec)
        self.assertEqual(frozen.spec_key, spec.spec_key)
        self.assertEqual(frozen.cohort_entry.signal, 'AVAILABILITY_REQUESTED')
        self.assertEqual(frozen.cohort_entry.kind,
                          'signal_observed_in_conversation')

    def test_freeze_target_outside_applies_to_signals_raises(self):
        # Constructed edge case: rec somehow bypassed resolver but signal
        # doesn't belong to the spec's applicability. freeze must catch it.
        rec = _mk_rec(signals=('SERVICE_DETAILS_PROVIDED',))
        with self.assertRaises(NoMeasurementSpec):
            HIGH_INTENT_SIGNAL_COVERAGE_V1.freeze_for_recommendation(rec)

    def test_freeze_no_signals_raises(self):
        rec = _mk_rec(signals=())
        with self.assertRaises(NoMeasurementSpec):
            HIGH_INTENT_SIGNAL_COVERAGE_V1.freeze_for_recommendation(rec)


class PersistenceContractTests(TestCase):
    """FrozenMeasurementSpec.to_dict() is an on-disk contract. Fields
    persisted on the measurement row must remain stable so future
    evaluators can re-score historical measurements deterministically."""

    def test_to_dict_shape_and_json_roundtrip(self):
        rec = _mk_rec(signals=('DISCOUNT_REQUESTED',))
        frozen = HIGH_INTENT_SIGNAL_COVERAGE_V1.freeze_for_recommendation(rec)
        d = frozen.to_dict()
        # Must roundtrip through JSON without loss.
        d_roundtrip = json.loads(json.dumps(d))
        self.assertEqual(d, d_roundtrip)
        # Locked-shape contract:
        self.assertEqual(d['spec_key'], 'high_intent_signal_coverage.v1')
        self.assertEqual(d['version'], 'measurement-spec-v1')
        self.assertEqual(d['cohort_entry']['signal'], 'DISCOUNT_REQUESTED')
        self.assertEqual(
            d['primary_outcome']['attribution_window_days'], 14,
        )
        self.assertEqual(
            d['primary_outcome']['baseline_window_days'], 90,
        )
        self.assertEqual(
            d['primary_outcome']['outcome_semantics'],
            'terminal_known_after_maturity_v1',
        )
        self.assertIn('LB_BOOKED',
                       d['primary_outcome']['positive_terminal_events'])
        self.assertIn('LEAD_MISMATCH', d['exclusions']['tokens'])
        for k in ('min_sample_per_arm', 'min_effect_size_pp',
                   'uncertainty_significance_alpha',
                   'min_provenance_coverage',
                   'min_outcome_resolution_coverage',
                   'max_window_days_for_inconclusive'):
            self.assertIn(k, d['verdict_gates'])


class HighIntentSignalSetTests(TestCase):
    """The set of HIGH_INTENT signals must stay aligned with the
    CustomerState v1 primitive used by 1B-6 (see project memory)."""

    def test_high_intent_signal_set_matches_1b6_primitives(self):
        # Any change here must be intentional and reflected in 1B-6.
        self.assertEqual(HIGH_INTENT_SIGNALS, frozenset({
            'DISCOUNT_REQUESTED',
            'BOOKING_REQUESTED',
            'AVAILABILITY_REQUESTED',
            'PRICE_REQUESTED',
        }))
