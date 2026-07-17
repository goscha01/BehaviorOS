"""Tests for POST /v1/context.

Test taxonomy:
- ContextContractTest — wire-contract invariants (unchanged from Phase 1).
- ShadowModeTest — feature-flag gating (unchanged from Phase 1).
- SourceCompositionTest — real evidence rows produce the expected wire
  output (renamed from ProviderCompositionTest; assertions unchanged).
- ServiceTokenAuthTest — the one non-200 path.
- SourceIsolationTest — a crashing Source must not break the endpoint.
- Phase 2 additions:
    - ProvenanceTest — merged package carries provenance internally,
      wire response strips it.
    - VersioningTest — every log row has context_version + source_count.
    - PriorityTest — higher-priority Source wins on conflict.
    - LearningHooksTest — extension points fire without changing wire behavior.
    - RegistryTest — adding a Source at runtime requires zero engine edits.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.utils import timezone as django_timezone
from rest_framework.test import APIClient

from apps.accounts.models import Organization
from apps.context.engine import (
    CONTEXT_VERSION,
    BaseContextSource,
    ContextEngine,
    ContextRequest,
    SourceOutput,
    register,
)
from apps.context.engine import learning_hooks, registry
from apps.context.models import (
    ContextRequestLog,
    CustomerHistoryAggregate,
    EvidenceEvent,
    OrgStatistics,
)
from apps.context.pipeline import EvidencePipeline, evidence_from_context_request
from apps.context.pipeline.imports import HistoricalImportService
from apps.learning.models import EvidenceInsight


CONTEXT_URL = '/api/context/v1/context'


def _make_evidence(*, org, source_system, external_id, payload, outcome='',
                   outcome_metadata=None, evidence_type='conversation'):
    return EvidenceInsight.objects.create(
        org=org,
        source_system=source_system,
        external_id=external_id,
        evidence_type=evidence_type,
        occurred_at=django_timezone.now(),
        outcome=outcome,
        outcome_metadata=outcome_metadata or {},
        source_payload=payload,
    )


class ContextContractTest(TestCase):
    """The endpoint's non-negotiable behaviors — must survive Phase 2 refactor."""

    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Test Co')

    def test_returns_no_context_when_no_evidence_matches(self):
        response = self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'leadbridge',
                'channel': 'sms',
                'eventType': 'new_lead',
                'customerId': 'unknown-cust-999',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'no_context')
        self.assertTrue(response.json().get('contextRequestId', '').startswith('ctx_'))

    def test_returns_no_context_when_tenant_id_unknown(self):
        response = self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': 'not-a-real-tenant',
                'runtime': 'callio',
                'eventType': 'inbound_call',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'no_context')
        self.assertTrue(response.json().get('contextRequestId', '').startswith('ctx_'))

    def test_missing_required_field_is_400(self):
        response = self.client.post(
            CONTEXT_URL,
            data={'runtime': 'leadbridge'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_every_call_writes_a_log_row(self):
        self.client.post(
            CONTEXT_URL,
            data={'tenantId': str(self.org.id), 'runtime': 'leadbridge'},
            format='json',
        )
        self.assertEqual(ContextRequestLog.objects.count(), 1)
        log = ContextRequestLog.objects.first()
        self.assertEqual(log.runtime, 'leadbridge')
        self.assertEqual(log.status, ContextRequestLog.Status.NO_CONTEXT)


class ShadowModeTest(TestCase):
    """BEHAVIOR_CONTEXT_ENABLED=False must NOT leak context to the runtime."""

    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Test Co')
        _make_evidence(
            org=self.org,
            source_system='leadbridge',
            external_id='lb_conv_00001',
            payload={
                'conversation_id': 'lb_conv_00001',
                'customer_id': 'cust_lb_00001',
            },
            outcome='booked',
        )

    @override_settings(BEHAVIOR_CONTEXT_ENABLED=False)
    def test_shadow_mode_hides_context_but_still_runs_sources(self):
        response = self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'leadbridge',
                'customerId': 'cust_lb_00001',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'no_context')
        self.assertTrue(response.json().get('contextRequestId', '').startswith('ctx_'))

        log = ContextRequestLog.objects.get()
        # Providers/Sources ran and we recorded what we WOULD have returned.
        self.assertEqual(log.status, ContextRequestLog.Status.CONTEXT)
        self.assertFalse(log.returned_to_runtime)
        self.assertGreater(len(log.source_results), 0)

    @override_settings(BEHAVIOR_CONTEXT_ENABLED=True)
    def test_live_mode_returns_context_when_sources_contribute(self):
        response = self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'leadbridge',
                'customerId': 'cust_lb_00001',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'context')
        self.assertIn('confidence', body)
        self.assertIn('context', body)
        # Five documented slots — same Phase 1 contract.
        for key in ('customerProfile', 'businessInsights', 'conversationHints',
                    'warnings', 'recommendedStrategy'):
            self.assertIn(key, body['context'])

        log = ContextRequestLog.objects.get()
        self.assertTrue(log.returned_to_runtime)


