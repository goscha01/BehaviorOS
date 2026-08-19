"""End-to-end ingestion pipeline tests.

Uses in-memory resolvers + fetchers to exercise the full path without
external HTTP, but hits real DB (Conversation, ConversationTurn,
EntityLink, OutcomeSnapshot) and the real EvidencePipeline.
"""

from django.test import TestCase

from apps.accounts.models import Organization
from apps.context.models import EvidenceEvent
from apps.conversations.models import (
    Conversation,
    ConversationTurn,
    EntityLink,
    IngestionStatus,
    MatchMethod,
    OutcomeSnapshot,
    TargetSystem,
    TargetType,
)
from apps.conversations.outcomes.base import (
    LeadBridgeOutcome,
    ServiceFlowOutcome,
)
from apps.conversations.outcomes.leadbridge import (
    InMemoryLeadBridgeOutcomeFetcher,
)
from apps.conversations.outcomes.serviceflow import (
    InMemoryServiceFlowOutcomeFetcher,
)
from apps.conversations.resolvers.leadbridge import InMemoryLeadBridgeResolver
from apps.conversations.resolvers.serviceflow import InMemoryServiceFlowResolver
from apps.conversations.services.ingestion import ConversationIngestionPipeline


SAMPLE_QUO_RECORD = {
    'id': 'CN_test_e2e_001',
    'channel': 'sms',
    'workspaceNumber': '+18139212100',
    'participantNumber': '+19045550101',
    'createdAt': '2026-06-10T14:00:00Z',
    'lastActivityAt': '2026-06-10T14:05:00Z',
    'messages': [
        {'id': 'MSG_1', 'body': 'hi looking for a quote', 'direction': 'in',
         'fromNumber': '+19045550101', 'toNumber': '+18139212100',
         'createdAt': '2026-06-10T14:00:00Z'},
        {'id': 'MSG_2', 'body': 'happy to help — deep clean is $220',
         'direction': 'out', 'fromNumber': '+18139212100',
         'toNumber': '+19045550101',
         'createdAt': '2026-06-10T14:03:00Z'},
    ],
}


def _make_org():
    return Organization.objects.create(name='Spotless Homes')


def _make_pipeline(org):
    lb_resolver = InMemoryLeadBridgeResolver()
    lb_resolver.register_lead('lb_lead_e2e', phone_e164='+19045550101')

    sf_resolver = InMemoryServiceFlowResolver()
    sf_resolver.register_entity_graph(
        entities=[
            (TargetType.CUSTOMER, 'sf_cust_e2e'),
            (TargetType.OPPORTUNITY, 'sf_opp_e2e'),
        ],
        lb_lead_id='lb_lead_e2e',
    )

    lb_fetcher = InMemoryLeadBridgeOutcomeFetcher()
    lb_fetcher.register(LeadBridgeOutcome(
        lb_lead_id='lb_lead_e2e', status='booked',
        engaged=True, booked=True,
    ))

    sf_fetcher = InMemoryServiceFlowOutcomeFetcher()
    sf_fetcher.register(ServiceFlowOutcome(
        sf_entity_type=TargetType.OPPORTUNITY, sf_entity_id='sf_opp_e2e',
        opportunity_status='won', booked=True, revenue_cents=22000,
    ))

    return ConversationIngestionPipeline(
        org=org,
        lb_resolver=lb_resolver,
        sf_resolver=sf_resolver,
        lb_outcome_fetcher=lb_fetcher,
        sf_outcome_fetcher=sf_fetcher,
        import_run_id='test-run-1',
    )


class HappyPathTests(TestCase):
    def test_full_pipeline_creates_all_rows(self):
        org = _make_org()
        pipeline = _make_pipeline(org)

        outcome = pipeline.ingest_record(SAMPLE_QUO_RECORD)

        self.assertFalse(outcome.skipped)
        self.assertEqual(outcome.error, '')
        self.assertTrue(outcome.conversation_created)
        self.assertEqual(outcome.turns_created, 2)
        self.assertEqual(outcome.lb_links_created, 1)
        self.assertEqual(outcome.sf_links_created, 2)  # customer + opportunity
        self.assertTrue(outcome.outcome_snapshot_created)
        self.assertIsNotNone(outcome.evidence_event_id)

        # All the stages should have completed.
        self.assertIn('normalize', outcome.stages_completed)
        self.assertIn('persist', outcome.stages_completed)
        self.assertIn('lb_resolve', outcome.stages_completed)
        self.assertIn('sf_resolve', outcome.stages_completed)
        self.assertIn('outcome', outcome.stages_completed)
        self.assertIn('evidence_emit', outcome.stages_completed)

        # Conversation persisted
        conv = Conversation.objects.get(source_conversation_id='CN_test_e2e_001')
        self.assertEqual(conv.ingestion_status, IngestionStatus.EMITTED)
        self.assertEqual(conv.customer_phone, '+19045550101')
        self.assertEqual(conv.import_run_id, 'test-run-1')

        # EntityLinks
        self.assertEqual(conv.entity_links.count(), 3)
        lb_link = conv.entity_links.get(target_system=TargetSystem.LEADBRIDGE)
        self.assertEqual(lb_link.target_id, 'lb_lead_e2e')
        self.assertEqual(lb_link.match_method, MatchMethod.PHONE_EXACT)

        # OutcomeSnapshot
        snap = conv.outcome_snapshots.first()
        self.assertEqual(snap.lb_status, 'booked')
        self.assertTrue(snap.lb_booked)
        self.assertEqual(snap.sf_revenue_cents, 22000)

        # EvidenceEvent emitted for the org, historical kind.
        event = EvidenceEvent.objects.get(id=outcome.evidence_event_id)
        self.assertEqual(event.org, org)
        self.assertEqual(event.source_kind, EvidenceEvent.SourceKind.HISTORICAL)
        self.assertEqual(event.runtime, 'quo')
        self.assertEqual(event.event_type, 'conversation')
        self.assertEqual(event.conversation_id, str(conv.id))
        self.assertEqual(
            event.external_id, 'conv:quo:CN_test_e2e_001',
        )
        # Payload references the entity links + outcome snapshot.
        self.assertEqual(event.payload['conversation']['id'], str(conv.id))
        self.assertEqual(len(event.payload['entity_links']), 3)
        self.assertIsNotNone(event.payload['outcome_snapshot'])
        self.assertEqual(
            event.payload['provenance']['import_run_id'], 'test-run-1',
        )


