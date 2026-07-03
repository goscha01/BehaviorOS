from unittest.mock import patch, MagicMock
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.billing.models import Subscription
from apps.training.models import (
    BusinessProfile, ScenarioTemplate, Script,
    TrainingSession, SessionTurn,
)

User = get_user_model()


class TrainingSessionTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='trainer', email='trainer@test.com', password='TestPass123!'
        )
        self.org = self.user.memberships.first().org

        # Create active subscription to pass gate
        Subscription.objects.create(
            org=self.org,
            stripe_subscription_id='sub_test_training',
            status='active',
            plan='starter',
        )

        tokens = self.client.post('/api/auth/login/', {
            'username': 'trainer', 'password': 'TestPass123!'
        }).json()['tokens']
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

        self.profile = BusinessProfile.objects.create(
            org=self.org, name='Test Co', service_desc='Test services'
        )
        self.scenario = ScenarioTemplate.objects.create(
            org=self.org,
            name='Test Scenario',
            system_prompt='You are a test customer.',
            difficulty='easy',
        )
        self.script = Script.objects.create(
            org=self.org, name='Test Script', content='Say hello.'
        )

    def test_create_session(self):
        response = self.client.post('/api/training/sessions/', {
            'business_profile': str(self.profile.id),
            'scenario_template': str(self.scenario.id),
            'script': str(self.script.id),
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['status'], 'created')

    @patch('apps.training.services.session_runner.generate_reply')
    @patch('apps.training.services.session_runner.generate_speech')
    def test_start_session(self, mock_speech, mock_reply):
        mock_reply.return_value = 'Hello, I need help with my AC.'
        mock_speech.return_value = 'media/audio/test.mp3'

        session = TrainingSession.objects.create(
            org=self.org,
            business_profile=self.profile,
            scenario_template=self.scenario,
            script=self.script,
        )
        response = self.client.post(f'/api/training/sessions/{session.id}/start/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'running')
        self.assertEqual(len(response.json()['turns']), 1)

    @patch('apps.training.services.session_runner.generate_reply')
    @patch('apps.training.services.session_runner.generate_speech')
    def test_submit_turn(self, mock_speech, mock_reply):
        mock_reply.return_value = 'Thanks for the info.'
        mock_speech.return_value = 'media/audio/test2.mp3'

        session = TrainingSession.objects.create(
            org=self.org,
            business_profile=self.profile,
            scenario_template=self.scenario,
            script=self.script,
            status='running',
        )
        response = self.client.post(
            f'/api/training/sessions/{session.id}/turn/',
            {'text': 'I can schedule a technician for tomorrow.'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['turns']), 2)  # candidate + AI

    @patch('apps.training.tasks.extract_session_signals.delay')
    def test_complete_session(self, mock_task):
        session = TrainingSession.objects.create(
            org=self.org,
            business_profile=self.profile,
            scenario_template=self.scenario,
            script=self.script,
            status='running',
        )
        response = self.client.post(f'/api/training/sessions/{session.id}/complete/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'completed')
        mock_task.assert_called_once()

    def test_session_requires_subscription(self):
        # Remove subscription
        Subscription.objects.filter(org=self.org).delete()
        response = self.client.post('/api/training/sessions/', {
            'business_profile': str(self.profile.id),
            'scenario_template': str(self.scenario.id),
            'script': str(self.script.id),
        })
        self.assertEqual(response.status_code, 403)


class BusinessProfileCRUDTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='cruduser', email='crud@test.com', password='TestPass123!'
        )
        self.org = self.user.memberships.first().org
        tokens = self.client.post('/api/auth/login/', {
            'username': 'cruduser', 'password': 'TestPass123!'
        }).json()['tokens']
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def test_create_business_profile(self):
        response = self.client.post('/api/training/business-profiles/', {
            'name': 'New Company',
            'service_desc': 'We fix things',
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['name'], 'New Company')

    def test_list_business_profiles(self):
        BusinessProfile.objects.create(org=self.org, name='Profile 1')
        BusinessProfile.objects.create(org=self.org, name='Profile 2')
        response = self.client.get('/api/training/business-profiles/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['results']), 2)

    def test_org_isolation(self):
        other_user = User.objects.create_user(
            username='other', email='other@test.com', password='TestPass123!'
        )
        other_org = other_user.memberships.first().org
        BusinessProfile.objects.create(org=other_org, name='Other Profile')
        response = self.client.get('/api/training/business-profiles/')
        self.assertEqual(len(response.json()['results']), 0)