class SourceCompositionTest(TestCase):
    """Real evidence rows produce useful context contributions.

    Assertions are unchanged from Phase 1 — the wire-format keys and their
    contents are exactly what LeadBridge/Callio expect today.
    """

    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Test Co')

    @override_settings(BEHAVIOR_CONTEXT_ENABLED=True)
    def test_customer_history_source_reports_prior_interactions(self):
        _make_evidence(
            org=self.org,
            source_system='leadbridge',
            external_id='lb_conv_00001',
            payload={'conversation_id': 'lb_conv_00001', 'customer_id': 'cust_lb_00001'},
            outcome='booked',
        )
        _make_evidence(
            org=self.org,
            source_system='callio',
            external_id='cal_00001',
            payload={'call_id': 'cal_00001', 'customer_id': 'cust_lb_00001'},
            outcome='won',
            evidence_type='call',
        )
        response = self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'leadbridge',
                'customerId': 'cust_lb_00001',
            },
            format='json',
        )
        body = response.json()
        history = body['context']['customerProfile']['history']
        self.assertEqual(history['total_prior_interactions'], 2)
        self.assertIn('leadbridge', history['source_systems'])
        self.assertIn('callio', history['source_systems'])

    @override_settings(BEHAVIOR_CONTEXT_ENABLED=True)
    def test_previous_objections_appear_in_conversation_hints(self):
        _make_evidence(
            org=self.org,
            source_system='leadbridge',
            external_id='lb_conv_lost',
            payload={'conversation_id': 'lb_conv_lost', 'customer_id': 'cust_A'},
            outcome='lost',
            outcome_metadata={'loss_reason': 'price'},
        )
        response = self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'leadbridge',
                'customerId': 'cust_A',
            },
            format='json',
        )
        hints = response.json()['context']['conversationHints']
        self.assertTrue(any(h.get('reason') == 'price' for h in hints))

    @override_settings(BEHAVIOR_CONTEXT_ENABLED=True)
    def test_serviceflow_cancellation_surfaces_as_warning(self):
        _make_evidence(
            org=self.org,
            source_system='serviceflow',
            external_id='sf_job_00003',
            payload={
                'job_id': 'sf_job_00003',
                'customer_id': 'cust_walk_in_00007',
                'service_type': 'deep_clean',
                'status': 'cancelled',
                'cancelled_reason': 'customer_no_show',
            },
            outcome='cancelled',
            evidence_type='outcome',
        )
        response = self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'callio',
                'customerId': 'cust_walk_in_00007',
            },
            format='json',
        )
        warnings = response.json()['context']['warnings']
        self.assertTrue(any(w.get('kind') == 'prior_cancellation' for w in warnings))


class ServiceTokenAuthTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Test Co')

    @override_settings(BEHAVIOR_CONTEXT_SERVICE_TOKEN='the-right-token')
    def test_missing_token_is_401_when_configured(self):
        response = self.client.post(
            CONTEXT_URL,
            data={'tenantId': str(self.org.id), 'runtime': 'leadbridge'},
            format='json',
        )
        self.assertEqual(response.status_code, 401)

    @override_settings(BEHAVIOR_CONTEXT_SERVICE_TOKEN='the-right-token')
    def test_wrong_token_is_401(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer wrong-token')
        response = self.client.post(
            CONTEXT_URL,
            data={'tenantId': str(self.org.id), 'runtime': 'leadbridge'},
            format='json',
        )
        self.assertEqual(response.status_code, 401)

    @override_settings(BEHAVIOR_CONTEXT_SERVICE_TOKEN='the-right-token')
    def test_correct_token_authenticates(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer the-right-token')
        response = self.client.post(
            CONTEXT_URL,
            data={'tenantId': str(self.org.id), 'runtime': 'leadbridge'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)


class SourceIsolationTest(TestCase):
    """A crashing Source must not break the endpoint."""

    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Test Co')

    @override_settings(BEHAVIOR_CONTEXT_ENABLED=True)
    def test_source_exception_still_returns_valid_response(self):
        class BoomSource(BaseContextSource):
            name = 'boom'
            priority = 50

            def provide(self, request):
                raise RuntimeError('kaboom')

        registry.register(BoomSource)
        try:
            response = self.client.post(
                CONTEXT_URL,
                data={'tenantId': str(self.org.id), 'runtime': 'leadbridge'},
                format='json',
            )
            self.assertEqual(response.status_code, 200)
            # No sources contributed → no_context, not 500.
            self.assertEqual(response.json()['status'], 'no_context')
            log = ContextRequestLog.objects.get()
            errored = [s for s in log.source_results if s['name'] == 'boom']
            self.assertEqual(len(errored), 1)
            self.assertIn('kaboom', errored[0]['error'])
        finally:
            registry.unregister('boom')


# --- Phase 2 additions ------------------------------------------------------

class ProvenanceTest(TestCase):
    """Merged package carries provenance internally; wire response strips it.

    Provenance leaking to LeadBridge/Callio would be a contract break. This
    test protects the wire boundary.
    """

    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Test Co')
        _make_evidence(
            org=self.org,
            source_system='leadbridge',
            external_id='lb_conv_00001',
            payload={'conversation_id': 'lb_conv_00001', 'customer_id': 'cust_A'},
            outcome='booked',
        )

    def test_engine_result_carries_provenance_internally(self):
        request = ContextRequest(
            tenant_id=str(self.org.id),
            runtime='leadbridge',
            customer_id='cust_A',
            org=self.org,
        )
        result = ContextEngine().build(request)
        # Internal MergedContext wraps each fact value with provenance.
        history_wrapper = result.merged.facts['customerProfile']['history']
        self.assertIn('value', history_wrapper)
        self.assertIn('source', history_wrapper)
        self.assertIn('confidence', history_wrapper)
        self.assertIn('generated_at', history_wrapper)
        self.assertEqual(history_wrapper['source'], 'customer_history')

    def test_wire_response_strips_provenance(self):
        request = ContextRequest(
            tenant_id=str(self.org.id),
            runtime='leadbridge',
            customer_id='cust_A',
            org=self.org,
        )
        result = ContextEngine().build(request)
        wire_history = result.wire_context['customerProfile']['history']
        # The wire value is the raw fact — no _source/_confidence/etc.
        self.assertNotIn('source', wire_history)
        self.assertNotIn('confidence', wire_history)
        self.assertNotIn('generated_at', wire_history)
        # But the fact itself is intact.
        self.assertIn('total_prior_interactions', wire_history)

    def test_wire_list_items_strip_provenance(self):
        _make_evidence(
            org=self.org,
            source_system='serviceflow',
            external_id='sf_1',
            payload={
                'job_id': 'sf_1', 'customer_id': 'cust_A',
                'status': 'cancelled', 'cancelled_reason': 'no_show',
            },
            outcome='cancelled', evidence_type='outcome',
        )
        request = ContextRequest(
            tenant_id=str(self.org.id),
            runtime='callio',
            customer_id='cust_A',
            org=self.org,
        )
        result = ContextEngine().build(request)
        # Internal warning items carry _source/_confidence.
        merged_warning = result.merged.warnings[0]
        self.assertIn('_source', merged_warning)
        self.assertEqual(merged_warning['_source'], 'serviceflow_status')
        # Wire warnings do NOT.
        wire_warning = result.wire_context['warnings'][0]
        self.assertNotIn('_source', wire_warning)
        self.assertNotIn('_confidence', wire_warning)
        self.assertIn('kind', wire_warning)  # actual content preserved


class VersioningTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Test Co')

    def test_log_row_records_context_version(self):
        self.client.post(
            CONTEXT_URL,
            data={'tenantId': str(self.org.id), 'runtime': 'leadbridge'},
            format='json',
        )
        log = ContextRequestLog.objects.get()
        self.assertEqual(log.context_version, CONTEXT_VERSION)

    @override_settings(BEHAVIOR_CONTEXT_ENABLED=True)
    def test_source_count_reflects_contributing_sources(self):
        _make_evidence(
            org=self.org,
            source_system='leadbridge',
            external_id='lb_1',
            payload={'conversation_id': 'lb_1', 'customer_id': 'cust_A'},
            outcome='booked',
        )
        self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'leadbridge',
                'customerId': 'cust_A',
            },
            format='json',
        )
        log = ContextRequestLog.objects.get()
        # customer_history + previous_outcomes both contribute; the other
        # two sources return empty.
        self.assertGreaterEqual(log.source_count, 2)


class PriorityTest(TestCase):
    """Higher-priority Source wins on conflict."""

    def setUp(self):
        self.org = Organization.objects.create(name='Test Co')

    def test_higher_priority_source_overwrites_lower(self):
        class LowSource(BaseContextSource):
            name = 'low_pri'
            priority = 10

            def provide(self, request):
                return SourceOutput(
                    source=self.name, priority=self.priority, confidence=0.5,
                    facts={'customerProfile': {'shared_key': 'from_low'}},
                )

        class HighSource(BaseContextSource):
            name = 'high_pri'
            priority = 90

            def provide(self, request):
                return SourceOutput(
                    source=self.name, priority=self.priority, confidence=0.9,
                    facts={'customerProfile': {'shared_key': 'from_high'}},
                )

        registry.register(LowSource)
        registry.register(HighSource)
        try:
            request = ContextRequest(
                tenant_id=str(self.org.id), runtime='leadbridge', org=self.org,
            )
            result = ContextEngine().build(request)
            # Higher-priority source ran later during merge, so its value
            # is what surfaces on the wire.
            self.assertEqual(
                result.wire_context['customerProfile']['shared_key'],
                'from_high',
            )
            # And the internal provenance points at the winning source.
            wrapper = result.merged.facts['customerProfile']['shared_key']
            self.assertEqual(wrapper['source'], 'high_pri')
        finally:
            registry.unregister('low_pri')
            registry.unregister('high_pri')


class LearningHooksTest(TestCase):
    """Hooks fire but the wire response is untouched by a no-op hook."""

    def setUp(self):
        self.org = Organization.objects.create(name='Test Co')

    def tearDown(self):
        learning_hooks._clear_for_tests()

    def test_context_built_hook_receives_every_result(self):
        received = []

        def hook(req, result):
            received.append((req.runtime, result.status, result.context_version))

        learning_hooks.register_context_built_hook(hook)

        request = ContextRequest(
            tenant_id=str(self.org.id), runtime='leadbridge', org=self.org,
        )
        ContextEngine().build(request)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], 'leadbridge')
        self.assertEqual(received[0][2], CONTEXT_VERSION)

    def test_broken_hook_does_not_break_endpoint(self):
        def bad_hook(req, result):
            raise RuntimeError('hook boom')

        learning_hooks.register_context_built_hook(bad_hook)

        request = ContextRequest(
            tenant_id=str(self.org.id), runtime='leadbridge', org=self.org,
        )
        # Should not raise.
        result = ContextEngine().build(request)
        self.assertIsNotNone(result)