class UnmatchedTests(TestCase):
    def test_unmatched_conversation_still_ingested(self):
        org = _make_org()
        # Empty in-memory resolvers — no leads registered.
        pipeline = ConversationIngestionPipeline(
            org=org,
            lb_resolver=InMemoryLeadBridgeResolver(),
            sf_resolver=InMemoryServiceFlowResolver(),
            lb_outcome_fetcher=InMemoryLeadBridgeOutcomeFetcher(),
            sf_outcome_fetcher=InMemoryServiceFlowOutcomeFetcher(),
            import_run_id='test-unmatched',
        )
        outcome = pipeline.ingest_record(SAMPLE_QUO_RECORD)
        self.assertTrue(outcome.conversation_created)
        self.assertEqual(outcome.lb_links_created, 0)
        self.assertEqual(outcome.sf_links_created, 0)
        # Snapshot MAY still be created (no LB/SF signal → all null), which
        # is fine — the snapshot records "we looked, found nothing."
        # EvidenceEvent MUST be emitted regardless.
        self.assertIsNotNone(outcome.evidence_event_id)


class IdempotencyTests(TestCase):
    def test_reingest_same_record_creates_no_duplicates(self):
        org = _make_org()
        pipeline = _make_pipeline(org)

        first = pipeline.ingest_record(SAMPLE_QUO_RECORD)
        second = pipeline.ingest_record(SAMPLE_QUO_RECORD)

        # Both attempts succeed but the second creates no new rows.
        self.assertTrue(first.conversation_created)
        self.assertFalse(second.conversation_created)
        self.assertEqual(second.turns_created, 0)
        self.assertEqual(second.turns_already_present, 2)
        self.assertEqual(second.lb_links_created, 0)  # link already present
        self.assertEqual(second.sf_links_created, 0)  # links already present

        # DB state: one Conversation, 2 turns, 3 links.
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(ConversationTurn.objects.count(), 2)
        self.assertEqual(EntityLink.objects.count(), 3)
        # One outcome snapshot (rerun in same second is a no-op).
        self.assertEqual(OutcomeSnapshot.objects.count(), 1)


class NormalizationFailureTests(TestCase):
    def test_bad_record_is_skipped_not_fatal(self):
        org = _make_org()
        pipeline = _make_pipeline(org)
        outcome = pipeline.ingest_record({'not': 'a valid quo record'})
        self.assertTrue(outcome.skipped)
        self.assertTrue(outcome.error.startswith('normalize:'))
        self.assertEqual(Conversation.objects.count(), 0)

    def test_batch_continues_after_one_failure(self):
        org = _make_org()
        pipeline = _make_pipeline(org)
        results = pipeline.ingest_batch([
            {'not': 'valid'},
            SAMPLE_QUO_RECORD,
            {'id': 'CN_empty_turns', 'messages': [], 'calls': []},
        ])
        self.assertEqual(len(results), 3)
        self.assertTrue(results[0].skipped)
        self.assertFalse(results[1].skipped)
        self.assertTrue(results[2].skipped)
        # Only the middle record produced a Conversation.
        self.assertEqual(Conversation.objects.count(), 1)


class TenantIsolationTests(TestCase):
    def test_same_source_id_across_orgs_creates_two_conversations(self):
        org_a = Organization.objects.create(name='Spotless A')
        org_b = Organization.objects.create(name='Spotless B')
        pipeline_a = _make_pipeline(org_a)
        pipeline_b = _make_pipeline(org_b)

        pipeline_a.ingest_record(SAMPLE_QUO_RECORD)
        pipeline_b.ingest_record(SAMPLE_QUO_RECORD)

        self.assertEqual(Conversation.objects.filter(org=org_a).count(), 1)
        self.assertEqual(Conversation.objects.filter(org=org_b).count(), 1)
        # Two evidence events, one per org.
        self.assertEqual(
            EvidenceEvent.objects.filter(org=org_a).count(), 1,
        )
        self.assertEqual(
            EvidenceEvent.objects.filter(org=org_b).count(), 1,
        )
