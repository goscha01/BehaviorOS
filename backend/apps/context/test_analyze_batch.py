"""Tests for POST /v1/context/analyze-batch.

Contract invariants:
- 200 with per-conversation results (analyzer JSON) on happy path.
- 400 when body is malformed / empty / oversized.
- 401 when service token is misconfigured (reuses ContextView's guard).
- ZERO writes to the durable learning dataset.
- Individual conversation failures don't fail the batch.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.context.models import EvidenceEvent, CustomerHistoryAggregate, OrgStatistics
from apps.learning.models import EvidenceInsight


ANALYZE_URL = '/api/context/v1/analyze-batch'


def _sample_conversation(id_='c1', messages=None):
    return {
        'id': id_,
        'channel': 'sms',
        'source_system': 'quo',
        'messages': messages or [
            {'role': 'customer', 'text': 'Hi, how much for a house cleaning?'},
            {'role': 'business', 'text': 'Deep cleaning starts at $220.'},
            {'role': 'customer', 'text': 'Ok let me think about it.'},
        ],
        'outcome': 'unknown',
    }


class AnalyzeBatchContractTest(TestCase):
    """Wire-contract invariants for the batch analyzer."""

    def setUp(self):
        self.client = APIClient()

    def test_happy_path_returns_per_conversation_analysis(self):
        response = self.client.post(
            ANALYZE_URL,
            data={'conversations': [_sample_conversation('a'), _sample_conversation('b')]},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn('batchId', body)
        self.assertTrue(body['batchId'].startswith('anab_'))
        self.assertEqual(len(body['results']), 2)
        for i, expected_id in enumerate(('a', 'b')):
            row = body['results'][i]
            self.assertEqual(row['id'], expected_id)
            self.assertIn('analysis', row)
            # Analyzer schema keys always present (stub or real).
            for key in (
                'summary', 'category', 'subcategory', 'confidence',
                'customer_intent', 'outcome_analysis',
                'candidate_playbook_rules', 'candidate_faq', 'signals',
            ):
                self.assertIn(key, row['analysis'])

    def test_missing_id_gets_synthesized_id(self):
        response = self.client.post(
            ANALYZE_URL,
            data={'conversations': [
                {**_sample_conversation(), 'id': ''},
                _sample_conversation('has-id'),
            ]},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        ids = [r['id'] for r in response.json()['results']]
        self.assertEqual(ids[0], 'conv_0')
        self.assertEqual(ids[1], 'has-id')

    def test_empty_batch_is_400(self):
        response = self.client.post(
            ANALYZE_URL, data={'conversations': []}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_conversations_is_400(self):
        response = self.client.post(ANALYZE_URL, data={}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_oversized_batch_is_400(self):
        many = [_sample_conversation(f'x{i}') for i in range(101)]
        response = self.client.post(
            ANALYZE_URL, data={'conversations': many}, format='json',
        )
        self.assertEqual(response.status_code, 400)


class AnalyzeBatchAuthTest(TestCase):
    """Reuses ContextView's service token guard — same 401 semantics."""

    def setUp(self):
        self.client = APIClient()

    @override_settings(BEHAVIOR_CONTEXT_SERVICE_TOKEN='right')
    def test_wrong_token_is_401(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer wrong')
        response = self.client.post(
            ANALYZE_URL,
            data={'conversations': [_sample_conversation()]},
            format='json',
        )
        self.assertEqual(response.status_code, 401)

    @override_settings(BEHAVIOR_CONTEXT_SERVICE_TOKEN='right')
    def test_correct_token_authenticates(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer right')
        response = self.client.post(
            ANALYZE_URL,
            data={'conversations': [_sample_conversation()]},
            format='json',
        )
        self.assertEqual(response.status_code, 200)


class AnalyzeBatchNoPersistenceTest(TestCase):
    """The non-negotiable: no rows in the durable learning dataset."""

    def setUp(self):
        self.client = APIClient()

    def test_no_evidence_insight_written(self):
        response = self.client.post(
            ANALYZE_URL,
            data={'conversations': [_sample_conversation('p1'), _sample_conversation('p2')]},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        # Zero writes into the learning corpus.
        self.assertEqual(EvidenceInsight.objects.count(), 0)
        # Zero writes into the context evidence pipeline.
        self.assertEqual(EvidenceEvent.objects.count(), 0)
        self.assertEqual(CustomerHistoryAggregate.objects.count(), 0)
        self.assertEqual(OrgStatistics.objects.count(), 0)