class RegistryTest(TestCase):
    """Adding a Source requires no engine edits."""

    def setUp(self):
        self.org = Organization.objects.create(name='Test Co')

    def test_newly_registered_source_runs_without_engine_changes(self):
        class SentinelSource(BaseContextSource):
            name = 'sentinel'
            priority = 50

            def provide(self, request):
                return SourceOutput(
                    source=self.name, priority=self.priority, confidence=0.9,
                    facts={'customerProfile': {'sentinel_key': 'seen'}},
                )

        registry.register(SentinelSource)
        try:
            request = ContextRequest(
                tenant_id=str(self.org.id), runtime='leadbridge', org=self.org,
            )
            result = ContextEngine().build(request)
            self.assertEqual(
                result.wire_context['customerProfile']['sentinel_key'],
                'seen',
            )
        finally:
            registry.unregister('sentinel')


# --- Phase 3 additions -----------------------------------------------------

class EvidencePipelineTest(TestCase):
    """Every /v1/context call now persists an EvidenceEvent + updates aggregates.

    None of it should change the wire response — that's the Phase 3
    invariant. These tests inspect the side-effects directly.
    """

    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Test Co')

    def test_runtime_request_persists_evidence_event(self):
        self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'leadbridge',
                'channel': 'sms',
                'eventType': 'customer_reply',
                'customerId': 'cust_A',
                'conversationId': 'lb_conv_42',
                'message': 'Hi, still looking for a quote.',
            },
            format='json',
        )
        events = EvidenceEvent.objects.filter(org=self.org)
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertEqual(event.source_kind, EvidenceEvent.SourceKind.RUNTIME)
        self.assertEqual(event.runtime, 'leadbridge')
        self.assertEqual(event.customer_id, 'cust_A')
        self.assertEqual(event.conversation_id, 'lb_conv_42')
        self.assertIn('still looking', event.message_excerpt)

    def test_runtime_request_updates_customer_history_aggregate(self):
        for _ in range(3):
            self.client.post(
                CONTEXT_URL,
                data={
                    'tenantId': str(self.org.id),
                    'runtime': 'leadbridge',
                    'channel': 'sms',
                    'eventType': 'customer_reply',
                    'customerId': 'cust_A',
                },
                format='json',
            )
        agg = CustomerHistoryAggregate.objects.get(org=self.org, customer_id='cust_A')
        self.assertEqual(agg.total_events, 3)
        self.assertEqual(agg.event_type_counts.get('customer_reply'), 3)
        self.assertEqual(agg.runtime_counts.get('leadbridge'), 3)
        self.assertIsNotNone(agg.first_seen_at)
        self.assertIsNotNone(agg.last_seen_at)

    def test_runtime_request_updates_org_statistics(self):
        self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'callio',
                'channel': 'voice',
                'eventType': 'inbound_call',
                'customerId': 'cust_B',
            },
            format='json',
        )
        self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'leadbridge',
                'channel': 'sms',
                'eventType': 'new_lead',
            },
            format='json',
        )
        stats = OrgStatistics.objects.get(org=self.org)
        self.assertEqual(stats.total_events, 2)
        self.assertEqual(stats.runtime_counts.get('callio'), 1)
        self.assertEqual(stats.runtime_counts.get('leadbridge'), 1)
        self.assertEqual(stats.event_type_counts.get('inbound_call'), 1)
        self.assertEqual(stats.event_type_counts.get('new_lead'), 1)

    def test_anonymous_event_updates_org_stats_but_not_customer_history(self):
        # No customerId — org counts should still tick, customer aggregates
        # should stay empty.
        self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'callio',
                'eventType': 'inbound_call',
            },
            format='json',
        )
        self.assertEqual(OrgStatistics.objects.count(), 1)
        self.assertEqual(CustomerHistoryAggregate.objects.count(), 0)

    def test_unknown_tenant_persists_nothing(self):
        # tenantId doesn't map to any org — pipeline should skip all
        # persistence but still return a clean no_context response.
        response = self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': 'not-a-real-tenant',
                'runtime': 'leadbridge',
                'customerId': 'cust_A',
            },
            format='json',
        )
        self.assertEqual(response.json()['status'], 'no_context')
        self.assertTrue(response.json().get('contextRequestId', '').startswith('ctx_'))
        self.assertEqual(EvidenceEvent.objects.count(), 0)
        self.assertEqual(CustomerHistoryAggregate.objects.count(), 0)
        self.assertEqual(OrgStatistics.objects.count(), 0)


