"""LeadBridge + ServiceFlow resolver tests.

Real HTTP resolvers are only exercised through their stub behavior
(no live endpoints exist yet). Tests focus on the deterministic matching
rules and dedupe semantics.
"""

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Organization
from apps.conversations.models import (
    Channel,
    Conversation,
    EntityLink,
    MatchMethod,
    TargetSystem,
    TargetType,
)
from apps.conversations.resolvers.leadbridge import InMemoryLeadBridgeResolver
from apps.conversations.resolvers.serviceflow import InMemoryServiceFlowResolver
from apps.conversations.services.entity_linking import persist_entity_links


def _make_conv(
    org: Organization,
    *,
    phone: str = '+18135551234',
    email: str = '',
    metadata: dict | None = None,
) -> Conversation:
    return Conversation.objects.create(
        org=org,
        source='quo',
        source_conversation_id='CN_' + phone,
        channel=Channel.SMS,
        customer_phone=phone,
        customer_email=email,
        started_at=timezone.now(),
        metadata=metadata or {},
    )


class LeadBridgePriorityTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Spotless Homes')
        self.resolver = InMemoryLeadBridgeResolver()

    def test_external_id_wins_over_phone(self):
        self.resolver.register_lead(
            'lb_lead_ext',
            external_id='lb-abc',
            phone_e164='+18135551234',
        )
        self.resolver.register_lead(
            'lb_lead_phone_only',
            phone_e164='+18135551234',
        )
        conv = _make_conv(self.org, metadata={'lead_id': 'lb-abc'})
        results = list(self.resolver.resolve(conv))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].target_id, 'lb_lead_ext')
        self.assertEqual(results[0].match_method, MatchMethod.EXTERNAL_ID)

    def test_phone_wins_over_email(self):
        self.resolver.register_lead(
            'lb_by_phone', phone_e164='+18135551234',
        )
        self.resolver.register_lead(
            'lb_by_email', email='customer@example.com',
        )
        conv = _make_conv(self.org, phone='+18135551234', email='customer@example.com')
        results = list(self.resolver.resolve(conv))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].target_id, 'lb_by_phone')
        self.assertEqual(results[0].match_method, MatchMethod.PHONE_EXACT)

    def test_email_used_when_phone_absent(self):
        self.resolver.register_lead('lb_email_only', email='foo@example.com')
        conv = _make_conv(self.org, phone='', email='foo@example.com')
        results = list(self.resolver.resolve(conv))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].match_method, MatchMethod.EMAIL_EXACT)

    def test_no_match_returns_empty(self):
        conv = _make_conv(self.org, phone='+19999999999', email='unknown@example.com')
        results = list(self.resolver.resolve(conv))
        self.assertEqual(results, [])

    def test_email_case_insensitive(self):
        self.resolver.register_lead('lb_case', email='Foo@Example.com')
        conv = _make_conv(self.org, phone='', email='foo@EXAMPLE.com')
        results = list(self.resolver.resolve(conv))
        self.assertEqual(len(results), 1)

    def test_no_fuzzy_match(self):
        # Register with different digits — no substring / prefix matching.
        self.resolver.register_lead('lb_neighbour', phone_e164='+18135551235')
        conv = _make_conv(self.org, phone='+18135551234')
        results = list(self.resolver.resolve(conv))
        self.assertEqual(results, [])


class ServiceFlowResolutionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Spotless Homes')
        self.resolver = InMemoryServiceFlowResolver()

    def test_lb_lead_id_returns_full_entity_graph(self):
        self.resolver.register_entity_graph(
            entities=[
                (TargetType.CUSTOMER, 'sf_cust_1'),
                (TargetType.OPPORTUNITY, 'sf_opp_1'),
                (TargetType.JOB, 'sf_job_1'),
                (TargetType.APPOINTMENT, 'sf_appt_1'),
            ],
            lb_lead_id='lb_lead_abc',
        )
        conv = _make_conv(self.org, phone='+18135551234')
        results = list(self.resolver.resolve(conv, leadbridge_lead_id='lb_lead_abc'))
        self.assertEqual(len(results), 4)
        types = {r.target_type for r in results}
        self.assertEqual(
            types,
            {TargetType.CUSTOMER, TargetType.OPPORTUNITY, TargetType.JOB, TargetType.APPOINTMENT},
        )
        # All linked via the same top-level match method.
        for r in results:
            self.assertEqual(r.match_method, MatchMethod.EXTERNAL_ID)

    def test_falls_back_to_phone_when_no_lb_lead(self):
        self.resolver.register_entity_graph(
            entities=[
                (TargetType.CUSTOMER, 'sf_cust_2'),
                (TargetType.OPPORTUNITY, 'sf_opp_2'),
            ],
            phone_e164='+19045550101',
        )
        conv = _make_conv(self.org, phone='+19045550101')
        results = list(self.resolver.resolve(conv))
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r.match_method, MatchMethod.PHONE_EXACT)

    def test_unknown_entity_type_skipped(self):
        self.resolver.register_entity_graph(
            entities=[
                (TargetType.CUSTOMER, 'sf_cust_3'),
                ('bogus_type', 'ignored_id'),
            ],
            phone_e164='+18135559999',
        )
        conv = _make_conv(self.org, phone='+18135559999')
        results = list(self.resolver.resolve(conv))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].target_id, 'sf_cust_3')

    def test_no_identifiers_returns_empty(self):
        conv = _make_conv(self.org, phone='', email='')
        results = list(self.resolver.resolve(conv))
        self.assertEqual(results, [])


class EntityLinkPersistenceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Spotless Homes')
        self.conv = _make_conv(self.org)
        self.resolver = InMemoryLeadBridgeResolver()
        self.resolver.register_lead('lb_abc', phone_e164='+18135551234')

    def test_persist_creates_link_row(self):
        results = list(self.resolver.resolve(self.conv))
        outcome = persist_entity_links(self.conv, results)
        self.assertEqual(outcome.created, 1)
        self.assertEqual(outcome.already_present, 0)
        self.assertEqual(self.conv.entity_links.count(), 1)
        link = self.conv.entity_links.first()
        self.assertEqual(link.target_system, TargetSystem.LEADBRIDGE)
        self.assertEqual(link.target_type, TargetType.LEAD)
        self.assertEqual(link.target_id, 'lb_abc')
        self.assertEqual(link.match_method, MatchMethod.PHONE_EXACT)

    def test_rerun_is_idempotent(self):
        results = list(self.resolver.resolve(self.conv))
        persist_entity_links(self.conv, results)
        # Second run — same identifiers, same conversation.
        results2 = list(self.resolver.resolve(self.conv))
        outcome = persist_entity_links(self.conv, results2)
        self.assertEqual(outcome.created, 0)
        self.assertEqual(outcome.already_present, 1)
        self.assertEqual(EntityLink.objects.filter(conversation=self.conv).count(), 1)

    def test_new_match_method_adds_row(self):
        # First pass: phone-exact match.
        persist_entity_links(self.conv, self.resolver.resolve(self.conv))

        # Later, an external_id becomes known — same target, different method.
        self.resolver.register_lead('lb_abc', external_id='ext-1')
        self.conv.metadata = {'lead_id': 'ext-1'}
        self.conv.save(update_fields=['metadata'])
        persist_entity_links(self.conv, self.resolver.resolve(self.conv))

        self.assertEqual(self.conv.entity_links.count(), 2)
        methods = set(self.conv.entity_links.values_list('match_method', flat=True))
        self.assertEqual(
            methods, {MatchMethod.PHONE_EXACT, MatchMethod.EXTERNAL_ID}
        )
