"""Tests for the EvidenceEvent → EvidenceInsight promotion service.

Focus areas:
- Basic promotion: one runtime event → one insight, promoted_at stamped.
- Idempotency: rerunning does not create duplicates and updates promoted_at
  only once (via the (source_system, external_id) uniqueness).
- Payload extraction: runtimeOutcome → EvidenceInsight.outcome,
  aiDecisionSummary / versions / timings preserved into outcome_metadata,
  leadbridgeConfigVersion → source_business_rules_version.
- Failure isolation: a broken event doesn't halt the batch; promoted_at
  stays null so the next run retries.
- Historical events are NOT promoted (they follow the adapter path).
- --limit is respected.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Organization
from apps.context.models import EvidenceEvent
from apps.learning.models import EvidenceInsight
from apps.learning.services.promotion import (
    PromotionResult,
    _evidence_from_event,
    _extract_business_rules_version,
    _extract_outcome,
    _map_evidence_type,
    promote_evidence_events,
)


def _payload_from_callio(
    runtime_outcome: str = 'booking_created',
    context_outcome: str = 'injected',
    lb_version: str = 'v27',
) -> dict:
    """Shape of the report body Callio sends. Matches the Callio reporter's
    output; keeping the fixture close to reality catches drift early."""
    return {
        'mode': 'report',
        'channel': 'voice',
        'product': 'callio',
        'metadata': {
            'runtimeOutcome': runtime_outcome,
            'aiDecisionSummary': {
                'bookingCreated': runtime_outcome == 'booking_created',
                'conversationResult': 'booking_created',
            },
            'versions': {
                'behaviorContextVersion': '2.0',
                'behaviorContextRequestId': 'ctx_seed',
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
    ) -> EvidenceEvent:
        return EvidenceEvent.objects.create(
            org=self.org,
            source_kind=source_kind,
            runtime=runtime,
            channel='voice',
            event_type=event_type,
            customer_id='+18135551234',
            conversation_id='conv-1',
            occurred_at=occurred_at or timezone.now(),
            payload=payload if payload is not None else _payload_from_callio(),
        )

    def test_basic_promotion_creates_insight_and_stamps_promoted_at(self):
        event = self._make_event()
        result = promote_evidence_events(self.org)

        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.failed, 0)

        event.refresh_from_db()
        self.assertIsNotNone(event.promoted_at)

        insights = list(EvidenceInsight.objects.filter(org=self.org))
        self.assertEqual(len(insights), 1)
        insight = insights[0]
        self.assertEqual(insight.source_system, 'callio')
        self.assertEqual(insight.external_id, str(event.id))
        self.assertEqual(insight.evidence_type, EvidenceInsight.EvidenceType.CALL)
        self.assertEqual(insight.outcome, 'booking_created')
        self.assertEqual(insight.source_business_rules_version, 'v27')
        self.assertEqual(insight.source_payload, event.payload)

    def test_promoted_events_are_skipped_on_second_run(self):
        self._make_event()
        first = promote_evidence_events(self.org)
        second = promote_evidence_events(self.org)

        self.assertEqual(first.created, 1)
        # Second run should scan zero — promoted_at gate excludes the row.
        self.assertEqual(second.scanned, 0)
        self.assertEqual(second.created, 0)
        self.assertEqual(EvidenceInsight.objects.filter(org=self.org).count(), 1)

    def test_re_promoting_a_manually_reset_event_updates_existing_insight(self):
        """Even if promoted_at gets NULL'd (recovery scenario), the
        EvidenceInsight (source_system, external_id) uniqueness prevents
        duplicate rows — the row is updated, not re-inserted."""
        event = self._make_event()
        promote_evidence_events(self.org)
        # Simulate a recovery: reset promoted_at and rerun.
        EvidenceEvent.objects.filter(pk=event.pk).update(promoted_at=None)

        result = promote_evidence_events(self.org)
        self.assertEqual(result.scanned, 1)
        # First was created; this rerun updates the same row → updated, not created.
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 1)
        self.assertEqual(EvidenceInsight.objects.filter(org=self.org).count(), 1)

    def test_historical_events_are_not_promoted(self):
        self._make_event(source_kind=EvidenceEvent.SourceKind.HISTORICAL)
        result = promote_evidence_events(self.org)
        self.assertEqual(result.scanned, 0)
        self.assertEqual(EvidenceInsight.objects.count(), 0)

    def test_limit_is_respected(self):
        for _ in range(5):
            self._make_event()
        result = promote_evidence_events(self.org, limit=2)
        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.created, 2)
        # Three still on the queue.
        remaining = EvidenceEvent.objects.filter(
            org=self.org, promoted_at__isnull=True,
        ).count()
        self.assertEqual(remaining, 3)

    def test_per_event_failure_leaves_promoted_at_null(self):
        """A crash inside the ingest upsert must not mark the event as
        promoted — otherwise the failed event is lost forever. Simulate
        by making the upsert raise on the second event only."""
        good = self._make_event(occurred_at=timezone.now() - timedelta(minutes=2))
        bad = self._make_event(occurred_at=timezone.now() - timedelta(minutes=1))

        original_upsert = None
        call_count = {'n': 0}

        def flaky_upsert(self_, evidence):
            call_count['n'] += 1
            if call_count['n'] == 2:
                raise RuntimeError('simulated failure')
            return original_upsert(self_, evidence)

        # Late import to keep the patch site local.
        from apps.learning.services.ingestion import EvidenceIngestionService

        original_upsert = EvidenceIngestionService._upsert
        with mock.patch.object(EvidenceIngestionService, '_upsert', flaky_upsert):
            result = promote_evidence_events(self.org)

        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.failed, 1)

        good.refresh_from_db()
        bad.refresh_from_db()
        self.assertIsNotNone(good.promoted_at)
        # bad must stay on the queue so the next run retries it.
        self.assertIsNone(bad.promoted_at)

    def test_result_reports_scanned_and_promoted_counts(self):
        self._make_event()
        self._make_event()
        result: PromotionResult = promote_evidence_events(self.org)
        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.promoted, 2)  # created + updated

    def test_evidence_dto_carries_occurred_at_and_metadata(self):
        event = self._make_event()
        dto = _evidence_from_event(event)
        self.assertEqual(dto.source_system, 'callio')
        self.assertEqual(dto.external_id, str(event.id))
        self.assertEqual(dto.occurred_at, event.occurred_at)
        self.assertEqual(dto.metadata['promoted_from_event_id'], str(event.id))
        self.assertEqual(dto.metadata['context_customer_id'], '+18135551234')
