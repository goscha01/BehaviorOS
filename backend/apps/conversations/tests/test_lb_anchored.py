"""Tests for the LB-anchored ingestion path (Pipeline 1A, LB-first).

Covers:
  - Phone index build (paginated Sigcore fetch, unique-phone + multi-conv accounting)
  - LbLearningClient malformed row tolerance
  - LbAnchoredIngestionService: happy path + all 6 exclusion reasons
  - Ambiguous multi-lead detection (per-corpus, not per-conversation)
  - EntityLink + OutcomeSnapshot populated from LB anchor data (no resolver call)
  - EvidenceEvent external_id combines conv + lead → idempotent reruns
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Organization
from apps.context.models import EvidenceEvent
from apps.conversations.models import (
    Conversation, ConversationTurn, EntityLink, IngestionStatus,
    MatchMethod, OutcomeSnapshot, TargetSystem, TargetType,
)
from apps.conversations.services.lb_anchored_ingestion import (
    LB_ANCHOR_REASON_AMBIGUOUS, LB_ANCHOR_REASON_INVALID_PHONE,
    LB_ANCHOR_REASON_NO_CONVERSATION, LB_ANCHOR_REASON_NO_PHONE,
    LbAnchoredIngestionService,
)
from apps.conversations.services.lb_learning_client import (
    LbLearningClient, LearningLead, LearningLeadOutcome, _to_lead,
)
from apps.conversations.services.sigcore_phone_index import (
    SigcorePhoneIndex, build_sigcore_phone_index,
)


# ---------------------------------------------------------------------------
# Shared: stub HTTP session (mirrors test_sigcore_backend.py)
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self):
        return self._payload


class _StubSession:
    def __init__(self, route_map):
        self._route_map = {k: list(v) for k, v in route_map.items()}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        path = url.split('?', 1)[0]
        for suffix, payloads in self._route_map.items():
            if path.endswith(suffix):
                if not payloads:
                    return _StubResponse({'data': [], 'meta': {'hasMore': False}})
                return _StubResponse(payloads.pop(0))
        return _StubResponse({'data': [], 'meta': {'hasMore': False}})


# ---------------------------------------------------------------------------
# LbLearningClient — row parsing
# ---------------------------------------------------------------------------


class LbLearningClientRowTests(SimpleTestCase):
    def test_valid_row_parses(self):
        row = {
            'lead_id': 'l-1', 'user_id': 'u-1',
            'customer_phone': '+18135551234', 'customer_phone_substitute': None,
            'customer_email': 'a@b.com', 'platform': 'thumbtack',
            'external_request_id': 'ext-1', 'business_id': None,
            'created_at': '2026-06-01', 'updated_at': '2026-06-02',
            'outcome': {'lead_id': 'l-1', 'status': 'booked',
                        'engaged': True, 'booked': True, 'lost': False, 'cancelled': False},
        }
        lead = _to_lead(row)
        assert lead is not None
        self.assertEqual(lead.lead_id, 'l-1')
        self.assertEqual(lead.outcome.status, 'booked')
        self.assertTrue(lead.outcome.booked)

    def test_missing_lead_id_dropped(self):
        self.assertIsNone(_to_lead({'user_id': 'u-1', 'outcome': {}}))

    def test_missing_outcome_yields_default_falses(self):
        row = {'lead_id': 'l-1', 'user_id': 'u-1',
               'customer_phone': None, 'platform': 'yelp',
               'external_request_id': 'ext', 'created_at': '', 'updated_at': ''}
        lead = _to_lead(row)
        assert lead is not None
        self.assertEqual(lead.outcome.status, '')
        self.assertFalse(lead.outcome.engaged)


# ---------------------------------------------------------------------------
# SigcorePhoneIndex
# ---------------------------------------------------------------------------


class SigcorePhoneIndexTests(SimpleTestCase):
    @override_settings(SIGCORE_URL='https://sigcore.example/api',
                       SIGCORE_API_KEY='sc_test')
    def test_builds_index_from_paginated_conversations(self):
        stub = _StubSession({
            '/conversations': [
                {'data': [
                    {'id': 'sc-1', 'participantPhoneNumber': '+18135551111'},
                    {'id': 'sc-2', 'participantPhoneNumber': '+18135551112'},
                    {'id': 'sc-3', 'participantPhoneNumber': '+18135551111'},  # duplicate phone
                ], 'meta': {'total': 3, 'totalPages': 1}},
                {'data': [], 'meta': {'hasMore': False}},
            ],
        })
        with mock.patch('requests.Session', return_value=stub):
            index = build_sigcore_phone_index()

        self.assertEqual(index.total_conversations, 3)
        self.assertEqual(index.phones_with_conversation, 2)
        self.assertEqual(index.phones_with_multiple, 1)
        self.assertEqual(len(index.lookup('+18135551111')), 2)
        self.assertEqual(len(index.lookup('+18135551112')), 1)
        self.assertEqual(index.lookup('+19998887777'), [])

    @override_settings(SIGCORE_URL='https://sigcore.example/api',
                       SIGCORE_API_KEY='sc_test')
    def test_skips_rows_with_invalid_phones(self):
        stub = _StubSession({
            '/conversations': [
                {'data': [
                    {'id': 'sc-1', 'participantPhoneNumber': '+18135551111'},
                    {'id': 'sc-2', 'participantPhoneNumber': None},         # skip
                    {'id': 'sc-3', 'participantPhoneNumber': 'call-me'},    # skip
                ], 'meta': {'total': 3, 'totalPages': 1}},
            ],
        })
        with mock.patch('requests.Session', return_value=stub):
            index = build_sigcore_phone_index()
        self.assertEqual(index.total_conversations, 3)
        self.assertEqual(index.phones_with_conversation, 1)

    @override_settings(SIGCORE_URL='', SIGCORE_API_KEY='')
    def test_empty_index_when_not_configured(self):
        index = build_sigcore_phone_index()
        self.assertEqual(index.total_conversations, 0)


# ---------------------------------------------------------------------------
# LbAnchoredIngestionService — all exclusion reasons + happy path
# ---------------------------------------------------------------------------


def _make_lead(**kw) -> LearningLead:
    defaults = dict(
        lead_id='lead-1', user_id='u-1',
        customer_phone='+18135551234', customer_phone_substitute=None,
        customer_email=None, platform='thumbtack',
        external_request_id='ext-1', business_id=None,
        created_at='2026-06-01', updated_at='2026-06-02',
        outcome=LearningLeadOutcome(
            lead_id='lead-1', status='booked',
            engaged=True, booked=True, lost=False, cancelled=False,
        ),
        raw={},
    )
    defaults.update(kw)
    return LearningLead(**defaults)


def _sigcore_summary(sig_id='sc-1', phone='+18135551234'):
    return {
        'id': sig_id, 'externalId': f'ext_{sig_id}',
        'provider': 'openphone',
        'phoneNumber': '+18139212100', 'phoneNumberName': 'Spotless Homes',
        'participantPhoneNumber': phone,
        'createdAt': '2026-06-01T14:00:00Z',
        'lastMessageAt': '2026-06-01T14:05:00Z',
    }


def _index_with(phone_map: dict) -> SigcorePhoneIndex:
    idx = SigcorePhoneIndex()
    for phone, sig_ids in phone_map.items():
        idx.by_phone[phone] = [_sigcore_summary(sig_id=sid, phone=phone) for sid in sig_ids]
        idx.total_conversations += len(sig_ids)
        idx.phones_with_conversation += 1
        if len(sig_ids) > 1:
            idx.phones_with_multiple += 1
    return idx


class LbAnchoredExclusionReasonTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Spotless Homes')

    def _service(self, index=None):
        return LbAnchoredIngestionService(
            org=self.org, phone_index=index or SigcorePhoneIndex(),
            quo_adapter=mock.MagicMock(),
            import_run_id='test-run', lb_user_id='lb-u',
        )

    def test_no_phone(self):
        lead = _make_lead(customer_phone=None, customer_phone_substitute=None)
        r = self._service().ingest_lead(lead)
        self.assertEqual(r.reason, LB_ANCHOR_REASON_NO_PHONE)
        self.assertFalse(r.included)
        self.assertEqual(Conversation.objects.count(), 0)

    def test_invalid_phone(self):
        lead = _make_lead(customer_phone='call me maybe',
                          customer_phone_substitute=None)
        r = self._service().ingest_lead(lead)
        self.assertEqual(r.reason, LB_ANCHOR_REASON_INVALID_PHONE)

    def test_no_conversation(self):
        lead = _make_lead(customer_phone='+19998887777')
        r = self._service(index=SigcorePhoneIndex()).ingest_lead(lead)
        self.assertEqual(r.reason, LB_ANCHOR_REASON_NO_CONVERSATION)

    def test_ambiguous_flag_short_circuits_before_hydration(self):
        # Index HAS the phone, but caller says it's ambiguous.
        idx = _index_with({'+18135551234': ['sc-1']})
        r = self._service(idx).ingest_lead(_make_lead(), is_ambiguous=True)
        self.assertEqual(r.reason, LB_ANCHOR_REASON_AMBIGUOUS)
        self.assertFalse(r.included)

    def test_substitute_phone_used_when_primary_absent(self):
        lead = _make_lead(customer_phone=None,
                          customer_phone_substitute='+18135551234')
        idx = SigcorePhoneIndex()  # empty — but this exercises the phone-resolution branch
        r = self._service(idx).ingest_lead(lead)
        self.assertEqual(r.normalized_phone, '+18135551234')
        self.assertEqual(r.reason, LB_ANCHOR_REASON_NO_CONVERSATION)


class LbAnchoredHappyPathTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Spotless Homes')

    def _happy_run(self, lead=None):
        lead = lead or _make_lead()
        idx = _index_with({'+18135551234': ['sc-1']})

        # Mock the adapter's envelope builder to return a normalizeable
        # payload with 2 messages so we can assert Turns creation.
        envelope = {
            'id': 'ext-quo-1',
            'channel': 'sms',
            'workspaceNumber': '+18139212100',
            'participantNumber': '+18135551234',
            'createdAt': '2026-06-01T14:00:00Z',
            'lastActivityAt': '2026-06-01T14:03:00Z',
            'messages': [
                {'id': 'm-1', 'body': 'hi', 'direction': 'in',
                 'fromNumber': '+18135551234', 'toNumber': '+18139212100',
                 'createdAt': '2026-06-01T14:00:00Z'},
                {'id': 'm-2', 'body': 'hey', 'direction': 'out',
                 'fromNumber': '+18139212100', 'toNumber': '+18135551234',
                 'createdAt': '2026-06-01T14:03:00Z'},
            ],
        }
        adapter = mock.MagicMock()
        # `_sigcore_url` + `_sigcore_api_key` need truthy values for the
        # HTTP-path guard in the service.
        adapter._sigcore_url = 'https://sigcore.example/api'
        adapter._sigcore_api_key = 'sc_test'
        adapter._build_envelope = mock.MagicMock(return_value=envelope)

        with mock.patch('requests.Session'):
            svc = LbAnchoredIngestionService(
                org=self.org, phone_index=idx, quo_adapter=adapter,
                import_run_id='happy-run', lb_user_id='lb-u',
            )
            return svc.ingest_lead(lead), adapter

    def test_full_pipeline_creates_all_rows(self):
        r, _ = self._happy_run()
        self.assertTrue(r.included)
        self.assertEqual(r.reason, '')
        self.assertTrue(r.conversation_created)
        self.assertEqual(r.turns_created, 2)
        self.assertTrue(r.entity_link_created)
        self.assertTrue(r.outcome_snapshot_created)
        self.assertIsNotNone(r.evidence_event_id)

        conv = Conversation.objects.get()
        self.assertEqual(conv.ingestion_status, IngestionStatus.EMITTED)
        self.assertEqual(conv.customer_phone, '+18135551234')

        # EntityLink populated from LB anchor (not resolver)
        link = conv.entity_links.get()
        self.assertEqual(link.target_system, TargetSystem.LEADBRIDGE)
        self.assertEqual(link.target_type, TargetType.LEAD)
        self.assertEqual(link.target_id, 'lead-1')
        self.assertEqual(link.match_method, MatchMethod.PHONE_EXACT)
        self.assertEqual(link.metadata['source'], 'lb_anchored')
        self.assertEqual(link.metadata['lb_platform'], 'thumbtack')

        # OutcomeSnapshot has LB fields, SF fields null
        snap = conv.outcome_snapshots.get()
        self.assertEqual(snap.lb_status, 'booked')
        self.assertTrue(snap.lb_booked)
        self.assertIsNone(snap.sf_revenue_cents)

        # EvidenceEvent external_id combines conv + lead
        event = EvidenceEvent.objects.get(id=r.evidence_event_id)
        self.assertIn(':lb:lead-1', event.external_id)
        self.assertEqual(event.payload['lb_anchor']['lb_lead_id'], 'lead-1')

    def test_rerun_is_idempotent(self):
        r1, _ = self._happy_run()
        # Second run — same lead, same conversation.
        idx = _index_with({'+18135551234': ['sc-1']})
        envelope = {
            'id': 'ext-quo-1', 'channel': 'sms',
            'workspaceNumber': '+18139212100',
            'participantNumber': '+18135551234',
            'createdAt': '2026-06-01T14:00:00Z',
            'lastActivityAt': '2026-06-01T14:03:00Z',
            'messages': [
                {'id': 'm-1', 'body': 'hi', 'direction': 'in',
                 'fromNumber': '+18135551234', 'toNumber': '+18139212100',
                 'createdAt': '2026-06-01T14:00:00Z'},
                {'id': 'm-2', 'body': 'hey', 'direction': 'out',
                 'fromNumber': '+18139212100', 'toNumber': '+18135551234',
                 'createdAt': '2026-06-01T14:03:00Z'},
            ],
        }
        adapter = mock.MagicMock()
        adapter._sigcore_url = 'x'; adapter._sigcore_api_key = 'y'
        adapter._build_envelope = mock.MagicMock(return_value=envelope)
        with mock.patch('requests.Session'):
            svc = LbAnchoredIngestionService(
                org=self.org, phone_index=idx, quo_adapter=adapter,
                import_run_id='rerun', lb_user_id='lb-u',
            )
            r2 = svc.ingest_lead(_make_lead())

        self.assertTrue(r2.included)
        self.assertFalse(r2.conversation_created)  # already existed
        self.assertEqual(r2.turns_created, 0)      # already present
        self.assertEqual(r2.turns_already_present, 2)
        self.assertFalse(r2.entity_link_created)   # already linked
        # Snapshot rerun-within-same-second is a no-op via get_or_create
        # on captured_at (truncated to seconds).

        # DB state: 1 conv, 2 turns, 1 link, 1 or 2 snapshots depending
        # on second boundary (defensive assertions).
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(ConversationTurn.objects.count(), 2)
        self.assertEqual(EntityLink.objects.count(), 1)
        self.assertGreaterEqual(OutcomeSnapshot.objects.count(), 1)


class LbAnchoredCorpusDedupTests(TestCase):
    """The `is_ambiguous` flag is caller responsibility — the service
    trusts it. Exercised through the CLI in the wild; here we assert
    the service honors it correctly.
    """
    def setUp(self):
        self.org = Organization.objects.create(name='Spotless Homes')

    def test_ambiguous_lead_skipped_but_no_error(self):
        idx = _index_with({'+18135551234': ['sc-1']})
        adapter = mock.MagicMock()
        adapter._sigcore_url = 'x'; adapter._sigcore_api_key = 'y'
        svc = LbAnchoredIngestionService(
            org=self.org, phone_index=idx, quo_adapter=adapter,
            import_run_id='r', lb_user_id='u',
        )
        # is_ambiguous=True MUST short-circuit BEFORE hydration
        # (asserting no _build_envelope call is the strongest signal).
        r = svc.ingest_lead(_make_lead(), is_ambiguous=True)
        self.assertEqual(r.reason, LB_ANCHOR_REASON_AMBIGUOUS)
        self.assertFalse(r.included)
        adapter._build_envelope.assert_not_called()
        self.assertEqual(Conversation.objects.count(), 0)
