from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.accounts.models import Organization, Membership

User = get_user_model()


class AuthFlowTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_register_creates_user_and_org(self):
        response = self.client.post('/api/auth/register/', {
            'username': 'testuser',
            'email': 'test@example.com',
            'password': 'Str0ngP@ss!',
            'password_confirm': 'Str0ngP@ss!',
        })
        self.assertEqual(response.status_code, 201)
        self.assertIn('tokens', response.json())
        self.assertIn('access', response.json()['tokens'])

        user = User.objects.get(username='testuser')
        self.assertTrue(user.memberships.exists())
        membership = user.memberships.first()
        self.assertEqual(membership.role, Membership.Role.OWNER)

    def test_register_with_org_name(self):
        response = self.client.post('/api/auth/register/', {
            'username': 'testuser2',
            'email': 'test2@example.com',
            'password': 'Str0ngP@ss!',
            'password_confirm': 'Str0ngP@ss!',
            'org_name': 'My Company',
        })
        self.assertEqual(response.status_code, 201)
        user = User.objects.get(username='testuser2')
        org = user.memberships.first().org
        self.assertEqual(org.name, 'My Company')

    def test_login_with_valid_credentials(self):
        User.objects.create_user(username='loginuser', email='login@test.com', password='TestPass123!')
        response = self.client.post('/api/auth/login/', {
            'username': 'loginuser',
            'password': 'TestPass123!',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('tokens', response.json())

    def test_login_with_invalid_credentials(self):
        response = self.client.post('/api/auth/login/', {
            'username': 'nonexistent',
            'password': 'wrong',
        })
        self.assertEqual(response.status_code, 401)

    def test_me_requires_auth(self):
        response = self.client.get('/api/auth/me/')
        self.assertEqual(response.status_code, 401)

    def test_me_returns_user_data(self):
        user = User.objects.create_user(username='meuser', email='me@test.com', password='TestPass123!')
        self.client.post('/api/auth/login/', {'username': 'meuser', 'password': 'TestPass123!'})
        tokens = self.client.post('/api/auth/login/', {'username': 'meuser', 'password': 'TestPass123!'}).json()['tokens']
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = self.client.get('/api/auth/me/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['username'], 'meuser')
