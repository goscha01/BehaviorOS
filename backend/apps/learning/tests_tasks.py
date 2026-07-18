"""Tests for the promotion Celery task wrappers.

These are thin wrappers around `promote_evidence_events` — the intent
is to prove the task<->service handshake works (correct org resolution,
limit passed through, dict shape suitable for beat/observability),
NOT to re-test eligibility rules (those live in tests_eligibility.py)
or promotion mechanics (tests_promotion.py).

Runs tasks in-process via CELERY_TASK_ALWAYS_EAGER — no real broker,
no worker, no scheduling. That's enough to catch signature drift and
serialization issues. End-to-end worker-runs-task is validated live
against the deployed worker service, not here.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Organization
from apps.context.models import EvidenceEvent
from apps.learning.models import EvidenceInsight
from apps.learning.tasks import (
    promote_all_orgs_task,
    promote_evidence_events_task,
)


def _real_payload(runtime_outcome: str = 'booking_created') -> dict:
    return {
        'contextRequestId': 'ctx_realcall_1234567890',
        'metadata': {
            'runtimeOutcome': runtime_outcome,
            'durationSeconds': 60,
            'aiDecisionSummary': {'conversationResult': 'booking_created'},
        },
    }


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class PromoteEvidenceEventsTaskTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='TaskTestCo')

    def _make_event(self, customer_id: str = '+14155551234') -> EvidenceEvent:
        return EvidenceEvent.objects.create(
            org=self.org,
            source_kind=EvidenceEvent.SourceKind.RUNTIME,
            runtime='callio',
            channel='voice',
            event_type='call_completed',
            customer_id=customer_id,
            conversation_id='conv-1',
            occurred_at=timezone.now(),
            payload=_real_payload(),
        )

    def test_task_promotes_an_eligible_event_end_to_end(self):
        event = self._make_event()
        result = promote_evidence_events_task.apply(args=[str(self.org.id)]).get()
        self.assertEqual(result['scanned'], 1)
        self.assertEqual(result['promoted'], 1)
        self.assertEqual(result['failed'], 0)
        event.refresh_from_db()
        self.assertEqual(event.promotion_status, EvidenceEvent.PromotionStatus.PROMOTED)
        self.assertEqual(EvidenceInsight.objects.count(), 1)

    def test_task_returns_skip_breakdown_by_reason(self):
        self._make_event()  # eligible
        self._make_event(customer_id='+18135551234')  # synthetic
        result = promote_evidence_events_task.apply(args=[str(self.org.id)]).get()
        self.assertEqual(result['scanned'], 2)
        self.assertEqual(result['promoted'], 1)
        self.assertEqual(result['skipped'], 1)
        self.assertIn('skip_synthetic', result['skipped_by_reason'])

    def test_task_returns_marker_for_unknown_org(self):
        result = promote_evidence_events_task.apply(
            args=['00000000-0000-0000-0000-000000000000'],
        ).get()
        self.assertEqual(result['skipped'], 'org_not_found')

    def test_task_honors_explicit_limit(self):
        for _ in range(5):
            self._make_event()
        result = promote_evidence_events_task.apply(
            args=[str(self.org.id), 2],
        ).get()
        self.assertEqual(result['scanned'], 2)
        remaining = EvidenceEvent.objects.filter(
            org=self.org, promotion_status=EvidenceEvent.PromotionStatus.PENDING,
        ).count()
        self.assertEqual(remaining, 3)


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class PromoteAllOrgsTaskTest(TestCase):
    def test_fan_out_queues_one_task_per_org(self):
        Organization.objects.create(name='OrgA')
        Organization.objects.create(name='OrgB')

        with mock.patch(
            'apps.learning.tasks.promote_evidence_events_task.delay',
        ) as delay:
            result = promote_all_orgs_task.apply(args=[50]).get()

        self.assertEqual(result['orgs_queued'], 2)
        self.assertEqual(result['limit_per_org'], 50)
        self.assertEqual(delay.call_count, 2)
