"""Customer Question Answered dimension tests.

Exercises the four-state matrix + question-detection heuristic +
the LLM-batching path (LLM stubbed).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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
from apps.quality_manager.dimensions.customer_question_answered import (
    CustomerQuestionAnsweredDimension,
    _is_customer_question,
    _verdict_to_state,
)


BASE = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


def _org():
    return Organization.objects.create(name='Q&A Test Org')


def _conv(org, seq=0) -> Conversation:
    return Conversation.objects.create(
        org=org, source='quo',
        source_conversation_id=f'quo:qa-{seq}',
        customer_phone='+18135550000',
        started_at=BASE,
        ingestion_status=IngestionStatus.LINKED,
    )


def _turn(conv, source_turn_id, speaker, offset_s, text) -> ConversationTurn:
    return ConversationTurn.objects.create(
        conversation=conv,
        source_turn_id=source_turn_id,
        speaker=speaker,
        direction=Direction.UNKNOWN,
        text=text,
        occurred_at=BASE + timedelta(seconds=offset_s),
        metadata={},
    )


def _fake_recon(org):
    snap = TenantConfigSnapshot.objects.create(
        org=org, source_system='leadbridge',
        tenant_external_id='t', contract_version='v1',
        raw_config={}, raw_config_sha256='h' * 64,
    )
    return UnifiedBusinessReconstructionRun.objects.create(
        org=org, tenant_external_id='t',
        snapshot=snap, reconstruction_version='test',
        status='completed',
    )


class IsCustomerQuestionTests(TestCase):
    def test_detects_question_mark(self):
        self.assertTrue(_is_customer_question('Do you clean windows?'))
        self.assertTrue(_is_customer_question('please help me?'))

    def test_detects_interrogative_starter(self):
        self.assertTrue(_is_customer_question('What time can you come'))
        self.assertTrue(_is_customer_question('How much for 3 bedrooms'))
        self.assertTrue(_is_customer_question('Do you offer weekly service'))
        self.assertTrue(_is_customer_question('Can you please confirm'))

    def test_rejects_declaration(self):
        self.assertFalse(_is_customer_question('I need a cleaning next week'))
        self.assertFalse(_is_customer_question('Book me for Tuesday'))
        self.assertFalse(_is_customer_question('Thanks'))

    def test_rejects_empty(self):
        self.assertFalse(_is_customer_question(''))
        self.assertFalse(_is_customer_question(None))
        self.assertFalse(_is_customer_question('   '))


class VerdictToStateTests(TestCase):
    def test_yes_pass(self):
        s, sev, rc = _verdict_to_state('yes')
        self.assertEqual((s, sev), (State.PASS, ''))

    def test_partial_fail_info(self):
        s, sev, rc = _verdict_to_state('partial')
        self.assertEqual((s, sev, rc), (State.FAIL, 'info', 'agent_partial_answer'))

    def test_no_fail_warning(self):
        s, sev, rc = _verdict_to_state('no')
        self.assertEqual((s, sev, rc), (State.FAIL, 'warning', 'agent_did_not_address'))

    def test_unknown_falls_through(self):
        s, sev, rc = _verdict_to_state('unknown')
        self.assertEqual(s, State.UNKNOWN_NOT_EVALUABLE)

    def test_bogus_verdict_is_unknown(self):
        s, sev, rc = _verdict_to_state('badbogus')
        self.assertEqual(s, State.UNKNOWN_NOT_EVALUABLE)


class QuestionAnsweredEvaluateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = _org()
        cls.recon = _fake_recon(cls.org)

    def _fake_llm_analyze(self, verdicts):
        """Returns a patch context manager that stubs LLMClient.analyze
        to return the given per-question verdicts.
        """
        from apps.learning.services.llm_client import LLMResult
        from decimal import Decimal
        parsed = {'verdicts': verdicts}
        fake_result = LLMResult(
            raw_response='',
            parsed_json=parsed,
            input_tokens=0, output_tokens=0,
            cache_read_tokens=0, cache_write_tokens=0,
            cost_usd=Decimal('0'),
            model_used='test',
            provider='stub',
        )
        return patch(
            'apps.learning.services.llm_client.LearningLLMClient.analyze',
            return_value=fake_result,
        )

    def test_not_applicable_no_questions(self):
        conv = _conv(self.org, 1)
        _turn(conv, 't0', Speaker.CUSTOMER, 0, 'I need a cleaning')
        _turn(conv, 't1', Speaker.AGENT, 60, 'Sure, when?')
        dim = CustomerQuestionAnsweredDimension()
        r = list(dim.evaluate(reconstruction_run=self.recon, conversation=conv))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].state, State.NOT_APPLICABLE)
        self.assertEqual(r[0].reason_code, 'no_customer_questions')

    def test_pass_when_llm_says_yes(self):
        conv = _conv(self.org, 2)
        _turn(conv, 't0', Speaker.CUSTOMER, 0, 'Do you clean windows?')
        _turn(conv, 't1', Speaker.AGENT, 60, 'Yes, exterior for $50 extra.')
        dim = CustomerQuestionAnsweredDimension()
        with self._fake_llm_analyze([
            {'q_id': 'q1', 'verdict': 'yes', 'rationale': 'Agent addressed windows directly.'},
        ]):
            r = list(dim.evaluate(reconstruction_run=self.recon, conversation=conv))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].state, State.PASS)
        # Evidence includes question turn + agent reply turn + llm_verdict
        kinds = [e.kind for e in r[0].evidence]
        self.assertEqual(kinds.count('conversation_turn'), 2)
        self.assertIn('llm_verdict', kinds)

    def test_fail_warning_when_llm_says_no(self):
        conv = _conv(self.org, 3)
        _turn(conv, 't0', Speaker.CUSTOMER, 0, 'Do you clean windows?')
        _turn(conv, 't1', Speaker.AGENT, 60, 'Great, when would you like to book?')
        dim = CustomerQuestionAnsweredDimension()
        with self._fake_llm_analyze([
            {'q_id': 'q1', 'verdict': 'no', 'rationale': 'Agent deflected.'},
        ]):
            r = list(dim.evaluate(reconstruction_run=self.recon, conversation=conv))
        self.assertEqual(r[0].state, State.FAIL)
        self.assertEqual(r[0].severity, 'warning')
        self.assertEqual(r[0].reason_code, 'agent_did_not_address')

    def test_fail_info_when_partial(self):
        conv = _conv(self.org, 4)
        _turn(conv, 't0', Speaker.CUSTOMER, 0,
              'How much is a 3BR cleaning and when can you come?')
        _turn(conv, 't1', Speaker.AGENT, 60, "$189 for a 3BR.")
        dim = CustomerQuestionAnsweredDimension()
        with self._fake_llm_analyze([
            {'q_id': 'q1', 'verdict': 'partial', 'rationale': 'Price given but no timing.'},
        ]):
            r = list(dim.evaluate(reconstruction_run=self.recon, conversation=conv))
        self.assertEqual(r[0].state, State.FAIL)
        self.assertEqual(r[0].severity, 'info')

    def test_unknown_when_no_agent_reply(self):
        conv = _conv(self.org, 5)
        _turn(conv, 't0', Speaker.CUSTOMER, 0, 'When can you come?')
        # (no agent reply)
        dim = CustomerQuestionAnsweredDimension()
        r = list(dim.evaluate(reconstruction_run=self.recon, conversation=conv))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].state, State.UNKNOWN_NOT_EVALUABLE)
        self.assertEqual(r[0].reason_code, 'no_agent_reply_after_question')
        # Evidence includes only the question turn (no reply to reference)
        kinds = [e.kind for e in r[0].evidence]
        self.assertEqual(kinds.count('conversation_turn'), 1)

    def test_multi_question_batched_in_one_llm_call(self):
        conv = _conv(self.org, 6)
        _turn(conv, 't0', Speaker.CUSTOMER, 0, 'Do you offer weekly?')
        _turn(conv, 't1', Speaker.AGENT, 30, 'Yes, weekly is available.')
        _turn(conv, 't2', Speaker.CUSTOMER, 60, 'How much?')
        _turn(conv, 't3', Speaker.AGENT, 90, 'It depends on size — what are your bedrooms?')
        dim = CustomerQuestionAnsweredDimension()
        with self._fake_llm_analyze([
            {'q_id': 'q1', 'verdict': 'yes', 'rationale': 'weekly confirmed'},
            {'q_id': 'q2', 'verdict': 'partial', 'rationale': 'asked back rather than answering'},
        ]) as mock_analyze:
            r = list(dim.evaluate(reconstruction_run=self.recon, conversation=conv))
        self.assertEqual(len(r), 2)
        self.assertEqual(mock_analyze.call_count, 1)  # single batched call
        states = sorted([x.state.value for x in r])
        self.assertEqual(states, [State.FAIL.value, State.PASS.value])

    def test_llm_failure_yields_unknown(self):
        conv = _conv(self.org, 7)
        _turn(conv, 't0', Speaker.CUSTOMER, 0, 'Do you clean windows?')
        _turn(conv, 't1', Speaker.AGENT, 60, "Sure, we do")
        dim = CustomerQuestionAnsweredDimension()
        with patch(
            'apps.learning.services.llm_client.LearningLLMClient.analyze',
            side_effect=RuntimeError('llm down'),
        ):
            r = list(dim.evaluate(reconstruction_run=self.recon, conversation=conv))
        self.assertEqual(r[0].state, State.UNKNOWN_NOT_EVALUABLE)
        self.assertEqual(r[0].reason_code, 'llm_evaluation_failed')
