"""Integration tests for the canonical-context resolver + LB client.

Exercises the full pipeline: fake LB context client + a live
Conversation row + optional caller-supplied conversation
observations → CanonicalConversationContext + persisted
ConversationContext row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import TestCase

from apps.accounts.models import Organization
from apps.conversations.context import resolve_conversation_context
from apps.conversations.context.lb_client import (
    InMemoryLeadBridgeContextClient,
    LbLeadContext,
)
from apps.conversations.context.types import (
    Attr,
    Authority,
    Observation,
)
from apps.conversations.models import (
    ConversationContext,
    Conversation,
    EntityLink,
    IngestionStatus,
    MatchMethod,
    TargetSystem,
    TargetType,
)


BASE = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_conversation(org, source_id: str = 'quo:conv-1') -> Conversation:
    return Conversation.objects.create(
        org=org,
        source='quo',
        source_conversation_id=source_id,
        customer_phone='+18135550100',
        started_at=BASE,
        ingestion_status=IngestionStatus.LINKED,
    )


def _link_lead(conv, lead_id: str) -> EntityLink:
    return EntityLink.objects.create(
        conversation=conv,
        target_system=TargetSystem.LEADBRIDGE,
        target_type=TargetType.LEAD,
        target_id=lead_id,
        match_method=MatchMethod.PHONE_EXACT,
        confidence=1.0,
    )


def _lb_context(
    lead_id: str,
    *,
    bedrooms=None,
    bathrooms=None,
    sqft=None,
    frequency=None,
    updated_at=BASE,
) -> LbLeadContext:
    attrs: dict = {
        'service': None,
        'service_tier': None,
        'bedrooms': None,
        'bathrooms': None,
        'square_footage': None,
        'frequency': None,
        'addons': None,
    }
    if bedrooms is not None:
        attrs['bedrooms'] = {
            'value': bedrooms,
            'source_field': 'lead_details:Bedrooms',
            'raw_value': str(bedrooms),
            'derivation': 'literal',
        }
    if bathrooms is not None:
        attrs['bathrooms'] = {
            'value': bathrooms,
            'source_field': 'lead_details:Bathrooms',
            'raw_value': str(bathrooms),
            'derivation': 'literal',
        }
    if sqft is not None:
        attrs['square_footage'] = {
            'value': sqft,
            'source_field': 'lead_details:Square footage',
            'raw_value': str(sqft),
            'derivation': 'literal',
        }
    if frequency is not None:
        attrs['frequency'] = {
            'value': frequency,
            'source_field': 'lead_details:Frequency',
            'raw_value': frequency,
            'derivation': 'enum_mapping',
        }
    return LbLeadContext(
        lead_id=lead_id,
        platform='thumbtack',
        external_request_id=f'ext-{lead_id}',
        observed_at=updated_at,
        updated_at=updated_at,
        mapping_version='lb-lead-context-v1',
        attributes_raw=attrs,
    )


class ResolverIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Test Org')

    def test_lb_only_no_conversation_observations(self):
        conv = _make_conversation(self.org)
        _link_lead(conv, 'lead-a')
        client = InMemoryLeadBridgeContextClient()
        client.register(_lb_context(
            'lead-a', bedrooms=3, bathrooms=2, sqft=1800, frequency='biweekly',
        ))
        ctx = resolve_conversation_context(
            conv,
            conversation_observations=[],
            lb_context_client=client,
            lb_user_id='lb-user-1',
        )
        self.assertEqual(ctx.get(Attr.BEDROOMS), 3)
        self.assertEqual(ctx.get(Attr.BATHROOMS), 2)
        self.assertEqual(ctx.get(Attr.SQUARE_FOOTAGE), 1800)
        self.assertEqual(ctx.get(Attr.FREQUENCY), 'biweekly')
        # Every canonical value must trace to a source_structured LB obs.
        for attr_name in (Attr.BEDROOMS, Attr.BATHROOMS, Attr.SQUARE_FOOTAGE):
            self.assertEqual(
                ctx.coverage[attr_name]['authority'],
                Authority.SOURCE_STRUCTURED.value,
            )
        # No conflict — only one observation per attribute.
        self.assertEqual(ctx.conflicts, {})

    def test_conversation_llm_cannot_override_lb_for_stable_attribute(self):
        conv = _make_conversation(self.org)
        _link_lead(conv, 'lead-a')
        client = InMemoryLeadBridgeContextClient()
        client.register(_lb_context('lead-a', bedrooms=3))
        conv_obs = [
            Observation(
                attribute=Attr.BEDROOMS,
                value=5,  # LLM disagreement
                source='conversation',
                source_field='turn:t0025',
                observed_at=BASE + timedelta(days=1),
                authority=Authority.CONVERSATION_LLM,
                text='I have 5 bedrooms actually',
            ),
        ]
        ctx = resolve_conversation_context(
            conv,
            conversation_observations=conv_obs,
            lb_context_client=client,
            lb_user_id='lb-user-1',
            use_cache=False,
        )
        self.assertEqual(ctx.get(Attr.BEDROOMS), 3)
        self.assertIn(Attr.BEDROOMS, ctx.conflicts)
        self.assertEqual(
            ctx.conflicts[Attr.BEDROOMS].losing_values, [5],
        )

    def test_conversation_explicit_correction_overrides_older_lb(self):
        conv = _make_conversation(self.org)
        _link_lead(conv, 'lead-a')
        client = InMemoryLeadBridgeContextClient()
        client.register(_lb_context('lead-a', bedrooms=3))
        conv_obs = [
            Observation(
                attribute=Attr.BEDROOMS,
                value=4,
                source='conversation',
                source_field='turn:t0025',
                observed_at=BASE + timedelta(days=30),
                authority=Authority.CONVERSATION_EXPLICIT,
                text='Sorry, we actually have 4 bedrooms',
            ),
        ]
        ctx = resolve_conversation_context(
            conv,
            conversation_observations=conv_obs,
            lb_context_client=client,
            lb_user_id='lb-user-1',
            use_cache=False,
        )
        self.assertEqual(ctx.get(Attr.BEDROOMS), 4)
        # Both observations preserved.
        self.assertEqual(len(ctx.observations[Attr.BEDROOMS]), 2)

    def test_missing_dimensions_stay_missing(self):
        conv = _make_conversation(self.org)
        _link_lead(conv, 'lead-a')
        client = InMemoryLeadBridgeContextClient()
        # LB returned bedrooms only — no bathroom / sqft / frequency data.
        client.register(_lb_context('lead-a', bedrooms=3))
        ctx = resolve_conversation_context(
            conv,
            conversation_observations=[],
            lb_context_client=client,
            lb_user_id='lb-user-1',
        )
        self.assertEqual(ctx.get(Attr.BEDROOMS), 3)
        self.assertIsNone(ctx.get(Attr.BATHROOMS))
        self.assertIsNone(ctx.get(Attr.SQUARE_FOOTAGE))
        self.assertFalse(ctx.coverage[Attr.BATHROOMS]['known'])

    def test_no_lb_client_still_functions_with_conv_observations(self):
        conv = _make_conversation(self.org)
        conv_obs = [
            Observation(
                attribute=Attr.BEDROOMS,
                value=3,
                source='conversation',
                source_field='turn:t0012',
                observed_at=BASE,
                authority=Authority.CONVERSATION_LLM,
            ),
        ]
        ctx = resolve_conversation_context(
            conv,
            conversation_observations=conv_obs,
            lb_context_client=None,
        )
        # Even without LB, the resolver runs and produces a result.
        self.assertEqual(ctx.get(Attr.BEDROOMS), 3)

    def test_cache_hit_reuses_row_when_source_versions_match(self):
        conv = _make_conversation(self.org)
        _link_lead(conv, 'lead-a')
        client = InMemoryLeadBridgeContextClient()
        client.register(_lb_context('lead-a', bedrooms=3))
        r1 = resolve_conversation_context(
            conv,
            conversation_observations=[],
            lb_context_client=client,
            lb_user_id='lb-user-1',
        )
        r2 = resolve_conversation_context(
            conv,
            conversation_observations=[],
            lb_context_client=client,
            lb_user_id='lb-user-1',
        )
        # Same values.
        self.assertEqual(r1.get(Attr.BEDROOMS), r2.get(Attr.BEDROOMS))
        # Exactly one persisted row.
        rows = ConversationContext.objects.filter(conversation=conv)
        self.assertEqual(rows.count(), 1)

    def test_cache_miss_when_lb_updated_at_changes(self):
        conv = _make_conversation(self.org)
        _link_lead(conv, 'lead-a')
        client = InMemoryLeadBridgeContextClient()
        client.register(_lb_context(
            'lead-a', bedrooms=3, updated_at=BASE,
        ))
        r1 = resolve_conversation_context(
            conv,
            conversation_observations=[],
            lb_context_client=client,
            lb_user_id='lb-user-1',
        )
        # Simulate LB updating the lead — bedrooms went from 3 to 4.
        client.register(_lb_context(
            'lead-a', bedrooms=4, updated_at=BASE + timedelta(hours=1),
        ))
        r2 = resolve_conversation_context(
            conv,
            conversation_observations=[],
            lb_context_client=client,
            lb_user_id='lb-user-1',
        )
        self.assertEqual(r1.get(Attr.BEDROOMS), 3)
        self.assertEqual(r2.get(Attr.BEDROOMS), 4)
