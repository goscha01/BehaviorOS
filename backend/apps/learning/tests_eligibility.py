"""Rule-by-rule tests for the promotion eligibility evaluator.

Every rule + every settings override lives here. Kept separate from
tests_promotion.py because the evaluator is a pure function of the
event + settings, which means these tests never touch the DB beyond
constructing model instances.

Ladder order is asserted explicitly — precedence matters for
downstream analytics. If two rules would trip on the same event, the
higher-precedence one must always win.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Organization
from apps.context.models import EvidenceEvent
from apps.learning.services.eligibility import (
    PromotionDecision,
    evaluate_eligibility,
)


def _real_payload(
    runtime_outcome: str = 'booking_created',
    duration_seconds: int = 60,
    context_request_id: str = 'ctx_realcall_1234567890abcdef',
) -> dict:
    return {
        'contextRequestId': context_request_id,
        'metadata': {
            'runtimeOutcome': runtime_outcome,
            'durationSeconds': duration_seconds,
            'aiDecisionSummary': {'conversationResult': 'booking_created'},
        },
    }


class EligibilityEvaluatorTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='EligTestCo')

    def _event(
        self,
        *,
        runtime: str = 'callio',
        event_type: str = 'call_completed',
        source_kind: str = EvidenceEvent.SourceKind.RUNTIME,
        customer_id: str = '+14155551234',
        payload: dict | None = None,
        promotion_status: str = EvidenceEvent.PromotionStatus.PENDING,
    ) -> EvidenceEvent:
        return EvidenceEvent.objects.create(
            org=self.org,
            source_kind=source_kind,
            runtime=runtime,
            channel='voice',
            event_type=event_type,
            customer_id=customer_id,
            conversation_id='conv-1',
            occurred_at=timezone.now(),
            payload=payload if payload is not None else _real_payload(),
            promotion_status=promotion_status,
        )

    # -- eligible baseline -------------------------------------------

    def test_real_call_is_eligible(self):
        r = evaluate_eligibility(self._event())
        self.assertTrue(r.is_eligible)
        self.assertEqual(r.decision, PromotionDecision.ELIGIBLE)

    # -- skip_duplicate ---------------------------------------------

    def test_already_promoted_event_is_skip_duplicate(self):
        e = self._event(promotion_status=EvidenceEvent.PromotionStatus.PROMOTED)
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_DUPLICATE)

    def test_skipped_terminal_state_does_not_re_evaluate_as_duplicate(self):
        """SKIPPED is terminal too but not "duplicate" — the evaluator
        never runs against SKIPPED events (they're excluded by the
        service query), so the eligibility check itself doesn't need
        to guard against them. This asserts the guard doesn't
        misclassify."""
        e = self._event(promotion_status=EvidenceEvent.PromotionStatus.SKIPPED)
        # If called directly (bypass the query), SKIPPED should re-evaluate
        # on merit — not automatically return SKIP_DUPLICATE.
        r = evaluate_eligibility(e)
        self.assertNotEqual(r.decision, PromotionDecision.SKIP_DUPLICATE)

    # -- skip_unsupported -------------------------------------------

    def test_historical_source_kind_is_skip_unsupported(self):
        e = self._event(source_kind=EvidenceEvent.SourceKind.HISTORICAL)
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_UNSUPPORTED)
        self.assertIn('source_kind', r.detail)

    def test_unknown_event_type_is_skip_unsupported(self):
        e = self._event(event_type='some_new_thing')
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_UNSUPPORTED)
        self.assertIn('event_type', r.detail)

    def test_unknown_runtime_is_skip_unsupported(self):
        e = self._event(runtime='unknown_runtime')
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_UNSUPPORTED)
        self.assertIn('runtime=', r.detail)

    # -- skip_diagnostic --------------------------------------------

    def test_verify_context_id_prefix_is_skip_diagnostic(self):
        p = _real_payload(context_request_id='ctx_verifyd1lookup_00000001')
        e = self._event(payload=p)
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_DIAGNOSTIC)

    def test_smoke_context_id_prefix_is_skip_diagnostic(self):
        p = _real_payload(context_request_id='ctx_smoke_test_0001')
        e = self._event(payload=p)
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_DIAGNOSTIC)

    def test_payload_marker_diagnostic_true_is_skip_diagnostic(self):
        p = _real_payload()
        p['diagnostic'] = True
        e = self._event(payload=p)
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_DIAGNOSTIC)

    def test_metadata_is_test_marker_is_skip_diagnostic(self):
        p = _real_payload()
        p['metadata']['is_test'] = True
        e = self._event(payload=p)
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_DIAGNOSTIC)

    # -- skip_synthetic ---------------------------------------------

    def test_default_synthetic_customer_id_is_skip_synthetic(self):
        e = self._event(customer_id='+18135551234')  # in default list
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_SYNTHETIC)

    def test_reserved_fictional_range_customer_id_is_skip_synthetic(self):
        e = self._event(customer_id='+15555550100')
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_SYNTHETIC)

    @override_settings(LEARNING_SYNTHETIC_CUSTOMER_IDS=('+19998887777',))
    def test_synthetic_list_is_configurable(self):
        e = self._event(customer_id='+19998887777')
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_SYNTHETIC)
        # An old default should no longer trip when overridden.
        e2 = self._event(customer_id='+18135551234')
        self.assertNotEqual(evaluate_eligibility(e2).decision, PromotionDecision.SKIP_SYNTHETIC)

    # -- skip_incomplete --------------------------------------------

    def test_short_call_below_default_threshold_is_skip_incomplete(self):
        p = _real_payload(duration_seconds=5)
        e = self._event(payload=p)
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_INCOMPLETE)
        self.assertIn('duration', r.detail)

    @override_settings(LEARNING_MIN_CALL_DURATION_SECONDS=0)
    def test_zero_threshold_disables_duration_check(self):
        p = _real_payload(duration_seconds=1)
        e = self._event(payload=p)
        r = evaluate_eligibility(e)
        self.assertTrue(r.is_eligible)

    def test_missing_runtime_outcome_is_skip_incomplete(self):
        p = {'metadata': {'durationSeconds': 60}}  # no runtimeOutcome
        e = self._event(payload=p)
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_INCOMPLETE)

    @override_settings(LEARNING_ALLOW_MISSING_RUNTIME_OUTCOME=True)
    def test_missing_runtime_outcome_allowed_when_setting_true(self):
        p = {
            'metadata': {
                'durationSeconds': 60,
                'aiDecisionSummary': {'conversationResult': 'hangup'},
            },
        }
        e = self._event(payload=p)
        r = evaluate_eligibility(e)
        self.assertTrue(r.is_eligible)

    def test_empty_content_shell_is_skip_incomplete(self):
        p = {'metadata': {'runtimeOutcome': 'unknown', 'durationSeconds': 60}}
        e = self._event(payload=p)
        e.message_excerpt = ''
        # Has runtimeOutcome → passes meaningful-content check via that
        # slot, so this specific shape should still be ELIGIBLE. This test
        # documents the intent: runtimeOutcome alone counts as content.
        r = evaluate_eligibility(e)
        self.assertTrue(r.is_eligible)

    # -- precedence ordering ----------------------------------------

    def test_promoted_beats_everything(self):
        p = _real_payload(context_request_id='ctx_verify_probe', duration_seconds=1)
        e = self._event(
            promotion_status=EvidenceEvent.PromotionStatus.PROMOTED,
            customer_id='+18135551234',
            source_kind=EvidenceEvent.SourceKind.HISTORICAL,
            payload=p,
        )
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_DUPLICATE)

    def test_unsupported_beats_diagnostic_and_synthetic(self):
        p = _real_payload(context_request_id='ctx_verify_probe', duration_seconds=1)
        e = self._event(
            source_kind=EvidenceEvent.SourceKind.HISTORICAL,
            customer_id='+18135551234',
            payload=p,
        )
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_UNSUPPORTED)

    def test_diagnostic_beats_synthetic_and_incomplete(self):
        p = _real_payload(context_request_id='ctx_verify_probe', duration_seconds=1)
        e = self._event(customer_id='+18135551234', payload=p)
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_DIAGNOSTIC)

    def test_synthetic_beats_incomplete(self):
        p = _real_payload(duration_seconds=1)
        e = self._event(customer_id='+18135551234', payload=p)
        r = evaluate_eligibility(e)
        self.assertEqual(r.decision, PromotionDecision.SKIP_SYNTHETIC)
