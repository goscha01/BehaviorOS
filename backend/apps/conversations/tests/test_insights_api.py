"""Insights API tests — tenant scoping, auth, lifecycle transitions."""

from __future__ import annotations

from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.accounts.models import Organization
from apps.conversations.models import (
    BehaviorRecommendation, CustomerStateInferenceRun, LearningCorpus,
    RecommendationLifecycleState, RecommendationRun,
    SemanticExtractionRun, TenantConfigSnapshot,
)


TOKEN = 'test-insights-token-abc'


def _bootstrap(tenant_id='c3d14499-dec1-42c3-a36c-713cb09842c6',
                other_tenant_id='wrong-tenant-uuid'):
    """Seed a minimal recommendation run for tenant A + one for tenant B."""
    org = Organization.objects.create(name='TestOrg')
    corpus = LearningCorpus.objects.create(
        org=org, name='c', version='v1',
    )
    extraction = SemanticExtractionRun.objects.create(
        org=org, corpus=corpus,
        extractor_version='e', ontology_version='o',
        prompt_version='p', model='m',
    )
    infer = CustomerStateInferenceRun.objects.create(
        org=org, corpus=corpus, extraction_run=extraction,
        inference_version='inf-v1',
    )
    snapshot_a = TenantConfigSnapshot.objects.create(
        org=org, source_system='leadbridge',
        tenant_external_id=tenant_id, service_group='cleaning',
        contract_version='v1', raw_config={}, raw_config_sha256='a' * 64,
    )
    snapshot_b = TenantConfigSnapshot.objects.create(
        org=org, source_system='leadbridge',
        tenant_external_id=other_tenant_id, service_group='cleaning',
        contract_version='v1', raw_config={}, raw_config_sha256='b' * 64,
    )
    run_a = RecommendationRun.objects.create(
        org=org, corpus=corpus,
        state_inference_run=infer,
        config_snapshot=snapshot_a,
        synthesizer_version='rec-v1',
    )
    run_b = RecommendationRun.objects.create(
        org=org, corpus=corpus,
        state_inference_run=infer,
        config_snapshot=snapshot_b,
        synthesizer_version='rec-v1',
    )
    rec_a = BehaviorRecommendation.objects.create(
        run=run_a, recommendation_id='R0001',
        rec_class=BehaviorRecommendation.RecClass.STATE_PARTIAL_COVERAGE,
        confidence=BehaviorRecommendation.Confidence.HIGH,
        subject_state='HIGH_INTENT',
        subject_signals=['DISCOUNT_REQUESTED'],
        proposed_action_scope=(
            BehaviorRecommendation.ProposedActionScope.CONFIG_ADDITION
        ),
        observation='obs A',
        interpretation='interp A',
        proposed_action='act A',
        limitations='limits A',
    )
    rec_b = BehaviorRecommendation.objects.create(
        run=run_b, recommendation_id='R0001',
        rec_class=BehaviorRecommendation.RecClass.CONFIG_ALIGNMENT,
        confidence=BehaviorRecommendation.Confidence.MEDIUM,
        subject_state='BOOKING_INTENT',
        observation='obs B',
        interpretation='interp B',
        limitations='limits B',
    )
    return {
        'org': org, 'tenant_a': tenant_id, 'tenant_b': other_tenant_id,
        'run_a': run_a, 'run_b': run_b,
        'rec_a': rec_a, 'rec_b': rec_b,
    }


@override_settings(BEHAVIOR_OS_INSIGHTS_TOKEN=TOKEN)
class TenantScopingTests(APITestCase):
    def setUp(self):
        self.ctx = _bootstrap()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {TOKEN}')

    def test_runs_list_scopes_to_tenant(self):
        # Tenant A sees only run_a
        r = self.client.get('/api/v1/insights/runs',
                             {'tenantId': self.ctx['tenant_a']})
        self.assertEqual(r.status_code, 200)
        results = r.data['results'] if isinstance(r.data, dict) else r.data
        ids = [row['id'] for row in results]
        self.assertIn(str(self.ctx['run_a'].pk), ids)
        self.assertNotIn(str(self.ctx['run_b'].pk), ids)

    def test_missing_tenant_id_is_400(self):
        r = self.client.get('/api/v1/insights/runs')
        self.assertEqual(r.status_code, 400)
        self.assertIn('tenantId', r.data)

    def test_cross_tenant_recommendation_access_is_404(self):
        # Tenant A tries to fetch tenant B's recommendation
        r = self.client.get(
            f'/api/v1/insights/recommendations/{self.ctx["rec_b"].pk}',
            {'tenantId': self.ctx['tenant_a']},
        )
        self.assertEqual(r.status_code, 404)

    def test_own_tenant_recommendation_retrieves(self):
        r = self.client.get(
            f'/api/v1/insights/recommendations/{self.ctx["rec_a"].pk}',
            {'tenantId': self.ctx['tenant_a']},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['recommendation_id'], 'R0001')
        self.assertEqual(r.data['rec_class'], 'STATE_PARTIAL_COVERAGE')
        self.assertEqual(r.data['lifecycle']['state'], 'new')

    def test_run_recommendations_action_returns_only_run_recs(self):
        r = self.client.get(
            f'/api/v1/insights/runs/{self.ctx["run_a"].pk}/recommendations',
            {'tenantId': self.ctx['tenant_a']},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data['recommendations']), 1)
        self.assertEqual(r.data['recommendations'][0]['rec_class'],
                          'STATE_PARTIAL_COVERAGE')


