"""Model-level constraint + defaults tests for apps.conversations.

Higher-level tests (adapter, resolver, importer) live alongside their
respective services in this package. These tests exist to lock in the
uniqueness / tenant-isolation guarantees that everything else depends on.
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Organization
from apps.conversations.models import (
    Channel,
    Conversation,
    ConversationTurn,
    Direction,
    EntityLink,
    IngestionStatus,
    MatchMethod,
    OutcomeSnapshot,
    Speaker,
    TargetSystem,
    TargetType,
)


def _make_org(name: str = 'Spotless Homes') -> Organization:
    return Organization.objects.create(name=name)


def _make_conversation(
    org: Organization,
    *,
    source: str = 'quo',
    source_conversation_id: str = 'quo_conv_1',
    channel: str = Channel.VOICE,
    customer_phone: str = '+18135551234',
    started_at=None,
) -> Conversation:
    return Conversation.objects.create(
        org=org,
        source=source,
        source_conversation_id=source_conversation_id,
        channel=channel,
        customer_phone=customer_phone,
        started_at=started_at or timezone.now(),
    )


class ConversationConstraintTests(TestCase):
    def test_default_ingestion_status_is_pending(self):
        conv = _make_conversation(_make_org())
        self.assertEqual(conv.ingestion_status, IngestionStatus.PENDING)

    def test_unique_by_org_source_external_id(self):
        org = _make_org()
        _make_conversation(org, source='quo', source_conversation_id='X')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _make_conversation(org, source='quo', source_conversation_id='X')

    def test_same_external_id_allowed_across_sources(self):
        org = _make_org()
        _make_conversation(org, source='quo', source_conversation_id='X')
        _make_conversation(org, source='callio', source_conversation_id='X')
        self.assertEqual(Conversation.objects.count(), 2)

    def test_same_external_id_allowed_across_orgs(self):
        org_a = _make_org('A')
        org_b = _make_org('B')
        _make_conversation(org_a, source='quo', source_conversation_id='X')
        _make_conversation(org_b, source='quo', source_conversation_id='X')
        self.assertEqual(Conversation.objects.count(), 2)

    def test_customer_phone_defaults_empty_not_null(self):
        # Deliberately no phone.
        conv = Conversation.objects.create(
            org=_make_org(),
            source='quo',
            source_conversation_id='NOPHONE',
            channel=Channel.SMS,
            started_at=timezone.now(),
        )
        self.assertEqual(conv.customer_phone, '')


class ConversationTurnConstraintTests(TestCase):
    def test_unique_by_conversation_and_source_turn_id(self):
        conv = _make_conversation(_make_org())
        ConversationTurn.objects.create(
            conversation=conv,
            source_turn_id='msg_1',
            speaker=Speaker.CUSTOMER,
            direction=Direction.INBOUND,
            text='Hello',
            occurred_at=timezone.now(),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ConversationTurn.objects.create(
                    conversation=conv,
                    source_turn_id='msg_1',
                    speaker=Speaker.AGENT,
                    direction=Direction.OUTBOUND,
                    text='Different content',
                    occurred_at=timezone.now(),
                )

    def test_same_source_turn_id_allowed_across_conversations(self):
        org = _make_org()
        conv_a = _make_conversation(org, source_conversation_id='A')
        conv_b = _make_conversation(org, source_conversation_id='B')
        ts = timezone.now()
        ConversationTurn.objects.create(
            conversation=conv_a,
            source_turn_id='msg_1',
            speaker=Speaker.CUSTOMER,
            direction=Direction.INBOUND,
            occurred_at=ts,
        )
        ConversationTurn.objects.create(
            conversation=conv_b,
            source_turn_id='msg_1',
            speaker=Speaker.CUSTOMER,
            direction=Direction.INBOUND,
            occurred_at=ts,
        )
        self.assertEqual(ConversationTurn.objects.count(), 2)

    def test_deleting_conversation_cascades_to_turns(self):
        conv = _make_conversation(_make_org())
        ConversationTurn.objects.create(
            conversation=conv,
            source_turn_id='msg_1',
            speaker=Speaker.CUSTOMER,
            direction=Direction.INBOUND,
            occurred_at=timezone.now(),
        )
        conv.delete()
        self.assertEqual(ConversationTurn.objects.count(), 0)


class EntityLinkConstraintTests(TestCase):
    def test_dedupe_on_full_tuple(self):
        conv = _make_conversation(_make_org())
        EntityLink.objects.create(
            conversation=conv,
            target_system=TargetSystem.LEADBRIDGE,
            target_type=TargetType.LEAD,
            target_id='lb_lead_1',
            match_method=MatchMethod.PHONE_EXACT,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                EntityLink.objects.create(
                    conversation=conv,
                    target_system=TargetSystem.LEADBRIDGE,
                    target_type=TargetType.LEAD,
                    target_id='lb_lead_1',
                    match_method=MatchMethod.PHONE_EXACT,
                )

    def test_different_match_method_creates_new_row(self):
        conv = _make_conversation(_make_org())
        EntityLink.objects.create(
            conversation=conv,
            target_system=TargetSystem.LEADBRIDGE,
            target_type=TargetType.LEAD,
            target_id='lb_lead_1',
            match_method=MatchMethod.PHONE_EXACT,
        )
        # Same target, different (stronger) match method — allowed.
        EntityLink.objects.create(
            conversation=conv,
            target_system=TargetSystem.LEADBRIDGE,
            target_type=TargetType.LEAD,
            target_id='lb_lead_1',
            match_method=MatchMethod.EXTERNAL_ID,
        )
        self.assertEqual(conv.entity_links.count(), 2)

    def test_confidence_defaults_to_one(self):
        conv = _make_conversation(_make_org())
        link = EntityLink.objects.create(
            conversation=conv,
            target_system=TargetSystem.LEADBRIDGE,
            target_type=TargetType.LEAD,
            target_id='lb_1',
            match_method=MatchMethod.PHONE_EXACT,
        )
        self.assertEqual(link.confidence, 1.0)


class OutcomeSnapshotConstraintTests(TestCase):
    def test_unique_by_conversation_and_captured_at(self):
        conv = _make_conversation(_make_org())
        ts = timezone.now()
        OutcomeSnapshot.objects.create(conversation=conv, captured_at=ts)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OutcomeSnapshot.objects.create(conversation=conv, captured_at=ts)

    def test_multiple_snapshots_with_different_timestamps(self):
        conv = _make_conversation(_make_org())
        now = timezone.now()
        OutcomeSnapshot.objects.create(
            conversation=conv,
            captured_at=now,
            lb_status='new',
            lb_booked=False,
        )
        # A week later, LB shows the lead was booked and SF has revenue.
        OutcomeSnapshot.objects.create(
            conversation=conv,
            captured_at=now + timedelta(days=7),
            lb_status='booked',
            lb_booked=True,
            sf_completed=True,
            sf_revenue_cents=15000,
        )
        self.assertEqual(conv.outcome_snapshots.count(), 2)
        latest = conv.outcome_snapshots.first()  # ordering = ['-captured_at']
        self.assertTrue(latest.lb_booked)
        self.assertEqual(latest.sf_revenue_cents, 15000)

    def test_all_lb_sf_fields_nullable_at_creation(self):
        conv = _make_conversation(_make_org())
        snap = OutcomeSnapshot.objects.create(
            conversation=conv,
            captured_at=timezone.now(),
        )
        # None means unknown; empty string means unknown for text fields.
        self.assertEqual(snap.lb_status, '')
        self.assertIsNone(snap.lb_booked)
        self.assertIsNone(snap.sf_revenue_cents)
