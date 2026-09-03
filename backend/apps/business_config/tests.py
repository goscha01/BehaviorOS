"""Focused tests for BusinessConfigProposal synthesizer (V2).

Verifies:
  - status computation with effectiveCurrentValue precedence
  - insufficient_evidence path when tenant is explicit + history is silent
  - confirmed_by_history when history matches effective (even if tenant
    is absent but template supplies default)
  - pricing_model as first-class structured field
  - pricing_examples emit as observed_example, action=no_op
  - policies domain (materials_included, payment_methods) routing
  - services domain (services_offered) routing
  - FAQ prompt drops observed_example fact_kind entries
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import Organization
from apps.context.models import EvidenceEvent
from apps.learning.services.llm_client import LLMResult

from .services import (
    BusinessConfigProposalSynthesizer,
    ProposalRequest,
    _compute_status_scalar,
    _effective_current,
)


class EffectiveCurrentTests(TestCase):
    def test_tenant_wins_when_present(self):
        self.assertEqual(_effective_current(50, 0), 50)
        self.assertEqual(_effective_current(True, False), True)

    def test_template_used_when_tenant_absent(self):
        self.assertEqual(_effective_current(None, 100), 100)
        self.assertEqual(_effective_current(0, True), True)  # zero treated as absent
        self.assertEqual(_effective_current('', 'x'), 'x')

    def test_none_when_both_absent(self):
        self.assertIsNone(_effective_current(None, None))
        self.assertIsNone(_effective_current(0, 0))


class ComputeStatusTests(TestCase):
    def _s(self, *, tenant, tenant_prov, history, template):
        eff = _effective_current(tenant, template)
        return _compute_status_scalar(
            tenant_value=tenant,
            tenant_provenance=tenant_prov,
            historical_value=history,
            template_value=template,
            effective_value=eff,
        )

    def test_confirmed_when_history_matches_explicit_tenant(self):
        self.assertEqual(
            self._s(tenant=50.0, tenant_prov='explicit_owner_input', history=50.0, template=0),
            'confirmed_by_history',
        )

    def test_contradicted_when_history_differs_from_explicit_tenant(self):
        self.assertEqual(
            self._s(tenant=50.0, tenant_prov='explicit_owner_input', history=75.0, template=0),
            'contradicted_by_history',
        )

    def test_confirmed_when_tenant_absent_and_template_default_matches_history(self):
        # Kris's quote_required: tenant=absent, template=True, history=True.
        # Should NOT be proposed_new_from_history — that's a redundant write.
        self.assertEqual(
            self._s(tenant=None, tenant_prov='absent', history=True, template=True),
            'confirmed_by_history',
        )

    def test_contradicted_when_tenant_absent_template_says_true_history_says_false(self):
        self.assertEqual(
            self._s(tenant=None, tenant_prov='absent', history=False, template=True),
            'proposed_new_from_history',  # tenant not explicit, so 'proposed_new'
        )

    def test_insufficient_when_tenant_explicit_history_silent(self):
        self.assertEqual(
            self._s(tenant=50.0, tenant_prov='explicit_owner_input', history=None, template=0),
            'insufficient_evidence',
        )

    def test_template_default_retained_when_tenant_absent_history_silent_template_has_value(self):
        self.assertEqual(
            self._s(tenant=None, tenant_prov='absent', history=None, template=True),
            'template_default_retained',
        )

    def test_numeric_tolerance(self):
        self.assertEqual(
            self._s(tenant=50.0, tenant_prov='explicit_owner_input', history=50.005, template=0),
            'confirmed_by_history',
        )


def _empty_snapshots():
    template = {
        'pricing': {
            'pricingModel': 'hourly',
            'hourlyRate': 0,
            'minimumHours': 0,
            'minimumCharge': 0,
            'quoteRequired': True,
            'currency': 'USD',
            'notes': '',
        },
        'faq': {'customQA': [], 'paymentMethods': [], 'materialsIncluded': None},
    }
    tenant = {
        'pricing': {
            'pricingModel': {'value': 'item_quantity', 'provenance': 'explicit_owner_input'},
            'hourlyRate': {'value': 50, 'provenance': 'explicit_owner_input'},
            'minimumHours': {'value': 2, 'provenance': 'explicit_owner_input'},
            'minimumCharge': {'value': None, 'provenance': 'absent'},
            'quoteRequired': {'value': None, 'provenance': 'absent'},
            'currency': {'value': None, 'provenance': 'absent'},
            'notes': {'value': None, 'provenance': 'absent'},
        },
        'faq': {
            'customQA': {'value': [], 'provenance': 'absent'},
            'paymentMethods': {'value': [], 'provenance': 'absent'},
            'materialsIncluded': {'value': None, 'provenance': 'absent'},
        },
    }
    return template, tenant


class SynthesizerNoEvidenceTests(TestCase):
    def setUp(self):
        self.tenant_id = str(uuid.uuid4())

    def test_no_evidence_emits_stub_pricing_changes(self):
        template, tenant = _empty_snapshots()
        req = ProposalRequest(
            tenant_id=self.tenant_id,
            template_key='handyman',
            template_id=str(uuid.uuid4()),
            template_snapshot=template,
            current_tenant_snapshot=tenant,
            domains=['pricing', 'faq', 'services', 'policies'],
        )
        proposal = BusinessConfigProposalSynthesizer().synthesize(req).proposal

        self.assertEqual(proposal['schemaVersion'], 'business-config-proposal:v1')
        pricing_changes = [c for c in proposal['changes'] if c['domain'] == 'pricing']
        self.assertGreaterEqual(len(pricing_changes), 5)  # incl. pricing_model

        # Kris-like: hourly_rate=50 explicit, history absent → insufficient
        hourly = next(c for c in pricing_changes if c['fieldKey'] == 'hourly_rate')
        self.assertEqual(hourly['currentTenantValue'], 50)
        self.assertEqual(hourly['currentTenantProvenance'], 'explicit_owner_input')
        self.assertEqual(hourly['effectiveCurrentValue'], 50)
        self.assertEqual(hourly['status'], 'insufficient_evidence')

        # quote_required: tenant absent, template=True, history absent →
        # template_default_retained (NOT proposed_new).
        qr = next(c for c in pricing_changes if c['fieldKey'] == 'quote_required')
        self.assertEqual(qr['effectiveCurrentValue'], True)
        self.assertEqual(qr['status'], 'template_default_retained')


class SynthesizerWithMockedLLMTests(TestCase):
    def setUp(self):
        self.tenant_id = str(uuid.uuid4())
        org = Organization.objects.create(id=uuid.UUID(self.tenant_id), name='test')
        EvidenceEvent.objects.create(
            org=org,
            source_kind='runtime',
            runtime='leadbridge',
            channel='sms',
            event_type='call_completed',
            conversation_id='conv-1',
            occurred_at='2026-01-01T00:00:00Z',
            payload={
                'sourceSystem': 'leadbridge-historical',
                'conversationId': 'conv-1',
                'metadata': {
                    'transcript': [
                        {'role': 'customer', 'text': 'How much for a bed frame?'},
                        {'role': 'pro', 'text': 'One hundred dollars for the assembly.'},
                    ],
                    'outcome': 'booked',
                    'category': 'handyman',
                },
            },
        )

    def _mock_pricing_llm(self, **overrides):
        base = {
            'pricing_model': {
                'observed_value': 'flat_project',
                'fact_kind': 'inferred_rule',
                'confidence': 0.9,
                'supporting_conversation_ids': ['conv-1'],
                'representative_snippet': 'pro: One hundred dollars for the assembly.',
                'reasoning': 'Pro quoted flat prices per job across all conversations.',
            },
            'hourly_rate': {
                'observed_value': None,
                'fact_kind': None,
                'confidence': 0.0,
                'supporting_conversation_ids': [],
                'representative_snippet': '',
                'reasoning': 'Pro used flat-project quotes; no hourly rate observed.',
            },
            'minimum_hours': {'observed_value': None, 'confidence': 0.0,
                              'supporting_conversation_ids': [], 'representative_snippet': '',
                              'reasoning': '', 'fact_kind': None},
            'minimum_charge': {'observed_value': None, 'confidence': 0.0,
                               'supporting_conversation_ids': [], 'representative_snippet': '',
                               'reasoning': '', 'fact_kind': None},
            'quote_required': {'observed_value': True, 'fact_kind': 'inferred_rule',
                               'confidence': 0.92, 'supporting_conversation_ids': ['conv-1'],
                               'representative_snippet': 'pro: quoted before booking',
                               'reasoning': 'Pro always quoted before accepting the job.'},
            'materials_included': {'observed_value': False, 'fact_kind': 'explicit_rule',
                                   'confidence': 0.85, 'supporting_conversation_ids': ['conv-1'],
                                   'representative_snippet': 'pro: Materials are not included.',
                                   'reasoning': 'Pro stated materials extra.'},
            'payment_methods': {'observed_value': ['Zelle'], 'fact_kind': 'inferred_rule',
                                'confidence': 0.75, 'supporting_conversation_ids': ['conv-1'],
                                'representative_snippet': 'pro: I can accept Zelle.',
                                'reasoning': 'Pro accepted Zelle payment.'},
            'services_observed': {'observed_value': ['furniture assembly'], 'fact_kind': 'inferred_rule',
                                  'confidence': 0.8, 'supporting_conversation_ids': ['conv-1'],
                                  'representative_snippet': 'pro: bed frame assembly',
                                  'reasoning': 'Pro performed assembly.'},
            'pricing_examples': [
                {'item': 'Bed frame assembly', 'price': 100, 'unit': 'flat',
                 'supporting_conversation_id': 'conv-1',
                 'representative_snippet': 'pro: One hundred dollars.'},
            ],
        }
        base.update(overrides)
        return LLMResult(
            raw_response='',
            parsed_json=base,
            input_tokens=100, output_tokens=200,
            cache_read_tokens=0, cache_write_tokens=0,
            cost_usd=Decimal('0.001'),
            model_used='claude-haiku-4-5-20251001', provider='anthropic',
        )

    def _mock_faq_llm(self, candidates=None):
        return LLMResult(
            raw_response='',
            parsed_json={'candidates': candidates or []},
            input_tokens=100, output_tokens=50,
            cache_read_tokens=0, cache_write_tokens=0,
            cost_usd=Decimal('0.0005'),
            model_used='claude-haiku-4-5-20251001', provider='anthropic',
        )

    @patch('apps.business_config.services.LearningLLMClient.analyze')
    def test_pricing_model_is_first_class(self, mock_analyze):
        mock_analyze.side_effect = [self._mock_pricing_llm(), self._mock_faq_llm()]
        template, tenant = _empty_snapshots()
        req = ProposalRequest(
            tenant_id=self.tenant_id, template_key='handyman',
            template_id=str(uuid.uuid4()),
            template_snapshot=template, current_tenant_snapshot=tenant,
            domains=['pricing', 'faq', 'services', 'policies'],
        )
        proposal = BusinessConfigProposalSynthesizer().synthesize(req).proposal
        pm = next(c for c in proposal['changes']
                  if c['domain'] == 'pricing' and c['fieldKey'] == 'pricing_model')
        # tenant='item_quantity', history='flat_project' → contradicted (different)
        self.assertEqual(pm['currentTenantValue'], 'item_quantity')
        self.assertEqual(pm['historicalObservedValue'], 'flat_project')
        self.assertEqual(pm['status'], 'contradicted_by_history')
        self.assertEqual(pm['factKind'], 'inferred_rule')
        self.assertEqual(pm['reviewPolicy'], 'must_review')

    @patch('apps.business_config.services.LearningLLMClient.analyze')
    def test_pricing_examples_are_observed_examples_no_op(self, mock_analyze):
        mock_analyze.side_effect = [self._mock_pricing_llm(), self._mock_faq_llm()]
        template, tenant = _empty_snapshots()
        req = ProposalRequest(
            tenant_id=self.tenant_id, template_key='handyman',
            template_id=str(uuid.uuid4()),
            template_snapshot=template, current_tenant_snapshot=tenant,
            domains=['pricing', 'faq', 'services', 'policies'],
        )
        proposal = BusinessConfigProposalSynthesizer().synthesize(req).proposal
        examples = [c for c in proposal['changes']
                    if c['domain'] == 'pricing' and c['fieldKey'].startswith('pricing_example:')]
        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0]['factKind'], 'observed_example')
        self.assertEqual(examples[0]['proposedAction']['kind'], 'no_op')
        self.assertEqual(examples[0]['humanLabel'], 'Observed example: Bed frame assembly')

    @patch('apps.business_config.services.LearningLLMClient.analyze')
    def test_policies_domain_populated(self, mock_analyze):
        mock_analyze.side_effect = [self._mock_pricing_llm(), self._mock_faq_llm()]
        template, tenant = _empty_snapshots()
        req = ProposalRequest(
            tenant_id=self.tenant_id, template_key='handyman',
            template_id=str(uuid.uuid4()),
            template_snapshot=template, current_tenant_snapshot=tenant,
            domains=['pricing', 'faq', 'services', 'policies'],
        )
        proposal = BusinessConfigProposalSynthesizer().synthesize(req).proposal
        policy_changes = [c for c in proposal['changes'] if c['domain'] == 'policies']
        materials = next(c for c in policy_changes if c['fieldKey'] == 'materials_included')
        self.assertEqual(materials['historicalObservedValue'], False)
        self.assertEqual(materials['status'], 'proposed_new_from_history')
        payment = next(c for c in policy_changes if c['fieldKey'] == 'payment_methods')
        self.assertEqual(payment['historicalObservedValue'], ['Zelle'])

    @patch('apps.business_config.services.LearningLLMClient.analyze')
    def test_services_domain_populated(self, mock_analyze):
        mock_analyze.side_effect = [self._mock_pricing_llm(), self._mock_faq_llm()]
        template, tenant = _empty_snapshots()
        req = ProposalRequest(
            tenant_id=self.tenant_id, template_key='handyman',
            template_id=str(uuid.uuid4()),
            template_snapshot=template, current_tenant_snapshot=tenant,
            domains=['pricing', 'faq', 'services', 'policies'],
        )
        proposal = BusinessConfigProposalSynthesizer().synthesize(req).proposal
        svc_changes = [c for c in proposal['changes'] if c['domain'] == 'services']
        self.assertEqual(len(svc_changes), 1)
        self.assertEqual(svc_changes[0]['fieldKey'], 'services_offered')
        self.assertEqual(svc_changes[0]['historicalObservedValue'], ['furniture assembly'])

    @patch('apps.business_config.services.LearningLLMClient.analyze')
    def test_faq_drops_observed_example_fact_kind(self, mock_analyze):
        mock_analyze.side_effect = [
            self._mock_pricing_llm(),
            self._mock_faq_llm(candidates=[
                {'field_key': 'valid_faq', 'question': 'Q?', 'answer': 'A.',
                 'fact_kind': 'explicit_rule', 'confidence': 0.9,
                 'supporting_conversation_ids': ['conv-1'],
                 'representative_snippet': 's', 'reasoning': 'r'},
                {'field_key': 'bad_example', 'question': 'Q?', 'answer': 'A.',
                 'fact_kind': 'observed_example', 'confidence': 0.9,
                 'supporting_conversation_ids': ['conv-1'],
                 'representative_snippet': 's', 'reasoning': 'r'},
            ])
        ]
        template, tenant = _empty_snapshots()
        req = ProposalRequest(
            tenant_id=self.tenant_id, template_key='handyman',
            template_id=str(uuid.uuid4()),
            template_snapshot=template, current_tenant_snapshot=tenant,
            domains=['pricing', 'faq', 'services', 'policies'],
        )
        proposal = BusinessConfigProposalSynthesizer().synthesize(req).proposal
        faq_changes = [c for c in proposal['changes'] if c['domain'] == 'faq']
        self.assertEqual(len(faq_changes), 1)  # observed_example dropped
        self.assertEqual(faq_changes[0]['fieldKey'], 'faq:valid_faq')
        # Note about the dropped candidate should appear
        self.assertTrue(any('bad_example' in n and 'observed_example' in n
                            for n in proposal['synthesizerNotes']))