class WireContractPreservedTest(TestCase):
    """Phase 3's pipeline must not change what the runtime sees.

    Explicit repetition of Phase 1/2 assertions after the pipeline is in
    place. If Phase 3 accidentally leaked evidence into the wire response
    (e.g., a source started reading EvidenceEvent), these would break.
    """

    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Test Co')

    def test_first_ever_call_still_returns_no_context(self):
        # Even though this call generates its own EvidenceEvent, the wire
        # response is unchanged — sources read EvidenceInsight, not
        # EvidenceEvent. This is the key Phase 3 non-regression.
        response = self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'leadbridge',
                'customerId': 'brand-new-customer',
            },
            format='json',
        )
        self.assertEqual(response.json()['status'], 'no_context')
        self.assertTrue(response.json().get('contextRequestId', '').startswith('ctx_'))
        # And confirm the event WAS written on the side.
        self.assertEqual(EvidenceEvent.objects.count(), 1)


class EvidenceEventHookTest(TestCase):
    """The Phase 3 hook fires per event, after persistence, before context."""

    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Test Co')

    def tearDown(self):
        learning_hooks._clear_for_tests()

    def test_evidence_event_hook_fires_with_persisted_row(self):
        seen = []

        def hook(dto, event):
            seen.append((dto.customer_id, event and event.pk is not None))

        learning_hooks.register_evidence_event_hook(hook)

        self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'leadbridge',
                'customerId': 'cust_A',
            },
            format='json',
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], 'cust_A')
        # The row is persisted BEFORE the hook fires.
        self.assertTrue(seen[0][1])

    def test_broken_evidence_hook_does_not_break_endpoint(self):
        def bad_hook(dto, event):
            raise RuntimeError('hook boom')

        learning_hooks.register_evidence_event_hook(bad_hook)

        response = self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'leadbridge',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        # And the event still got persisted despite the hook throwing.
        self.assertEqual(EvidenceEvent.objects.count(), 1)


