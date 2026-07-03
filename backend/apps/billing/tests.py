import json
from unittest.mock import patch, MagicMock
from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.accounts.models import Organization, Membership
from apps.billing.models import StripeCustomer, Subscription
from apps.billing.services import sync_subscription

User = get_user_model()


class BillingWebhookTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='billinguser', email='billing@test.com', password='TestPass123!'
        )
        self.org = self.user.memberships.first().org
        self.stripe_customer = StripeCustomer.objects.create(
            org=self.org, stripe_customer_id='cus_test123'
        )

    def test_sync_subscription_creates_new(self):
        stripe_sub = {
            'id': 'sub_test123',
            'customer': 'cus_test123',
            'status': 'active',
            'current_period_end': 1700000000,
            'cancel_at_period_end': False,
            'items': {'data': [{'price': {'id': 'price_starter'}}]},
        }
        sub = sync_subscription(stripe_sub)
        self.assertIsNotNone(sub)
        self.assertEqual(sub.status, 'active')
        self.assertEqual(sub.org, self.org)

    def test_sync_subscription_updates_existing(self):
        Subscription.objects.create(
            org=self.org,
            stripe_subscription_id='sub_test123',
            status='incomplete',
            plan='starter',
        )
        stripe_sub = {
            'id': 'sub_test123',
            'customer': 'cus_test123',
            'status': 'active',
            'current_period_end': 1700000000,
            'cancel_at_period_end': False,
            'items': {'data': [{'price': {'id': 'price_starter'}}]},
        }
        sub = sync_subscription(stripe_sub)
        self.assertEqual(sub.status, 'active')
        self.assertEqual(Subscription.objects.count(), 1)

    def test_sync_subscription_unknown_customer(self):
        stripe_sub = {
            'id': 'sub_unknown',
            'customer': 'cus_unknown',
            'status': 'active',
            'current_period_end': 1700000000,
            'cancel_at_period_end': False,
            'items': {'data': []},
        }
        result = sync_subscription(stripe_sub)
        self.assertIsNone(result)


class SubscriptionStatusViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='statususer', email='status@test.com', password='TestPass123!'
        )
        tokens = self.client.post('/api/auth/login/', {
            'username': 'statususer', 'password': 'TestPass123!'
        }).json()['tokens']
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        self.org = self.user.memberships.first().org

    def test_no_subscription(self):
        response = self.client.get('/api/billing/subscription/')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['plan'])

    def test_with_subscription(self):
        Subscription.objects.create(
            org=self.org,
            stripe_subscription_id='sub_test',
            status='active',
            plan='pro',
        )
        response = self.client.get('/api/billing/subscription/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['plan'], 'pro')
        self.assertEqual(response.json()['status'], 'active')
