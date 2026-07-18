"""Tests for the EvidenceEvent → EvidenceInsight promotion service.

Focus areas:
- Basic promotion: eligible runtime event → one insight, terminal
  status=PROMOTED, promoted_at + promotion_checked_at both stamped.
- Idempotency: second run scans zero (PENDING gate + PROMOTED terminal
  state exclude the row).
- Skip flow-through: an event failing the eligibility evaluator lands
  as SKIPPED with the correct reason token — same row, no insight, no
  perpetual-pending state.
- Recovery scenario: manually resetting promotion_status→PENDING
  triggers re-evaluation; existing insight is UPDATED (not re-created)
  via the (source_system, external_id) uniqueness constraint.
- Failure isolation: ingestion raises → event marked FAILED, subsequent
  events still processed, batch reported per-event.
- --limit respected against the eligibility-filtered queue.
- Counters: promotion_attempts_total / promotion_success_total /
  promotion_skipped_total (with per-reason breakdown) all reflect
  the run's real disposition.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Organization
from apps.context.models import EvidenceEvent
from apps.learning.models import EvidenceInsight
from apps.learning.services.eligibility import PromotionDecision
from apps.learning.services.promotion import (
    PromotionResult,
    _evidence_from_event,
    _extract_business_rules_version,
    _extract_outcome,
    _map_evidence_type,
    promote_evidence_events,
)


# A real-looking customer number that ISN'T in the reserved-for-fictional
# +1 555-01XX block or our default synthetic set — so it passes the
# skip_synthetic gate. All service-level tests use this by default.
_REAL_CUSTOMER = '+14155551234'


def _payload_from_callio(
    runtime_outcome: str = 'booking_created',
    context_outcome: str = 'injected',
    lb_version: str = 'v27',
    duration_seconds: int = 187,
) -> dict:
    """Report body shape Callio actually sends. Keeps fixtures close to
    reality so drift between Callio and BehaviorOS breaks tests early."""
    return {
        'mode': 'report',
        'channel': 'voice',
        'product': 'callio',
        'metadata': {
            'runtimeOutcome': runtime_outcome,
            'durationSeconds': duration_seconds,
            'aiDecisionSummary': {
                'bookingCreated': runtime_outcome == 'booking_created',
                'conversationResult': 'booking_created',
            },
            'versions': {
                'behaviorContextVersion': '2.0',
                'behaviorContextRequestId': 'ctx_seed_1234567890abcdef',
                'behaviorContextInjected': True,
                'behaviorContextConfidence': 0.82,
                'behaviorContextOutcome': context_outcome,
                'behaviorContextLatencyMs': 42,
                'leadbridgeConfigVersion': lb_version,
                'playbookVersion': lb_version,
            },
            'timings': {
                'lookupStartedAt': '2026-07-18T14:00:00.000Z',
                'lookupCompletedAt': '2026-07-18T14:00:00.042Z',
                'lookupDeadlineMissed': False,
            },
        },
    }


class PromotionUnitsTest(TestCase):
    def test_event_type_mapping_covers_known_and_falls_back_to_other(self):
        self.assertEqual(_map_evidence_type('call_completed'), EvidenceInsight.EvidenceType.CALL)
        self.assertEqual(_map_evidence_type('inbound_call'), EvidenceInsight.EvidenceType.CALL)
        self.assertEqual(_map_evidence_type('new_lead'), EvidenceInsight.EvidenceType.CONVERSATION)
        self.assertEqual(_map_evidence_type('never_seen'), EvidenceInsight.EvidenceType.OTHER)
        self.assertEqual(_map_evidence_type(''), EvidenceInsight.EvidenceType.OTHER)

    def test_extract_outcome_returns_none_when_no_runtime_outcome(self):
        self.assertIsNone(_extract_outcome({}))
        self.assertIsNone(_extract_outcome({'metadata': {}}))
        self.assertIsNone(_extract_outcome({'metadata': {'runtimeOutcome': ''}}))

    def test_extract_outcome_preserves_related_slots(self):
        outcome = _extract_outcome(_payload_from_callio())
        self.assertEqual(outcome['status'], 'booking_created')
        self.assertIn('aiDecisionSummary', outcome)
        self.assertIn('versions', outcome)
        self.assertIn('timings', outcome)

    def test_extract_business_rules_version_reads_leadbridge_config_version(self):
        self.assertEqual(_extract_business_rules_version(_payload_from_callio(lb_version='v42')), 'v42')
        self.assertEqual(_extract_business_rules_version({'metadata': {'versions': {}}}), '')
        self.assertEqual(_extract_business_rules_version({}), '')


class PromotionServiceTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='PromotionTestCo')

    def _make_event(
        self,
        runtime: str = 'callio',
        event_type: str = 'call_completed',
        source_kind: str = EvidenceEvent.SourceKind.RUNTIME,
        payload: dict | None = None,
        occurred_at=None,
        customer_id: str = _REAL_CUSTOMER,
    ) -> EvidenceEvent:
        return EvidenceEvent.objects.create(
            org=self.org,
            source_kind=source_kind,
            runtime=runtime,
            channel='voice',
            event_type=event_type,
            customer_id=customer_id,
            conversation_id='conv-1',
            occurred_at=occurred_at or timezone.now(),
            payload=payload if payload is not None else _payload_from_callio(),
        )

    def test_eligible_event_gets_promoted_and_marked_terminal(self):
        event = self._make_event()
        result = promote_evidence_events(self.org)

        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.failed, 0)

        event.refresh_from_db()
        self.assertEqual(event.promotion_status, EvidenceEvent.PromotionStatus.PROMOTED)
        self.assertEqual(event.promotion_reason, '')
        self.assertIsNotNone(event.promoted_at)
        self.assertIsNotNone(event.promotion_checked_at)

        insight = EvidenceInsight.objects.get(org=self.org)
        self.assertEqual(insight.source_system, 'callio')
        self.assertEqual(insight.external_id, str(event.id))
        self.assertEqual(insight.evidence_type, EvidenceInsight.EvidenceType.CALL)
        self.assertEqual(insight.outcome, 'booking_created')
        self.assertEqual(insight.source_business_rules_version, 'v27')

    def test_second_run_scans_zero_promoted_or_skipped_terminal_states(self):
        self._make_event()  # eligible → will be promoted
        first = promote_evidence_events(self.org)
        second = promote_evidence_events(self.org)

        self.assertEqual(first.created, 1)
        self.assertEqual(second.scanned, 0)
        self.assertEqual(EvidenceInsight.objects.filter(org=self.org).count(), 1)

    def test_recovery_reset_to_pending_re_promotes_and_updates_existing_insight(self):
        """After manually setting promotion_status→PENDING (e.g. because
        we found a bug and want to re-process), the row is picked up
        again. EvidenceInsight uniqueness ensures the existing row is
        UPDATED, not re-inserted."""
        event = self._make_event()
        promote_evidence_events(self.org)

        EvidenceEvent.objects.filter(pk=event.pk).update(
            promotion_status=EvidenceEvent.PromotionStatus.PENDING,
            promoted_at=None,
            promotion_reason='',
            promotion_checked_at=None,
        )

        result = promote_evidence_events(self.org)
        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 1)
        self.assertEqual(EvidenceInsight.objects.filter(org=self.org).count(), 1)

    def test_historical_events_land_as_skip_unsupported_not_promoted(self):
        event = self._make_event(source_kind=EvidenceEvent.SourceKind.HISTORICAL)
        result = promote_evidence_events(self.org)

        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.promoted, 0)
        self.assertEqual(
            result.skipped_by_reason.get(PromotionDecision.SKIP_UNSUPPORTED, 0), 1,
        )

        event.refresh_from_db()
        self.assertEqual(event.promotion_status, EvidenceEvent.PromotionStatus.SKIPPED)
        self.assertEqual(event.promotion_reason, PromotionDecision.SKIP_UNSUPPORTED)
        self.assertIsNone(event.promoted_at)
        self.assertIsNotNone(event.promotion_checked_at)
        self.assertEqual(EvidenceInsight.objects.count(), 0)

    def test_synthetic_customer_lands_as_skip_synthetic(self):
        self._make_event(customer_id='+18135551234')  # in default synthetic list
        result = promote_evidence_events(self.org)
        self.assertEqual(
            result.skipped_by_reason.get(PromotionDecision.SKIP_SYNTHETIC, 0), 1,
        )

    def test_diagnostic_context_id_lands_as_skip_diagnostic(self):
        payload = _payload_from_callio()
        payload['contextRequestId'] = 'ctx_verifyd1_probe_9999'
        self._make_event(payload=payload)
        result = promote_evidence_events(self.org)
        self.assertEqual(
            result.skipped_by_reason.get(PromotionDecision.SKIP_DIAGNOSTIC, 0), 1,
        )

    def test_limit_is_respected_and_remaining_events_stay_pending(self):
        for _ in range(5):
            self._make_event()
        result = promote_evidence_events(self.org, limit=2)
        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.promoted, 2)
        remaining = EvidenceEvent.objects.filter(
            org=self.org,
            promotion_status=EvidenceEvent.PromotionStatus.PENDING,
        ).count()
        self.assertEqual(remaining, 3)

    def test_per_event_ingest_failure_marks_failed_and_continues_batch(self):
        """A crash inside the ingest upsert must not abort the batch or
        lose the failed event's status — it lands as FAILED, siblings
        proceed normally, and the FAILED event is available for retry
        (operator can reset to PENDING once the root cause is fixed)."""
        good = self._make_event(occurred_at=timezone.now() - timedelta(minutes=2))
        bad = self._make_event(occurred_at=timezone.now() - timedelta(minutes=1))

        original_upsert = None
        call_count = {'n': 0}

        def flaky_upsert(self_, evidence):
            call_count['n'] += 1
            if call_count['n'] == 2:
                raise RuntimeError('simulated failure')
            return original_upsert(self_, evidence)

        from apps.learning.services.ingestion import EvidenceIngestionService
        original_upsert = EvidenceIngestionService._upsert
        with mock.patch.object(EvidenceIngestionService, '_upsert', flaky_upsert):
            result = promote_evidence_events(self.org)

        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.failed, 1)

        good.refresh_from_db()
        bad.refresh_from_db()
        self.assertEqual(good.promotion_status, EvidenceEvent.PromotionStatus.PROMOTED)
        self.assertEqual(bad.promotion_status, EvidenceEvent.PromotionStatus.FAILED)
        self.assertTrue(bad.promotion_reason.startswith('failed:ingest:'))
        self.assertIsNone(bad.promoted_at)
        self.assertIsNotNone(bad.promotion_checked_at)

    def test_counters_reflect_run_disposition(self):
        # 1 eligible, 1 diagnostic, 1 synthetic
        self._make_event()
        payload = _payload_from_callio()
        payload['contextRequestId'] = 'ctx_verify_probe'
        self._make_event(payload=payload)
        self._make_event(customer_id='+18135551234')

        result: PromotionResult = promote_evidence_events(self.org)
        self.assertEqual(result.scanned, 3)
        self.assertEqual(result.promoted, 1)
        self.assertEqual(result.skipped, 2)
        self.assertEqual(
            result.skipped_by_reason[PromotionDecision.SKIP_DIAGNOSTIC], 1,
        )
        self.assertEqual(
            result.skipped_by_reason[PromotionDecision.SKIP_SYNTHETIC], 1,
        )

    @override_settings(LEARNING_MIN_CALL_DURATION_SECONDS=60)
    def test_short_call_lands_as_skip_incomplete(self):
        payload = _payload_from_callio(duration_seconds=10)
        self._make_event(payload=payload)
        result = promote_evidence_events(self.org)
        self.assertEqual(
            result.skipped_by_reason.get(PromotionDecision.SKIP_INCOMPLETE, 0), 1,
        )

    def test_evidence_dto_carries_occurred_at_and_metadata(self):
        event = self._make_event()
        dto = _evidence_from_event(event)
        self.assertEqual(dto.source_system, 'callio')
        self.assertEqual(dto.external_id, str(event.id))
        self.assertEqual(dto.occurred_at, event.occurred_at)
        self.assertEqual(dto.metadata['promoted_from_event_id'], str(event.id))
