"""Tests for MeasurementCreationService and the /measurement API endpoint.

Focus is on the FROZEN contract semantics:
  - Spec is deterministically resolved + frozen at creation
  - Baseline cohort is computed from pre-application conversations
    matching cohort_entry + scored via OutcomeSnapshots
  - Idempotency per lb_recommendation_application_id
  - Schema-version mismatch is recorded but does not fail creation
  - Cross-tenant reads return 404
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Organization
from apps.conversations.measurement.creation import (
    LbApplyContext, MeasurementCreationError, create_measurement,
)
from apps.conversations.measurement.effective_config_contract import (
    EFFECTIVE_CONFIG_SCHEMA_VERSION,
)
from apps.conversations.models import (
    BehaviorRecommendation, Conversation, ConversationSemanticEvent,
    CustomerStateInferenceRun, LearningCorpus, OutcomeSnapshot,
    RecommendationLifecycleState, RecommendationOutcomeMeasurement,
    RecommendationRun, SemanticExtractionRun, TenantConfigSnapshot,
)


TENANT = 'c3d14499-dec1-42c3-a36c-713cb09842c6'


def _bootstrap_rec(
    rec_class='STATE_PARTIAL_COVERAGE',
    signals=('DISCOUNT_REQUESTED',),
    lifecycle_state='accepted',
):
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
        tenant_external_id=TENANT, service_group='house_cleaning',
        contract_version='v1', raw_config={'x': 'y'},
        raw_config_sha256='a' * 64,
    )
    run = RecommendationRun.objects.create(
        org=org, corpus=corpus,
        state_inference_run=infer,
        config_snapshot=snapshot,
        synthesizer_version='rec-v1',
    )
    rec = BehaviorRecommendation.objects.create(
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
    if lifecycle_state:
        RecommendationLifecycleState.objects.create(
            recommendation=rec, state=lifecycle_state,
        )
    return {'org': org, 'rec': rec, 'extraction': extraction}


def _mk_ctx(applied_at=None, **overrides):
    if applied_at is None:
        applied_at = timezone.now()
    base = dict(
        lb_recommendation_application_id='lb-app-uuid-1',
        applied_at=applied_at,
        pre_effective_config_hash='pre' + 'a' * 61,
        treatment_effective_config_hash='trt' + 'b' * 61,
        treatment_managed_hash='mgd' + 'c' * 61,
        effective_config_schema_version=EFFECTIVE_CONFIG_SCHEMA_VERSION,
    )
    base.update(overrides)
    return LbApplyContext(**base)


class CreationFreezeTests(TestCase):
    def test_creates_baseline_frozen_row_with_frozen_spec(self):
        b = _bootstrap_rec()
        ctx = _mk_ctx()
        row = create_measurement(b['rec'], ctx)
        self.assertEqual(
            row.status,
            RecommendationOutcomeMeasurement.Status.BASELINE_FROZEN,
        )
        self.assertEqual(row.measurement_spec_key,
                          'high_intent_signal_coverage.v1')
        self.assertEqual(row.target_signal, 'DISCOUNT_REQUESTED')
        # frozen_spec_json is the serialized on-disk contract
        self.assertEqual(row.frozen_spec_json['spec_key'],
                          'high_intent_signal_coverage.v1')
        self.assertEqual(
            row.frozen_spec_json['cohort_entry']['signal'],
            'DISCOUNT_REQUESTED',
        )
        # applied_at + max_window drives deadline (v1 spec = 90 days)
        expected_deadline = ctx.applied_at + timedelta(days=90)
        self.assertAlmostEqual(
            (row.measurement_deadline_at - expected_deadline).total_seconds(),
            0, delta=5,
        )

    def test_idempotent_per_lb_application_id(self):
        b = _bootstrap_rec()
        ctx = _mk_ctx()
        first = create_measurement(b['rec'], ctx)
        second = create_measurement(b['rec'], ctx)
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            RecommendationOutcomeMeasurement.objects.count(), 1,
        )

    def test_ineligible_rec_class_raises(self):
        b = _bootstrap_rec(rec_class='OBSERVED_STATE_INSIGHT')
        with self.assertRaises(MeasurementCreationError):
            create_measurement(b['rec'], _mk_ctx())

    def test_non_high_intent_signal_raises(self):
        b = _bootstrap_rec(signals=('SERVICE_DETAILS_PROVIDED',))
        with self.assertRaises(MeasurementCreationError):
            create_measurement(b['rec'], _mk_ctx())

    def test_schema_version_mismatch_is_recorded_not_rejected(self):
        # Evaluator later applies schema-mismatch cohort exclusion,
        # but creation must accept the row so the tenant's Apply
        # doesn't fail on a version mismatch we can still handle.
        b = _bootstrap_rec()
        ctx = _mk_ctx(
            effective_config_schema_version='lb-effective-config-v99',
        )
        row = create_measurement(b['rec'], ctx)
        self.assertEqual(
            row.effective_config_schema_version,
            'lb-effective-config-v99',
        )


class BaselineCohortTests(TestCase):
    def test_baseline_cohort_scores_positive_and_negative(self):
        b = _bootstrap_rec()
        applied_at = timezone.now()
        # 3 conversations, all matured (started 30 days ago >> 14d gate).
        # 1 positive (LB_BOOKED), 1 negative (LB_LOST), 1 no-signal.
        for i, (has_signal, positive, negative) in enumerate([
            (True, True, False),
            (True, False, True),
            (False, False, False),  # no target signal → not in cohort
        ]):
            conv = Conversation.objects.create(
                org=b['org'], source='leadbridge',
                source_conversation_id=f'c{i}',
                started_at=applied_at - timedelta(days=30),
            )
            if has_signal:
                ConversationSemanticEvent.objects.create(
                    org=b['org'], conversation=conv,
                    extraction_run=b['extraction'],
                    ordinal=0, event_type='DISCOUNT_REQUESTED',
                    actor='customer', turn_start=1, turn_end=1,
                    confidence=0.9,
                )
            # captured_at is now IRRELEVANT under
            # terminal_known_after_maturity_v1 — the maturity gate
            # (started_at + 14d) is what decides eligibility. Pick a
            # far-future captured_at to prove the point.
            if positive:
                OutcomeSnapshot.objects.create(
                    conversation=conv,
                    captured_at=applied_at + timedelta(days=180),
                    lb_booked=True,
                )
            if negative:
                OutcomeSnapshot.objects.create(
                    conversation=conv,
                    captured_at=applied_at + timedelta(days=180),
                    lb_lost=True,
                )

        row = create_measurement(b['rec'], _mk_ctx(applied_at=applied_at))
        # Cohort membership = 2 (both signal-matched convs).
        # Both are matured. 1 positive, 1 negative, 0 unresolved.
        self.assertEqual(len(row.pre_cohort_conversation_ids), 2)
        self.assertEqual(row.pre_matured_n, 2)
        self.assertEqual(row.pre_n, 2)
        self.assertEqual(row.pre_positive_n, 1)
        self.assertEqual(row.pre_negative_n, 1)
        self.assertEqual(row.pre_unresolved_n, 0)
        self.assertAlmostEqual(row.pre_rate, 0.5, places=6)

    def test_immature_cohort_member_counted_in_membership_but_not_scored(self):
        """Conversation started too recently to have matured belongs
        to the frozen cohort membership but doesn't contribute to
        matured/positive/negative/unresolved."""
        b = _bootstrap_rec()
        applied_at = timezone.now()
        # Conv started 5 days ago — inside 14d maturity gate.
        conv = Conversation.objects.create(
            org=b['org'], source='leadbridge',
            source_conversation_id='c-immature',
            started_at=applied_at - timedelta(days=5),
        )
        ConversationSemanticEvent.objects.create(
            org=b['org'], conversation=conv,
            extraction_run=b['extraction'],
            ordinal=0, event_type='DISCOUNT_REQUESTED',
            actor='customer', turn_start=1, turn_end=1, confidence=0.9,
        )
        OutcomeSnapshot.objects.create(
            conversation=conv, captured_at=applied_at,
            lb_booked=True,
        )
        row = create_measurement(b['rec'], _mk_ctx(applied_at=applied_at))
        self.assertEqual(len(row.pre_cohort_conversation_ids), 1)
        self.assertEqual(row.pre_matured_n, 0)
        self.assertEqual(row.pre_n, 0)
        self.assertEqual(row.pre_positive_n, 0)
        self.assertEqual(row.pre_unresolved_n, 0)
        self.assertIsNone(row.pre_rate)

    def test_matured_but_unresolved_counted_separately(self):
        """Matured conv with no OutcomeSnapshot → unresolved (NOT
        counted as negative). Slow resolver coverage must not become
        a rate bias."""
        b = _bootstrap_rec()
        applied_at = timezone.now()
        conv = Conversation.objects.create(
            org=b['org'], source='leadbridge',
            source_conversation_id='c-unresolved',
            started_at=applied_at - timedelta(days=30),
        )
        ConversationSemanticEvent.objects.create(
            org=b['org'], conversation=conv,
            extraction_run=b['extraction'],
            ordinal=0, event_type='DISCOUNT_REQUESTED',
            actor='customer', turn_start=1, turn_end=1, confidence=0.9,
        )
        # No OutcomeSnapshot created.
        row = create_measurement(b['rec'], _mk_ctx(applied_at=applied_at))
        self.assertEqual(row.pre_matured_n, 1)
        self.assertEqual(row.pre_unresolved_n, 1)
        self.assertEqual(row.pre_n, 0)
        self.assertEqual(row.pre_positive_n, 0)
        self.assertEqual(row.pre_negative_n, 0)
        self.assertIsNone(row.pre_rate)

    def test_latest_terminal_snapshot_wins_regardless_of_captured_at(self):
        """If two snapshots exist for the same conv, the LATEST by
        captured_at defines the outcome — even if an older snapshot
        showed a different terminal."""
        b = _bootstrap_rec()
        applied_at = timezone.now()
        conv = Conversation.objects.create(
            org=b['org'], source='leadbridge',
            source_conversation_id='c-latest-wins',
            started_at=applied_at - timedelta(days=30),
        )
        ConversationSemanticEvent.objects.create(
            org=b['org'], conversation=conv,
            extraction_run=b['extraction'],
            ordinal=0, event_type='DISCOUNT_REQUESTED',
            actor='customer', turn_start=1, turn_end=1, confidence=0.9,
        )
        # Older snapshot: lost. Newer snapshot: booked.
        # Latest wins → positive.
        OutcomeSnapshot.objects.create(
            conversation=conv,
            captured_at=applied_at - timedelta(days=25),
            lb_lost=True,
        )
        OutcomeSnapshot.objects.create(
            conversation=conv,
            captured_at=applied_at - timedelta(days=5),
            lb_booked=True,
        )
        row = create_measurement(b['rec'], _mk_ctx(applied_at=applied_at))
        self.assertEqual(row.pre_matured_n, 1)
        self.assertEqual(row.pre_positive_n, 1)
        self.assertEqual(row.pre_negative_n, 0)
        self.assertEqual(row.pre_n, 1)

    def test_outside_baseline_window_excluded(self):
        b = _bootstrap_rec()
        applied_at = timezone.now()
        # Conv 200 days before applied_at — outside the 90d baseline
        conv = Conversation.objects.create(
            org=b['org'], source='leadbridge',
            source_conversation_id='c-old',
            started_at=applied_at - timedelta(days=200),
        )
        ConversationSemanticEvent.objects.create(
            org=b['org'], conversation=conv,
            extraction_run=b['extraction'],
            ordinal=0, event_type='DISCOUNT_REQUESTED',
            actor='customer', turn_start=1, turn_end=1, confidence=0.9,
        )
        OutcomeSnapshot.objects.create(
            conversation=conv, captured_at=applied_at - timedelta(days=195),
            lb_booked=True,
        )
        row = create_measurement(b['rec'], _mk_ctx(applied_at=applied_at))
        self.assertEqual(row.pre_n, 0)
        self.assertIsNone(row.pre_rate)

    def test_post_apply_conversations_excluded_from_baseline(self):
        b = _bootstrap_rec()
        applied_at = timezone.now()
        # Conv 2 days AFTER applied_at — not baseline material.
        conv = Conversation.objects.create(
            org=b['org'], source='leadbridge',
            source_conversation_id='c-post',
            started_at=applied_at + timedelta(days=2),
        )
        ConversationSemanticEvent.objects.create(
            org=b['org'], conversation=conv,
            extraction_run=b['extraction'],
            ordinal=0, event_type='DISCOUNT_REQUESTED',
            actor='customer', turn_start=1, turn_end=1, confidence=0.9,
        )
        row = create_measurement(b['rec'], _mk_ctx(applied_at=applied_at))
        self.assertEqual(row.pre_n, 0)
        self.assertEqual(len(row.pre_cohort_conversation_ids), 0)


class ProvenanceCoverageMethodTests(TestCase):
    def test_returns_none_when_denominator_zero(self):
        b = _bootstrap_rec()
        row = create_measurement(b['rec'], _mk_ctx())
        # Fresh row — no post observations yet.
        self.assertEqual(row.target_signal_conversations_n, 0)
        self.assertIsNone(row.provenance_coverage())

    def test_returns_ratio_when_populated(self):
        b = _bootstrap_rec()
        row = create_measurement(b['rec'], _mk_ctx())
        row.target_signal_conversations_n = 10
        row.provenance_eligible_n = 6
        row.save()
        self.assertAlmostEqual(row.provenance_coverage(), 0.6, places=6)


@override_settings(BEHAVIOR_OS_INSIGHTS_SERVICE_TOKENS='test-token')
class MeasurementApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION='Bearer test-token')

    def _url(self, pk):
        return f'/api/v1/insights/recommendations/{pk}/measurement'

    def test_get_before_creation_returns_404(self):
        b = _bootstrap_rec()
        r = self.client.get(
            self._url(b['rec'].pk), {'tenantId': TENANT},
        )
        self.assertEqual(r.status_code, 404)

    def test_post_creates_measurement_then_get_returns_it(self):
        b = _bootstrap_rec()
        r = self.client.post(
            self._url(b['rec'].pk) + f'?tenantId={TENANT}',
            {
                'lb_recommendation_application_id': 'lb-app-x',
                'applied_at': timezone.now().isoformat(),
                'pre_effective_config_hash': 'pre' + 'a' * 61,
                'treatment_effective_config_hash': 'trt' + 'b' * 61,
                'treatment_managed_hash': 'mgd' + 'c' * 61,
                'effective_config_schema_version': (
                    EFFECTIVE_CONFIG_SCHEMA_VERSION
                ),
            },
            format='json',
        )
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data['status'], 'baseline_frozen')
        self.assertEqual(r.data['target_signal'], 'DISCOUNT_REQUESTED')

        # GET returns the same row
        g = self.client.get(
            self._url(b['rec'].pk), {'tenantId': TENANT},
        )
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.data['id'], r.data['id'])

    def test_cross_tenant_returns_404(self):
        b = _bootstrap_rec()
        r = self.client.post(
            self._url(b['rec'].pk) + '?tenantId=other-tenant',
            {
                'lb_recommendation_application_id': 'lb-app-y',
                'applied_at': timezone.now().isoformat(),
                'treatment_effective_config_hash': 'trt' + 'b' * 61,
                'treatment_managed_hash': 'mgd' + 'c' * 61,
                'effective_config_schema_version': (
                    EFFECTIVE_CONFIG_SCHEMA_VERSION
                ),
            },
            format='json',
        )
        self.assertEqual(r.status_code, 404)

    def test_ineligible_rec_returns_422(self):
        b = _bootstrap_rec(rec_class='OBSERVED_STATE_INSIGHT')
        r = self.client.post(
            self._url(b['rec'].pk) + f'?tenantId={TENANT}',
            {
                'lb_recommendation_application_id': 'lb-app-z',
                'applied_at': timezone.now().isoformat(),
                'treatment_effective_config_hash': 'trt' + 'b' * 61,
                'treatment_managed_hash': 'mgd' + 'c' * 61,
                'effective_config_schema_version': (
                    EFFECTIVE_CONFIG_SCHEMA_VERSION
                ),
            },
            format='json',
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.data['reason'], 'creation_failed')