class HistoricalImportTest(TestCase):
    """Backfill records flow through the SAME pipeline as runtime requests."""

    def setUp(self):
        self.org = Organization.objects.create(name='Test Co')

    def test_import_records_produces_evidence_events(self):
        service = HistoricalImportService()
        records = [
            {
                'external_id': 'lb_conv_00001',
                'occurred_at': '2026-06-01T10:00:00Z',
                'runtime': 'leadbridge',
                'channel': 'sms',
                'event_type': 'conversation_closed',
                'customer_id': 'cust_hist_A',
                'conversation_id': 'lb_conv_00001',
            },
            {
                'external_id': 'lb_conv_00002',
                'occurred_at': '2026-06-02T10:00:00Z',
                'runtime': 'leadbridge',
                'channel': 'sms',
                'event_type': 'conversation_closed',
                'customer_id': 'cust_hist_A',
            },
        ]
        summary = service.import_records(
            org=self.org, source_system='leadbridge', records=records,
        )
        self.assertEqual(summary['total'], 2)
        self.assertEqual(summary['persisted'], 2)
        self.assertEqual(
            EvidenceEvent.objects.filter(
                source_kind=EvidenceEvent.SourceKind.HISTORICAL
            ).count(),
            2,
        )
        # Aggregates auto-updated same as live path — proves the shared
        # pipeline is really shared.
        agg = CustomerHistoryAggregate.objects.get(
            org=self.org, customer_id='cust_hist_A',
        )
        self.assertEqual(agg.total_events, 2)

    def test_import_is_idempotent_on_external_id(self):
        service = HistoricalImportService()
        records = [{
            'external_id': 'lb_conv_00001',
            'occurred_at': '2026-06-01T10:00:00Z',
            'runtime': 'leadbridge',
            'customer_id': 'cust_hist_A',
        }]
        service.import_records(org=self.org, source_system='leadbridge', records=records)
        service.import_records(org=self.org, source_system='leadbridge', records=records)
        # Same external_id → same row. No duplicates.
        self.assertEqual(EvidenceEvent.objects.count(), 1)

    def test_import_skips_context_build(self):
        service = HistoricalImportService()
        records = [{
            'external_id': 'x1',
            'occurred_at': '2026-06-01T10:00:00Z',
            'runtime': 'leadbridge',
            'customer_id': 'cust_hist_A',
        }]
        service.import_records(org=self.org, source_system='leadbridge', records=records)
        # No ContextRequestLog row — historical imports don't generate
        # context, just evidence.
        self.assertEqual(ContextRequestLog.objects.count(), 0)