@override_settings(BEHAVIOR_OS_INSIGHTS_TOKEN=TOKEN)
class AuthTests(APITestCase):
    def setUp(self):
        self.ctx = _bootstrap()

    def test_missing_token_is_401(self):
        r = self.client.get('/api/v1/insights/runs',
                             {'tenantId': self.ctx['tenant_a']})
        self.assertEqual(r.status_code, 401)

    def test_wrong_token_is_401(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer nope')
        r = self.client.get('/api/v1/insights/runs',
                             {'tenantId': self.ctx['tenant_a']})
        self.assertEqual(r.status_code, 401)

    def test_correct_token_is_200(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {TOKEN}')
        r = self.client.get('/api/v1/insights/runs',
                             {'tenantId': self.ctx['tenant_a']})
        self.assertEqual(r.status_code, 200)


@override_settings(BEHAVIOR_OS_INSIGHTS_TOKEN=TOKEN)
class LifecycleTransitionTests(APITestCase):
    def setUp(self):
        self.ctx = _bootstrap()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {TOKEN}')

    def _url(self, rec):
        return f'/api/v1/insights/recommendations/{rec.pk}/lifecycle'

    def test_transition_to_viewed_creates_lifecycle_row(self):
        rec = self.ctx['rec_a']
        r = self.client.post(
            self._url(rec) + f'?tenantId={self.ctx["tenant_a"]}',
            data={'state': 'viewed', 'actor': 'operator@example.com'},
            format='json',
        )
        self.assertEqual(r.status_code, 200)
        lc = RecommendationLifecycleState.objects.get(recommendation=rec)
        self.assertEqual(lc.state, 'viewed')
        self.assertEqual(lc.state_changed_by, 'operator@example.com')
        # History has one entry
        self.assertEqual(len(lc.history), 1)
        self.assertEqual(lc.history[0]['to'], 'viewed')
        self.assertEqual(lc.history[0]['from'], 'new')

    def test_transition_to_accepted_does_not_touch_config(self):
        # V1: accept only records the decision; no LB config change
        rec = self.ctx['rec_a']
        r = self.client.post(
            self._url(rec) + f'?tenantId={self.ctx["tenant_a"]}',
            data={'state': 'accepted'}, format='json',
        )
        self.assertEqual(r.status_code, 200)
        # The BehaviorRecommendation itself is unchanged
        rec.refresh_from_db()
        self.assertEqual(rec.observation, 'obs A')
        # Lifecycle reflects accept
        lc = RecommendationLifecycleState.objects.get(recommendation=rec)
        self.assertEqual(lc.state, 'accepted')

    def test_transition_to_dismissed_with_reason(self):
        rec = self.ctx['rec_a']
        r = self.client.post(
            self._url(rec) + f'?tenantId={self.ctx["tenant_a"]}',
            data={
                'state': 'dismissed',
                'reason': 'already_doing_this',
                'note': 'we already handle discounts via a separate flow',
            },
            format='json',
        )
        self.assertEqual(r.status_code, 200)
        lc = RecommendationLifecycleState.objects.get(recommendation=rec)
        self.assertEqual(lc.state, 'dismissed')
        self.assertEqual(lc.dismissal_reason, 'already_doing_this')
        self.assertIn('separate flow', lc.dismissal_note)

    def test_cross_tenant_lifecycle_transition_is_404(self):
        rec = self.ctx['rec_b']
        r = self.client.post(
            self._url(rec) + f'?tenantId={self.ctx["tenant_a"]}',
            data={'state': 'viewed'}, format='json',
        )
        self.assertEqual(r.status_code, 404)

    def test_history_appends_across_transitions(self):
        rec = self.ctx['rec_a']
        for state in ['viewed', 'accepted']:
            self.client.post(
                self._url(rec) + f'?tenantId={self.ctx["tenant_a"]}',
                data={'state': state}, format='json',
            )
        lc = RecommendationLifecycleState.objects.get(recommendation=rec)
        self.assertEqual(len(lc.history), 2)
        self.assertEqual(lc.history[0]['to'], 'viewed')
        self.assertEqual(lc.history[1]['to'], 'accepted')

    def test_bad_state_value_is_400(self):
        rec = self.ctx['rec_a']
        r = self.client.post(
            self._url(rec) + f'?tenantId={self.ctx["tenant_a"]}',
            data={'state': 'not_a_real_state'}, format='json',
        )
        self.assertEqual(r.status_code, 400)
