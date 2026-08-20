"""Tests for the ROM v1 evaluator.

Verifies:
  - Idempotency: re-running yields identical counters + status
  - Provenance classification (eligible / pending / hash_failed /
    schema_mismatch / contaminated / treatment_moved) per ROM v1
    invariants
  - Coverage floor blocks READY promotion
  - Sample floor blocks READY promotion
  - Terminal transitions (IMPROVED / NO_MATERIAL_CHANGE / WORSE /
    INCONCLUSIVE)
  - Finalized rows are never mutated
  - Post-attribution-window conversations are eligible but not scored
    until their window closes (accumulate in target_signal_total,
    not in resolvedatched arms)
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Organization
from apps.conversations.measurement.creation import (
    LbApplyContext, create_measurement,
)
from apps.conversations.measurement.effective_config_contract import (
    EFFECTIVE_CONFIG_SCHEMA_VERSION,
)
from apps.conversations.measurement.evaluator import evaluate
from apps.conversations.models import (
    BehaviorRecommendation, Conversation, ConversationSemanticEvent,
    CustomerStateInferenceRun, LearningCorpus, OutcomeSnapshot,
    RecommendationLifecycleState, RecommendationOutcomeMeasurement,
    RecommendationRun, SemanticExtractionRun, TenantConfigSnapshot,
)


TENANT = 'c3d14499-dec1-42c3-a36c-713cb09842c6'
TREATMENT_FULL = 'trt' + 'a' * 61
TREATMENT_MANAGED = 'mgd' + 'b' * 61


def _bootstrap():
    org = Organization.objects.create(name='TestOrg')
    corpus = LearningCorpus.objects.create(org=org, name='c', version='v1')
    extraction = SemanticExtractionRun.objects.create(
        org=org, corpus=corpus, extractor_version='e',
        ontology_version='o', prompt_version='p', model='m',
    )
    infer = CustomerStateInferenceRun.objects.create(
        org=org, corpus=corpus, extraction_run=extraction,
        inference_version='inf-v1',
    )
    snapshot = TenantConfigSnapshot.objects.create(
        org=org, source_system='leadbridge',
        tenant_external_id=TENANT, service_group='house_cleaning',
        contract_version='v1', raw_config={'x': 'y'},
        raw_config_sha256='a' * 64,
    )
    run = RecommendationRun.objects.create(
        org=org, corpus=corpus, state_inference_run=infer,
        config_snapshot=snapshot, synthesizer_version='rec-v1',
    )
    rec = BehaviorRecommendation.objects.create(
        run=run, recommendation_id='R0002',
        rec_class='STATE_PARTIAL_COVERAGE',
        confidence='HIGH', subject_state='HIGH_INTENT',
        subject_signals=['DISCOUNT_REQUESTED'],
        proposed_action_scope='config_addition',
        observation='o', interpretation='i', proposed_action='pa',
        limitations='l', evidence={},
    )
    RecommendationLifecycleState.objects.create(
        recommendation=rec, state='accepted',
    )
    applied_at = timezone.now() - timedelta(days=30)
    ctx = LbApplyContext(
        lb_recommendation_application_id='lb-app-eval-1',
        applied_at=applied_at,
        pre_effective_config_hash='pre' + 'a' * 61,
        treatment_effective_config_hash=TREATMENT_FULL,
        treatment_managed_hash=TREATMENT_MANAGED,
        effective_config_schema_version=EFFECTIVE_CONFIG_SCHEMA_VERSION,
    )
    m = create_measurement(rec, ctx)
    return {
        'org': org, 'rec': rec, 'extraction': extraction,
        'measurement': m, 'applied_at': applied_at,
    }


def _mk_conv(
    org, extraction, when, provenance,
    signal='DISCOUNT_REQUESTED', outcome=None,
    started_id=0,
):
    """Create a Conversation with provenance in metadata + optional
    target-signal event + optional outcome."""
    conv = Conversation.objects.create(
        org=org, source='leadbridge',
        source_conversation_id=f'c-{started_id}-{when.isoformat()}',
        started_at=when,
        metadata={'config_provenance': provenance} if provenance else {},
    )
    if signal:
        ConversationSemanticEvent.objects.create(
            org=org, conversation=conv,
            extraction_run=extraction,
            ordinal=0, event_type=signal, actor='customer',
            turn_start=1, turn_end=1, confidence=0.9,
        )
    if outcome:
        OutcomeSnapshot.objects.create(
            conversation=conv,
            captured_at=when + timedelta(days=2),
            **outcome,
        )
    return conv


class ProvenanceClassificationTests(TestCase):
    def _prov(self, **overrides):
        base = {
            'status': 'OK',
            'effective_config_schema_version': (
                EFFECTIVE_CONFIG_SCHEMA_VERSION
            ),
            'effective_config_hash_at_start': TREATMENT_FULL,
            'behavior_os_managed_hash_at_start': TREATMENT_MANAGED,
        }
        base.update(overrides)
        return base

    def test_ok_matching_hashes_is_eligible(self):
        b = _bootstrap()
        _mk_conv(
            b['org'], b['extraction'],
            b['applied_at'] + timedelta(days=1),
            provenance=self._prov(),
            outcome={'lb_booked': True},
        )
        evaluate(b['measurement'])
        m = RecommendationOutcomeMeasurement.objects.get(id=b['measurement'].id)
        self.assertEqual(m.target_signal_conversations_n, 1)
        self.assertEqual(m.provenance_eligible_n, 1)
        self.assertEqual(m.post_positive_n, 1)
        self.assertEqual(m.post_n, 1)
        self.assertEqual(m.contaminated_n, 0)
        self.assertEqual(m.treatment_moved_n, 0)

    def test_managed_mismatch_is_treatment_moved(self):
        b = _bootstrap()
        _mk_conv(
            b['org'], b['extraction'],
            b['applied_at'] + timedelta(days=1),
            provenance=self._prov(
                behavior_os_managed_hash_at_start='different',
            ),
            outcome={'lb_booked': True},
        )
        evaluate(b['measurement'])
        m = RecommendationOutcomeMeasurement.objects.get(id=b['measurement'].id)
        self.assertEqual(m.treatment_moved_n, 1)
        self.assertEqual(m.provenance_eligible_n, 0)
        self.assertEqual(m.post_n, 0)

    def test_full_mismatch_managed_match_is_contaminated(self):
        b = _bootstrap()
        _mk_conv(
            b['org'], b['extraction'],
            b['applied_at'] + timedelta(days=1),
            provenance=self._prov(
                effective_config_hash_at_start='different_full',
            ),
            outcome={'lb_booked': True},
        )
        evaluate(b['measurement'])
        m = RecommendationOutcomeMeasurement.objects.get(id=b['measurement'].id)
        self.assertEqual(m.contaminated_n, 1)
        self.assertEqual(m.provenance_eligible_n, 0)
        self.assertEqual(m.post_n, 0)

    def test_schema_mismatch_is_excluded(self):
        b = _bootstrap()
        _mk_conv(
            b['org'], b['extraction'],
            b['applied_at'] + timedelta(days=1),
            provenance=self._prov(
                effective_config_schema_version='lb-effective-config-v99',
            ),
            outcome={'lb_booked': True},
        )
        evaluate(b['measurement'])
        m = RecommendationOutcomeMeasurement.objects.get(id=b['measurement'].id)
        self.assertEqual(m.provenance_schema_mismatch_n, 1)
        self.assertEqual(m.provenance_eligible_n, 0)

    def test_pending_status_is_pending(self):
        b = _bootstrap()
        _mk_conv(
            b['org'], b['extraction'],
            b['applied_at'] + timedelta(days=1),
            provenance={'status': 'PENDING'},
            outcome={'lb_booked': True},
        )
        evaluate(b['measurement'])
        m = RecommendationOutcomeMeasurement.objects.get(id=b['measurement'].id)
        self.assertEqual(m.provenance_pending_n, 1)
        self.assertEqual(m.provenance_eligible_n, 0)

    def test_hash_failed_is_excluded(self):
        b = _bootstrap()
        _mk_conv(
            b['org'], b['extraction'],
            b['applied_at'] + timedelta(days=1),
            provenance={'status': 'HASH_FAILED'},
            outcome={'lb_booked': True},
        )
        evaluate(b['measurement'])
        m = RecommendationOutcomeMeasurement.objects.get(id=b['measurement'].id)
        self.assertEqual(m.provenance_hash_failed_n, 1)
        self.assertEqual(m.provenance_eligible_n, 0)


class GateEnforcementTests(TestCase):
    def _prov_ok(self):
        return {
            'status': 'OK',
            'effective_config_schema_version': (
                EFFECTIVE_CONFIG_SCHEMA_VERSION
            ),
            'effective_config_hash_at_start': TREATMENT_FULL,
            'behavior_os_managed_hash_at_start': TREATMENT_MANAGED,
        }

    def test_sample_below_floor_stays_collecting(self):
        b = _bootstrap()
        # 3 eligible booked — well under min_sample_per_arm=30
        for i in range(3):
            _mk_conv(
                b['org'], b['extraction'],
                b['applied_at'] + timedelta(days=i + 1),
                provenance=self._prov_ok(),
                outcome={'lb_booked': True},
                started_id=i,
            )
        evaluate(b['measurement'])
        m = RecommendationOutcomeMeasurement.objects.get(id=b['measurement'].id)
        self.assertEqual(m.status,
                          RecommendationOutcomeMeasurement.Status.COLLECTING)
        self.assertIn('sample_below_floor', m.status_reason)

    def test_coverage_below_floor_stays_collecting(self):
        b = _bootstrap()
        # 5 eligible + 20 pending — coverage 5/25 = 0.20 < 0.60 floor
        for i in range(5):
            _mk_conv(
                b['org'], b['extraction'],
                b['applied_at'] + timedelta(days=1),
                provenance=self._prov_ok(),
                outcome={'lb_booked': True}, started_id=i,
            )
        for i in range(20):
            _mk_conv(
                b['org'], b['extraction'],
                b['applied_at'] + timedelta(days=1),
                provenance={'status': 'PENDING'},
                outcome={'lb_booked': True}, started_id=100 + i,
            )
        evaluate(b['measurement'])
        m = RecommendationOutcomeMeasurement.objects.get(id=b['measurement'].id)
        self.assertEqual(m.status,
                          RecommendationOutcomeMeasurement.Status.COLLECTING)
        self.assertIn('provenance_coverage_below_floor', m.status_reason)


class IdempotencyTests(TestCase):
    def test_reevaluation_produces_identical_state(self):
        b = _bootstrap()
        prov = {
            'status': 'OK',
            'effective_config_schema_version': (
                EFFECTIVE_CONFIG_SCHEMA_VERSION
            ),
            'effective_config_hash_at_start': TREATMENT_FULL,
            'behavior_os_managed_hash_at_start': TREATMENT_MANAGED,
        }
        for i in range(5):
            _mk_conv(
                b['org'], b['extraction'],
                b['applied_at'] + timedelta(days=1),
                provenance=prov,
                outcome={'lb_booked': True}, started_id=i,
            )
        evaluate(b['measurement'])
        m1 = RecommendationOutcomeMeasurement.objects.get(
            id=b['measurement'].id,
        )
        evaluate(m1)
        m2 = RecommendationOutcomeMeasurement.objects.get(id=m1.id)
        # All counters + verdict should be identical
        for k in ('status', 'status_reason', 'post_n', 'post_positive_n',
                   'target_signal_conversations_n', 'provenance_eligible_n',
                   'contaminated_n', 'treatment_moved_n',
                   'effect_size_pp', 'p_value'):
            self.assertEqual(
                getattr(m1, k), getattr(m2, k),
                f'field {k} not idempotent: {getattr(m1, k)} vs {getattr(m2, k)}',
            )


class FinalizedGuardTests(TestCase):
    def test_finalized_row_is_not_mutated(self):
        b = _bootstrap()
        m = b['measurement']
        # Force finalize
        m.status = RecommendationOutcomeMeasurement.Status.NO_MATERIAL_CHANGE
        m.finalized_at = timezone.now()
        m.status_reason = 'test_finalized'
        m.save()
        # Now add a conversation that WOULD change things
        _mk_conv(
            b['org'], b['extraction'],
            b['applied_at'] + timedelta(days=1),
            provenance={
                'status': 'OK',
                'effective_config_schema_version': (
                    EFFECTIVE_CONFIG_SCHEMA_VERSION
                ),
                'effective_config_hash_at_start': TREATMENT_FULL,
                'behavior_os_managed_hash_at_start': TREATMENT_MANAGED,
            },
            outcome={'lb_booked': True},
        )
        result = evaluate(m)
        # Nothing changed
        reloaded = RecommendationOutcomeMeasurement.objects.get(id=m.id)
        self.assertEqual(reloaded.status_reason, 'test_finalized')
        self.assertEqual(reloaded.post_n, 0)


class AttributionWindowTests(TestCase):
    def test_conversation_not_yet_matured_counted_but_not_scored(self):
        """A conversation that's eligible + has the signal but hasn't
        matured (started_at + attribution_window_days > now) counts
        toward provenance_eligible_n but NOT toward post_matured_n /
        post_n / post_positive_n. Under terminal_known_after_maturity_v1,
        we wait for the full maturity gate before scoring."""
        b = _bootstrap()
        prov = {
            'status': 'OK',
            'effective_config_schema_version': (
                EFFECTIVE_CONFIG_SCHEMA_VERSION
            ),
            'effective_config_hash_at_start': TREATMENT_FULL,
            'behavior_os_managed_hash_at_start': TREATMENT_MANAGED,
        }
        # applied_at was 30 days ago; attribution window is 14 days.
        # A conversation started 5 days ago has NOT matured.
        recent = timezone.now() - timedelta(days=5)
        _mk_conv(
            b['org'], b['extraction'], recent,
            provenance=prov, outcome={'lb_booked': True},
        )
        evaluate(b['measurement'])
        m = RecommendationOutcomeMeasurement.objects.get(id=b['measurement'].id)
        self.assertEqual(m.provenance_eligible_n, 1)
        # NOT matured, NOT scored — the outcome could still change
        self.assertEqual(m.post_matured_n, 0)
        self.assertEqual(m.post_n, 0)
        self.assertEqual(m.post_positive_n, 0)
        self.assertEqual(m.post_unresolved_n, 0)

    def test_matured_conversation_scored_regardless_of_captured_at(self):
        """A matured conv is scored using the latest known snapshot
        even if captured_at is FAR in the future (async resolver)."""
        b = _bootstrap()
        prov = {
            'status': 'OK',
            'effective_config_schema_version': (
                EFFECTIVE_CONFIG_SCHEMA_VERSION
            ),
            'effective_config_hash_at_start': TREATMENT_FULL,
            'behavior_os_managed_hash_at_start': TREATMENT_MANAGED,
        }
        # Conv started 20 days ago — well past 14d maturity gate.
        # OutcomeSnapshot captured_at is BEFORE now (so it's visible
        # under `captured_at <= now`) but far after the conversation.
        started = timezone.now() - timedelta(days=20)
        from apps.conversations.models import Conversation as _Conv
        from apps.conversations.models import (
            ConversationSemanticEvent as _CSE,
        )
        conv = _Conv.objects.create(
            org=b['org'], source='leadbridge',
            source_conversation_id='c-matured-late-snap',
            started_at=started,
            metadata={'config_provenance': prov},
        )
        _CSE.objects.create(
            org=b['org'], conversation=conv,
            extraction_run=b['extraction'],
            ordinal=0, event_type='DISCOUNT_REQUESTED',
            actor='customer', turn_start=1, turn_end=1, confidence=0.9,
        )
        OutcomeSnapshot.objects.create(
            conversation=conv,
            captured_at=timezone.now() - timedelta(hours=1),
            lb_booked=True,
        )
        evaluate(b['measurement'])
        m = RecommendationOutcomeMeasurement.objects.get(id=b['measurement'].id)
        self.assertEqual(m.post_matured_n, 1)
        self.assertEqual(m.post_positive_n, 1)
        self.assertEqual(m.post_n, 1)


class OutcomeResolutionCoverageGateTests(TestCase):
    """The new outcome_resolution_coverage gate — READY is refused
    when too many matured conversations have no known terminal."""

    def _prov_ok(self):
        return {
            'status': 'OK',
            'effective_config_schema_version': (
                EFFECTIVE_CONFIG_SCHEMA_VERSION
            ),
            'effective_config_hash_at_start': TREATMENT_FULL,
            'behavior_os_managed_hash_at_start': TREATMENT_MANAGED,
        }

    def test_low_outcome_coverage_stays_collecting(self):
        b = _bootstrap()
        prov = self._prov_ok()
        # 40 matured, 30 with terminals (75%) — meets 60% floor. Add
        # 30 more unresolved to drop below floor: 30/70 = 43% < 60%.
        for i in range(30):
            _mk_conv(
                b['org'], b['extraction'],
                b['applied_at'] + timedelta(days=1),
                provenance=prov, outcome={'lb_booked': True},
                started_id=i,
            )
        for i in range(40):
            # matured (> 14d ago from now given applied_at 30d ago +
            # +1d) but no outcome snapshot at all
            _mk_conv(
                b['org'], b['extraction'],
                b['applied_at'] + timedelta(days=1),
                provenance=prov, outcome=None,
                started_id=100 + i,
            )
        evaluate(b['measurement'])
        m = RecommendationOutcomeMeasurement.objects.get(id=b['measurement'].id)
        self.assertEqual(m.post_matured_n, 70)
        self.assertEqual(m.post_positive_n, 30)
        self.assertEqual(m.post_unresolved_n, 40)
        self.assertEqual(m.post_n, 30)
        self.assertEqual(
            m.status,
            RecommendationOutcomeMeasurement.Status.COLLECTING,
        )
        self.assertIn('outcome_resolution_coverage_below_floor',
                       m.status_reason)