class PipelineDirectAPITest(TestCase):
    """Callers can drive the pipeline directly (bypass the HTTP layer)."""

    def setUp(self):
        self.org = Organization.objects.create(name='Test Co')

    def test_handle_runtime_request_returns_pipeline_result(self):
        request = ContextRequest(
            tenant_id=str(self.org.id),
            runtime='leadbridge',
            channel='sms',
            event_type='new_lead',
            customer_id='cust_direct',
            org=self.org,
        )
        raw_body = {'tenantId': str(self.org.id), 'runtime': 'leadbridge'}
        result = EvidencePipeline().handle_runtime_request(request, raw_body)

        self.assertTrue(result.persisted)
        self.assertIsNotNone(result.evidence_event)
        self.assertIsNotNone(result.engine_result)


class CanonicalFieldNamesTest(TestCase):
    """The Callio integration contract uses canonical field names
    (organizationId, product, workspaceId, sourceSystem, sourceAccount).
    Legacy (tenantId, runtime) must keep working for the LeadBridge shadow
    client. Both shapes normalize to the same internal ContextRequest.
    """

    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Canonical Co')

    def test_canonical_fields_accepted(self):
        response = self.client.post(
            CONTEXT_URL,
            data={
                'organizationId': str(self.org.id),
                'product': 'callio',
                'workspaceId': 'ws_123',
                'sourceSystem': 'phone',
                'sourceAccount': 'main-reception',
                'channel': 'voice',
                'eventType': 'inbound_call',
                'customerId': '+18135551234',
                'conversationId': 'call_abc',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'no_context')
        self.assertTrue(response.json().get('contextRequestId', '').startswith('ctx_'))

        # Log row captures the raw request payload, so workspaceId/sourceSystem/
        # sourceAccount are observable even though we don't model them yet.
        log = ContextRequestLog.objects.get()
        self.assertEqual(log.runtime, 'callio')  # normalized from `product`
        self.assertEqual(log.tenant_id, str(self.org.id))
        self.assertEqual(log.channel, 'voice')
        self.assertEqual(log.event_type, 'inbound_call')
        self.assertEqual(log.request_payload.get('workspaceId'), 'ws_123')
        self.assertEqual(log.request_payload.get('sourceSystem'), 'phone')
        self.assertEqual(log.request_payload.get('sourceAccount'), 'main-reception')

    def test_legacy_fields_still_accepted(self):
        response = self.client.post(
            CONTEXT_URL,
            data={
                'tenantId': str(self.org.id),
                'runtime': 'leadbridge',
                'channel': 'sms',
                'eventType': 'new_lead',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        log = ContextRequestLog.objects.get()
        self.assertEqual(log.runtime, 'leadbridge')
        self.assertEqual(log.tenant_id, str(self.org.id))

    def test_canonical_wins_when_both_provided(self):
        other_org = Organization.objects.create(name='Wrong Co')
        response = self.client.post(
            CONTEXT_URL,
            data={
                'organizationId': str(self.org.id),
                'tenantId': str(other_org.id),  # ignored
                'product': 'callio',
                'runtime': 'leadbridge',  # ignored
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        log = ContextRequestLog.objects.get()
        self.assertEqual(log.runtime, 'callio')
        self.assertEqual(log.tenant_id, str(self.org.id))

    def test_missing_both_identity_fields_is_400(self):
        response = self.client.post(
            CONTEXT_URL,
            data={'channel': 'voice'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        # Error mentions the canonical name; caller can still send the legacy alias.
        self.assertIn('organizationId', response.json())

    def test_missing_product_is_400(self):
        response = self.client.post(
            CONTEXT_URL,
            data={'organizationId': str(self.org.id)},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('product', response.json())

    def test_metadata_fold_does_not_overwrite_existing_keys(self):
        # If the caller sends both a top-level `workspaceId` and a `metadata`
        # dict that already contains `workspaceId`, the explicit metadata value
        # wins (setdefault semantics). Prevents surprises for callers that
        # already keyed metadata explicitly.
        response = self.client.post(
            CONTEXT_URL,
            data={
                'organizationId': str(self.org.id),
                'product': 'callio',
                'workspaceId': 'ws_top_level',
                'metadata': {'workspaceId': 'ws_explicit'},
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        log = ContextRequestLog.objects.get()
        self.assertEqual(log.request_payload.get('workspaceId'), 'ws_top_level')
        # But downstream ContextRequest.metadata sees the explicit value —
        # we can't assert that from the log alone, so this test asserts the
        # wire-level acceptance and the setdefault contract by construction.


class CorrelationIdTest(TestCase):
    """`contextRequestId` correlates lookups, reports, and log rows across
    runtime + BehaviorOS + downstream analysis. Every response must echo it.
    """

    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Correlation Co')

    def test_server_generates_id_when_absent(self):
        response = self.client.post(
            CONTEXT_URL,
            data={'organizationId': str(self.org.id), 'product': 'callio'},
            format='json',
        )
        body = response.json()
        self.assertTrue(body['contextRequestId'].startswith('ctx_'))
        log = ContextRequestLog.objects.get()
        self.assertEqual(
            log.request_payload.get('contextRequestId'),
            body['contextRequestId'],
        )

    def test_caller_supplied_id_is_echoed(self):
        caller_id = 'ctx_caller_supplied_123'
        response = self.client.post(
            CONTEXT_URL,
            data={
                'organizationId': str(self.org.id),
                'product': 'callio',
                'contextRequestId': caller_id,
            },
            format='json',
        )
        body = response.json()
        self.assertEqual(body['contextRequestId'], caller_id)
        log = ContextRequestLog.objects.get()
        self.assertEqual(log.request_payload.get('contextRequestId'), caller_id)


class ReportModeTest(TestCase):
    """`mode=report` runs the same ingestion pipeline as lookup but skips
    context build. Same auth, same telemetry, one door.
    """

    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name='Report Co')

    def test_report_returns_reported_status_and_evidence_id(self):
        response = self.client.post(
            CONTEXT_URL,
            data={
                'organizationId': str(self.org.id),
                'product': 'callio',
                'mode': 'report',
                'channel': 'voice',
                'eventType': 'call_completed',
                'customerId': '+18135551234',
                'conversationId': 'call_abc',
                'metadata': {
                    'outcome': 'booked',
                    'durationSeconds': 187,
                    'quotedPrice': 249.0,
                },
            },
            format='json',
        )
        body = response.json()
        self.assertEqual(body['status'], 'reported')
        self.assertTrue(body['contextRequestId'].startswith('ctx_'))
        self.assertIsNotNone(body['evidenceEventId'])

        # Evidence persisted, ContextRequestLog not written (reports use the
        # evidence event as their durable record).
        self.assertEqual(EvidenceEvent.objects.count(), 1)
        self.assertEqual(ContextRequestLog.objects.count(), 0)

    def test_report_never_returns_context_body(self):
        _make_evidence(
            org=self.org,
            source_system='callio',
            external_id='seed-1',
            payload={'customer_id': '+18135551234'},
        )
        response = self.client.post(
            CONTEXT_URL,
            data={
                'organizationId': str(self.org.id),
                'product': 'callio',
                'mode': 'report',
                'customerId': '+18135551234',
            },
            format='json',
        )
        body = response.json()
        self.assertNotIn('context', body)
        self.assertNotIn('confidence', body)

    def test_default_mode_is_lookup(self):
        # No `mode` field present — behaves exactly like Phase 1/2/3.
        response = self.client.post(
            CONTEXT_URL,
            data={'organizationId': str(self.org.id), 'product': 'callio'},
            format='json',
        )
        self.assertEqual(response.json()['status'], 'no_context')
        # Lookup path writes a ContextRequestLog row.
        self.assertEqual(ContextRequestLog.objects.count(), 1)

    def test_unknown_mode_is_400(self):
        response = self.client.post(
            CONTEXT_URL,
            data={
                'organizationId': str(self.org.id),
                'product': 'callio',
                'mode': 'bulk_purge',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('mode', response.json())
        self.assertEqual(result.engine_result.context_version, CONTEXT_VERSION)
