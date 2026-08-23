"""Response Timing dimension tests.

Covers the four-state matrix:
  * PASS   — SLA configured + latency ≤ SLA
  * FAIL   — SLA configured + latency > SLA (severity by ratio)
  * UNKNOWN_NOT_EVALUABLE — no SLA configured (latency still in evidence)
  * NOT_APPLICABLE — no customer message OR no agent reply after customer

Uses in-memory fixtures — no LLM, no HTTP.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import TestCase

from apps.accounts.models import Organization
from apps.conversations.models import (
    Conversation,
    ConversationTurn,
    Direction,
    IngestionStatus,
    Speaker,
    TenantConfigSnapshot,
    UnifiedBusinessReconstructionRun,
)
from apps.quality_manager.dimensions.base import State
from apps.quality_manager.dimensions.response_timing import (
    ResponseTimingDimension,
    _read_sla_seconds,
    _severity_from_ratio,
)


BASE = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


def _conv(org, seq=0) -> Conversation:
    return Conversation.objects.create(
        org=org, source='quo',
        source_conversation_id=f'quo:timing-{seq}',
        customer_phone='+18135550000',
        started_at=BASE,
        ingestion_status=IngestionStatus.LINKED,
    )


def _turn(conv, source_turn_id, speaker, seconds_after_base) -> ConversationTurn:
    return ConversationTurn.objects.create(
        conversation=conv,
        source_turn_id=source_turn_id,
        speaker=speaker,
        direction=Direction.UNKNOWN,
        text='(test)',
        occurred_at=BASE + timedelta(seconds=seconds_after_base),
        metadata={},
    )


def _snapshot(org, raw_config):
    return TenantConfigSnapshot.objects.create(
        org=org, source_system='leadbridge',
        tenant_external_id='test-tenant',
        contract_version='v1',
        raw_config=raw_config,
        raw_config_sha256='h' * 64,
    )


def _fake_recon(org):
    # ResponseTiming doesn't consult reconstruction; a bare stub is fine.
    return UnifiedBusinessReconstructionRun.objects.create(
        org=org, tenant_external_id='test-tenant',
        snapshot=_snapshot(org, {'sla': {'first_response_seconds': 300}}),
        reconstruction_version='test',
        status='completed',
    )


class ReadSlaSecondsTests(TestCase):
    def test_reads_user_first_response(self):
        cfg = {'user': {'first_response_sla_seconds': 600}}
        self.assertEqual(_read_sla_seconds(cfg), (600, 'user.first_response_sla_seconds'))

    def test_reads_sla_first_response(self):
        cfg = {'sla': {'first_response_seconds': 300}}
        self.assertEqual(_read_sla_seconds(cfg), (300, 'sla.first_response_seconds'))

    def test_zero_treated_as_absent(self):
        cfg = {'user': {'first_response_sla_seconds': 0}}
        self.assertEqual(_read_sla_seconds(cfg), (None, None))

    def test_missing_returns_none(self):
        self.assertEqual(_read_sla_seconds({}), (None, None))
        self.assertEqual(_read_sla_seconds(None), (None, None))

    def test_non_int_treated_as_absent(self):
        cfg = {'user': {'first_response_sla_seconds': 'many'}}
        self.assertEqual(_read_sla_seconds(cfg), (None, None))

    def test_priority_first_path_wins(self):
        cfg = {
            'user': {'first_response_sla_seconds': 100},
            'sla': {'first_response_seconds': 500},
        }
        # First path in _CONFIGURED_SLA_PATHS wins
        seconds, path = _read_sla_seconds(cfg)
        self.assertEqual(seconds, 100)


class SeverityFromRatioTests(TestCase):
    def test_ratio_lte_1_5_info(self):
        self.assertEqual(_severity_from_ratio(400, 300), 'info')  # 1.33x

    def test_ratio_lte_3_warning(self):
        self.assertEqual(_severity_from_ratio(600, 300), 'warning')  # 2x
        self.assertEqual(_severity_from_ratio(900, 300), 'warning')  # 3x

    def test_ratio_gt_3_critical(self):
        self.assertEqual(_severity_from_ratio(1200, 300), 'critical')  # 4x


class ResponseTimingSeedCasesTests(TestCase):
    """Full four-state matrix on synthetic conversations."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Timing Test Org')
        cls.recon = _fake_recon(cls.org)

    def test_pass_within_configured_sla(self):
        conv = _conv(self.org, 1)
        _turn(conv, 't0', Speaker.CUSTOMER, 0)
        _turn(conv, 't1', Speaker.AGENT, 60)   # 60s reply
        dim = ResponseTimingDimension()
        results = list(dim.evaluate(
            reconstruction_run=self.recon, conversation=conv,
        ))
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r.state, State.PASS)
        self.assertEqual(r.reason_code, 'within_sla')
        # Evidence includes both turns + timing_metric + configured_rule
        kinds = [e.kind for e in r.evidence]
        self.assertEqual(kinds.count('conversation_turn'), 2)
        self.assertIn('timing_metric', kinds)
        self.assertIn('configured_rule', kinds)

    def test_fail_over_configured_sla_info_severity(self):
        conv = _conv(self.org, 2)
        _turn(conv, 't0', Speaker.CUSTOMER, 0)
        _turn(conv, 't1', Speaker.AGENT, 400)  # 400s = 1.33x SLA
        dim = ResponseTimingDimension()
        results = list(dim.evaluate(
            reconstruction_run=self.recon, conversation=conv,
        ))
        r = results[0]
        self.assertEqual(r.state, State.FAIL)
        self.assertEqual(r.severity, 'info')
        self.assertEqual(r.reason_code, 'over_sla')
        self.assertIn('exceeded configured', r.rationale_text)

    def test_fail_critical_at_high_ratio(self):
        conv = _conv(self.org, 3)
        _turn(conv, 't0', Speaker.CUSTOMER, 0)
        _turn(conv, 't1', Speaker.AGENT, 1800)  # 1800s = 6x SLA
        dim = ResponseTimingDimension()
        r = list(dim.evaluate(
            reconstruction_run=self.recon, conversation=conv,
        ))[0]
        self.assertEqual(r.state, State.FAIL)
        self.assertEqual(r.severity, 'critical')

    def test_unknown_when_no_sla_configured(self):
        # Use an org whose latest snapshot has NO SLA field.
        org2 = Organization.objects.create(name='No SLA Org')
        _snapshot(org2, {'user': {'other_field': 'x'}})
        # The dimension doesn't need reconstruction to have matching org
        # since it only reads reconstruction_run to pass through — but
        # snapshot lookup uses conversation.org.
        conv = Conversation.objects.create(
            org=org2, source='quo',
            source_conversation_id='quo:no-sla', started_at=BASE,
            ingestion_status=IngestionStatus.LINKED,
        )
        _turn(conv, 't0', Speaker.CUSTOMER, 0)
        _turn(conv, 't1', Speaker.AGENT, 120)
        dim = ResponseTimingDimension()
        r = list(dim.evaluate(
            reconstruction_run=self.recon, conversation=conv,
        ))[0]
        self.assertEqual(r.state, State.UNKNOWN_NOT_EVALUABLE)
        self.assertEqual(r.reason_code, 'no_configured_response_sla')
        # Latency STILL recorded as evidence
        self.assertTrue(any(
            e.kind == 'timing_metric' for e in r.evidence
        ))

    def test_not_applicable_no_customer_message(self):
        conv = _conv(self.org, 4)
        _turn(conv, 't0', Speaker.AGENT, 0)  # only agent turn
        dim = ResponseTimingDimension()
        r = list(dim.evaluate(
            reconstruction_run=self.recon, conversation=conv,
        ))[0]
        self.assertEqual(r.state, State.NOT_APPLICABLE)
        self.assertEqual(r.reason_code, 'no_customer_message')

    def test_not_applicable_no_agent_reply_after_customer(self):
        conv = _conv(self.org, 5)
        _turn(conv, 't0', Speaker.AGENT, 0)      # outbound opener
        _turn(conv, 't1', Speaker.CUSTOMER, 30)  # customer replies once
        # (no subsequent agent reply)
        dim = ResponseTimingDimension()
        r = list(dim.evaluate(
            reconstruction_run=self.recon, conversation=conv,
        ))[0]
        self.assertEqual(r.state, State.NOT_APPLICABLE)
        self.assertEqual(r.reason_code, 'no_agent_reply_after_customer')

    def test_agent_before_customer_does_not_count_as_reply(self):
        """First customer msg comes AFTER an agent outreach. Only agent
        turns AFTER first customer count as reply."""
        conv = _conv(self.org, 6)
        _turn(conv, 't0', Speaker.AGENT, 0)      # outreach
        _turn(conv, 't1', Speaker.CUSTOMER, 30)  # customer reply
        _turn(conv, 't2', Speaker.AGENT, 90)     # agent's actual reply (60s after customer)
        dim = ResponseTimingDimension()
        r = list(dim.evaluate(
            reconstruction_run=self.recon, conversation=conv,
        ))[0]
        self.assertEqual(r.state, State.PASS)  # 60s ≤ 300s SLA
        # Evidence points to t1 (customer) + t2 (agent), NOT t0 (pre-customer)
        turn_refs = [
            e.ref for e in r.evidence if e.kind == 'conversation_turn'
        ]
        self.assertIn('t1', turn_refs)
        self.assertIn('t2', turn_refs)
        self.assertNotIn('t0', turn_refs)
