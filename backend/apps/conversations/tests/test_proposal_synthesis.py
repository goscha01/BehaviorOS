"""Tests for the proposal generation eligibility engine + API.

Covers the ELIGIBILITY GATE (deterministic) end-to-end:
  - Rec must be STATE_COVERAGE_GAP or STATE_PARTIAL_COVERAGE
  - Lifecycle must be ACCEPTED (Accept is the first authorization boundary)
  - subject_signals must be non-empty
  - run's config_snapshot must exist (drift-detection hash)

LLM is mocked. We don't test the LLM prose here — separate concern.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from django.test import TestCase, override_settings

from apps.accounts.models import Organization
from apps.conversations.api.proposal_synthesis import (
    GENERATOR_VERSION, ProposalIneligible, check_eligibility,
    generate_proposal,
)
from apps.conversations.models import (
    BehaviorRecommendation, CustomerStateInferenceRun, LearningCorpus,
    RecommendationLifecycleState, RecommendationProposal,
    RecommendationRun, SemanticExtractionRun, TenantConfigSnapshot,
)


def _bootstrap(rec_class='STATE_PARTIAL_COVERAGE',
                signals=('DISCOUNT_REQUESTED',),
                lifecycle_state='accepted',
                tenant_id='c3d14499-dec1-42c3-a36c-713cb09842c6'):
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
        tenant_external_id=tenant_id, service_group='house_cleaning',
        contract_version='v1', raw_config={'sample': 'config'},
        raw_config_sha256='deadbeef' * 8,
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
        observation='obs',
        interpretation='interp',
        proposed_action='consider adding coverage',
        limitations='does not confirm conversion improvement',
        evidence={'state_n_discovery': 48, 'state_discovery_lift': 0.15},
    )
    if lifecycle_state:
        RecommendationLifecycleState.objects.create(
            recommendation=rec, state=lifecycle_state,
            state_changed_by='operator@example.com',
        )
    return {'rec': rec, 'snapshot': snapshot, 'org': org}


class EligibilityGateTests(TestCase):
    def test_accepted_partial_coverage_with_signals_is_eligible(self):
        ctx = _bootstrap()
        # Should not raise
        check_eligibility(ctx['rec'])

    def test_wrong_rec_class_rejected(self):
        ctx = _bootstrap(rec_class='OBSERVED_STATE_INSIGHT')
        with self.assertRaises(ProposalIneligible) as e:
            check_eligibility(ctx['rec'])
        self.assertIn('STATE_COVERAGE_GAP', str(e.exception))

    def test_lifecycle_not_accepted_rejected(self):
        for state in ['new', 'viewed', 'dismissed']:
            ctx = _bootstrap(lifecycle_state=state)
            with self.assertRaises(ProposalIneligible) as e:
                check_eligibility(ctx['rec'])
            self.assertIn('accepted', str(e.exception))

    def test_no_lifecycle_row_rejected(self):
        ctx = _bootstrap(lifecycle_state=None)
        with self.assertRaises(ProposalIneligible):
            check_eligibility(ctx['rec'])

    def test_empty_signals_rejected(self):
        ctx = _bootstrap(signals=())
        with self.assertRaises(ProposalIneligible) as e:
            check_eligibility(ctx['rec'])
        self.assertIn('subject_signals', str(e.exception))

    def test_insufficient_evidence_class_rejected(self):
        # INSUFFICIENT_EVIDENCE never becomes a proposal, even if
        # somehow lifecycle=accepted got set
        ctx = _bootstrap(rec_class='INSUFFICIENT_EVIDENCE')
        with self.assertRaises(ProposalIneligible):
            check_eligibility(ctx['rec'])


class GenerateProposalTests(TestCase):
    def _mock_llm(self):
        llm = MagicMock()
        llm.analyze.return_value = MagicMock(
            parsed_json={
                'summary': 'Respond to discount requests with our returning-customer rate.',
                'detail': 'When a customer asks about discounts or special offers, respond with the standard first-time-customer discount if applicable. Otherwise, offer a package discount for recurring service.',
            },
            input_tokens=100, output_tokens=50,
            cost_usd='0.0001',
        )
        return llm

    def test_generate_persists_proposal_with_expected_fields(self):
        ctx = _bootstrap()
        p = generate_proposal(ctx['rec'], llm_client=self._mock_llm())
        self.assertEqual(p.recommendation, ctx['rec'])
        self.assertEqual(p.condition, 'DISCOUNT_REQUESTED')
        self.assertEqual(p.scope, 'house_cleaning')
        self.assertEqual(p.change_type, 'add_behavior_rule')
        self.assertEqual(p.target_system, 'leadbridge')
        self.assertEqual(p.status, 'proposed')
        self.assertEqual(p.generator_version, GENERATOR_VERSION)
        # Provenance hash copied from snapshot
        self.assertEqual(p.config_snapshot_hash, ctx['snapshot'].raw_config_sha256)
        # LLM-drafted prose is non-empty
        self.assertTrue(p.proposed_behavior_summary)
        self.assertTrue(p.proposed_behavior_detail)

    def test_generate_is_idempotent_updates_in_place(self):
        ctx = _bootstrap()
        llm = self._mock_llm()
        p1 = generate_proposal(ctx['rec'], llm_client=llm)
        p2 = generate_proposal(ctx['rec'], llm_client=llm)
        # Same DB row (update-in-place)
        self.assertEqual(p1.pk, p2.pk)

    def test_generate_rejects_ineligible_recommendation(self):
        ctx = _bootstrap(rec_class='CONFIG_ALIGNMENT')
        with self.assertRaises(ProposalIneligible):
            generate_proposal(ctx['rec'], llm_client=self._mock_llm())

    def test_generate_raises_on_llm_returning_unusable(self):
        ctx = _bootstrap()
        bad_llm = MagicMock()
        bad_llm.analyze.return_value = MagicMock(
            parsed_json={'summary': '', 'detail': ''},
            input_tokens=1, output_tokens=1, cost_usd='0',
        )
        with self.assertRaises(RuntimeError) as e:
            generate_proposal(ctx['rec'], llm_client=bad_llm)
        self.assertIn('unusable draft', str(e.exception))


# ---------------------------------------------------------------------------
# API integration
# ---------------------------------------------------------------------------


from rest_framework.test import APITestCase


TOKEN = 'test-insights-token-xyz'


@override_settings(BEHAVIOR_OS_INSIGHTS_TOKEN=TOKEN)
class ProposalApiTests(APITestCase):
    def setUp(self):
        self.ctx = _bootstrap()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {TOKEN}')
        self.rec_id = self.ctx['rec'].pk
        self.tenant_id = self.ctx['snapshot'].tenant_external_id

    def test_get_before_generation_is_404(self):
        r = self.client.get(
            f'/api/v1/insights/recommendations/{self.rec_id}/proposal',
            {'tenantId': self.tenant_id},
        )
        self.assertEqual(r.status_code, 404)

    def test_post_generates_proposal_when_eligible(self):
        # Mock the LLM by monkey-patching the client class
        from unittest.mock import patch
        with patch('apps.conversations.api.views.LearningLLMClient') as mock_cls:
            mock_llm = MagicMock()
            mock_llm.analyze.return_value = MagicMock(
                parsed_json={
                    'summary': 's', 'detail': 'd',
                },
                input_tokens=1, output_tokens=1, cost_usd='0',
            )
            mock_cls.return_value = mock_llm
            r = self.client.post(
                f'/api/v1/insights/recommendations/{self.rec_id}/proposal'
                f'?tenantId={self.tenant_id}',
                data={}, format='json',
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'proposed')
        self.assertEqual(r.data['condition'], 'DISCOUNT_REQUESTED')

    def test_post_ineligible_returns_422(self):
        # Wrong rec class
        self.ctx['rec'].rec_class = 'INSUFFICIENT_EVIDENCE'
        self.ctx['rec'].save()
        r = self.client.post(
            f'/api/v1/insights/recommendations/{self.rec_id}/proposal'
            f'?tenantId={self.tenant_id}',
            data={}, format='json',
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.data['reason'], 'ineligible')

    def test_cross_tenant_get_is_404(self):
        r = self.client.get(
            f'/api/v1/insights/recommendations/{self.rec_id}/proposal',
            {'tenantId': 'wrong-tenant'},
        )
        self.assertEqual(r.status_code, 404)

    def test_consumer_status_update_applied(self):
        # First generate the proposal
        from unittest.mock import patch
        with patch('apps.conversations.api.views.LearningLLMClient') as mock_cls:
            mock_cls.return_value.analyze.return_value = MagicMock(
                parsed_json={'summary': 's', 'detail': 'd'},
                input_tokens=1, output_tokens=1, cost_usd='0',
            )
            self.client.post(
                f'/api/v1/insights/recommendations/{self.rec_id}/proposal'
                f'?tenantId={self.tenant_id}',
                data={}, format='json',
            )
        # Consumer reports applied
        r = self.client.post(
            f'/api/v1/insights/recommendations/{self.rec_id}/proposal/status'
            f'?tenantId={self.tenant_id}',
            data={'status': 'applied'}, format='json',
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'applied')
        self.assertIsNotNone(r.data['consumer_applied_at'])

    def test_consumer_status_update_stale(self):
        from unittest.mock import patch
        with patch('apps.conversations.api.views.LearningLLMClient') as mock_cls:
            mock_cls.return_value.analyze.return_value = MagicMock(
                parsed_json={'summary': 's', 'detail': 'd'},
                input_tokens=1, output_tokens=1, cost_usd='0',
            )
            self.client.post(
                f'/api/v1/insights/recommendations/{self.rec_id}/proposal'
                f'?tenantId={self.tenant_id}',
                data={}, format='json',
            )
        r = self.client.post(
            f'/api/v1/insights/recommendations/{self.rec_id}/proposal/status'
            f'?tenantId={self.tenant_id}',
            data={'status': 'stale', 'error': 'config hash mismatch'},
            format='json',
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'stale')
        self.assertIn('mismatch', r.data['consumer_error'])
