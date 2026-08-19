"""Outcome resolution tests — in-memory fetchers + folding rules."""

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Organization
from apps.conversations.models import (
    Channel,
    Conversation,
    EntityLink,
    MatchMethod,
    OutcomeSnapshot,
    TargetSystem,
    TargetType,
)
from apps.conversations.outcomes.base import LeadBridgeOutcome, ServiceFlowOutcome
from apps.conversations.outcomes.leadbridge import InMemoryLeadBridgeOutcomeFetcher
from apps.conversations.outcomes.serviceflow import InMemoryServiceFlowOutcomeFetcher
from apps.conversations.outcomes.service import resolve_and_persist


def _make_conv(org: Organization, **kw) -> Conversation:
    defaults = dict(
        org=org,
        source='quo',
        source_conversation_id=f'CN_{timezone.now().isoformat()}',
        channel=Channel.SMS,
        customer_phone='+18135551234',
        started_at=timezone.now(),
    )
    defaults.update(kw)
    return Conversation.objects.create(**defaults)


def _link_lb_lead(conv: Conversation, lead_id: str) -> EntityLink:
    return EntityLink.objects.create(
        conversation=conv,
        target_system=TargetSystem.LEADBRIDGE,
        target_type=TargetType.LEAD,
        target_id=lead_id,
        match_method=MatchMethod.PHONE_EXACT,
    )


def _link_sf(conv: Conversation, entity_type: str, entity_id: str) -> EntityLink:
    return EntityLink.objects.create(
        conversation=conv,
        target_system=TargetSystem.SERVICEFLOW,
        target_type=entity_type,
        target_id=entity_id,
        match_method=MatchMethod.EXTERNAL_ID,
    )


class LeadBridgeOutcomeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Spotless')
        self.conv = _make_conv(self.org)
        self.lb = InMemoryLeadBridgeOutcomeFetcher()

    def test_snapshot_records_lb_fields(self):
        _link_lb_lead(self.conv, 'lb_1')
        self.lb.register(LeadBridgeOutcome(
            lb_lead_id='lb_1', status='booked',
            engaged=True, booked=True, lost=False, cancelled=False,
        ))
        result = resolve_and_persist(self.conv, lb_fetcher=self.lb)
        self.assertTrue(result.created)
        snap = result.snapshot
        self.assertEqual(snap.lb_status, 'booked')
        self.assertTrue(snap.lb_engaged)
        self.assertTrue(snap.lb_booked)
        self.assertFalse(snap.lb_lost)
        self.assertFalse(snap.lb_cancelled)

    def test_missing_lb_outcome_persists_empty_fields(self):
        _link_lb_lead(self.conv, 'lb_missing')
        # No outcome registered — fetcher returns nothing.
        result = resolve_and_persist(self.conv, lb_fetcher=self.lb)
        snap = result.snapshot
        self.assertIsNotNone(snap)
        self.assertEqual(snap.lb_status, '')
        self.assertIsNone(snap.lb_booked)

    def test_multiple_lb_leads_or_reduce_flags(self):
        _link_lb_lead(self.conv, 'lb_a')
        _link_lb_lead(self.conv, 'lb_b')
        self.lb.register(LeadBridgeOutcome(lb_lead_id='lb_a', booked=False))
        self.lb.register(LeadBridgeOutcome(lb_lead_id='lb_b', booked=True))
        result = resolve_and_persist(self.conv, lb_fetcher=self.lb)
        # OR-reduce: at least one lead booked → booked=True.
        self.assertTrue(result.snapshot.lb_booked)


class ServiceFlowOutcomeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Spotless')
        self.conv = _make_conv(self.org)
        self.sf = InMemoryServiceFlowOutcomeFetcher()

    def test_opportunity_status_preferred_over_job(self):
        _link_sf(self.conv, TargetType.OPPORTUNITY, 'sf_opp_1')
        _link_sf(self.conv, TargetType.JOB, 'sf_job_1')
        self.sf.register(ServiceFlowOutcome(
            sf_entity_type=TargetType.OPPORTUNITY, sf_entity_id='sf_opp_1',
            opportunity_status='won',
        ))
        self.sf.register(ServiceFlowOutcome(
            sf_entity_type=TargetType.JOB, sf_entity_id='sf_job_1',
            opportunity_status='pending',
        ))
        result = resolve_and_persist(self.conv, sf_fetcher=self.sf)
        self.assertEqual(result.snapshot.sf_opportunity_status, 'won')

    def test_revenue_takes_max_across_entities(self):
        _link_sf(self.conv, TargetType.OPPORTUNITY, 'sf_opp_1')
        _link_sf(self.conv, TargetType.CUSTOMER, 'sf_cust_1')
        self.sf.register(ServiceFlowOutcome(
            sf_entity_type=TargetType.OPPORTUNITY, sf_entity_id='sf_opp_1',
            revenue_cents=15000,
        ))
        self.sf.register(ServiceFlowOutcome(
            sf_entity_type=TargetType.CUSTOMER, sf_entity_id='sf_cust_1',
            revenue_cents=60000,  # 4 recurring cleans
            recurring=True,
            job_count=4,
        ))
        result = resolve_and_persist(self.conv, sf_fetcher=self.sf)
        self.assertEqual(result.snapshot.sf_revenue_cents, 60000)
        self.assertTrue(result.snapshot.sf_recurring)
        self.assertEqual(result.snapshot.sf_job_count, 4)

    def test_no_sf_signal_leaves_null_fields(self):
        # No SF links at all — snapshot may still exist if there's LB data
        # but SF fields remain None.
        _link_lb_lead(self.conv, 'lb_1')
        result = resolve_and_persist(self.conv, sf_fetcher=self.sf)
        # No LB fetcher supplied here — snapshot is created but empty.
        snap = result.snapshot
        self.assertIsNotNone(snap)
        self.assertIsNone(snap.sf_revenue_cents)
        self.assertIsNone(snap.sf_completed)


class RerunSemanticsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Spotless')
        self.conv = _make_conv(self.org)
        _link_lb_lead(self.conv, 'lb_1')
        self.lb = InMemoryLeadBridgeOutcomeFetcher()
        self.lb.register(LeadBridgeOutcome(lb_lead_id='lb_1', status='new'))

    def test_rerun_within_same_second_is_noop(self):
        result_a = resolve_and_persist(self.conv, lb_fetcher=self.lb)
        result_b = resolve_and_persist(self.conv, lb_fetcher=self.lb)
        self.assertTrue(result_a.created)
        self.assertFalse(result_b.created)
        self.assertEqual(OutcomeSnapshot.objects.count(), 1)

    def test_history_preserved_when_outcome_evolves(self):
        # First snapshot: status='new'.
        first = resolve_and_persist(self.conv, lb_fetcher=self.lb)
        self.assertTrue(first.created)

        # Simulate a week passing + LB shows booking + revenue.
        from datetime import timedelta
        first.snapshot.captured_at = first.snapshot.captured_at - timedelta(days=7)
        first.snapshot.save(update_fields=['captured_at'])

        # Update the fetcher's answer + rerun.
        self.lb.register(LeadBridgeOutcome(
            lb_lead_id='lb_1', status='booked', booked=True,
        ))
        second = resolve_and_persist(self.conv, lb_fetcher=self.lb)
        self.assertTrue(second.created)

        # Both snapshots exist; ordering shows newest first.
        snaps = list(self.conv.outcome_snapshots.all())
        self.assertEqual(len(snaps), 2)
        # Newest first.
        self.assertEqual(snaps[0].lb_status, 'booked')
        self.assertEqual(snaps[1].lb_status, 'new')
