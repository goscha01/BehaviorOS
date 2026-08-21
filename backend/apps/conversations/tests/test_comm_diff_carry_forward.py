"""Regression tests — server-side approval carry-forward in
`build_diffs()` per the 2026-08-21 reviewer directive.

Contract:
  Source of truth is the active TenantBehaviorProfile, NOT any prior
  CommunicationProfileDiff row. A re-analysis that reaches the SAME
  conclusion must preserve the owner's decision; a materially changed
  conclusion must force a fresh review.

Five cases exercised:
  1. Same-value           → carry as ACCEPTED, prior payload copied
  2. Changed-value        → PENDING with prior-decision narrative
  3. Same-as-default      → PENDING flagged as "revert candidate"
                            (approval on TBP is preserved)
  4. Insufficient-evidence→ PENDING flagged as "evidence weakened"
  5. No prior approval    → PENDING, normal path
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Organization
from apps.conversations.communication_profile.diff import build_diffs
from apps.conversations.models import (
    CommunicationProfileDiff, CommunicationProfileRun, LearningCorpus,
    TenantBehaviorProfile,
)

TENANT = 'test-tenant-carry-forward'


class CommDiffCarryForwardTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='CarryForward Test Org')
        self.corpus = LearningCorpus.objects.create(
            org=self.org, name='test', version='v1',
            selection_criteria={}, member_count=100,
        )
        self.run = CommunicationProfileRun.objects.create(
            org=self.org, corpus=self.corpus,
            tenant_external_id=TENANT,
            profile_version='communication-profile-v1',
            status=CommunicationProfileRun.Status.COMPLETED,
            corpus_conversations=100,
            agent_turns_scanned=2000,
        )

    def _seed_tbp_with_override(
        self, *, dimension: str, observed_value, category='DIFFERENT_FROM_DEFAULT',
        support_n=100,
    ):
        """Persist a TenantBehaviorProfile whose communication_overrides
        already contains an active approval for `dimension`."""
        now = timezone.now()
        return TenantBehaviorProfile.objects.create(
            org=self.org, tenant_external_id=TENANT,
            base_template_version='leadbridge-playbook-v1',
            profile_version=1,
            communication_overrides=[
                {
                    'id': 'override-1',
                    'source': 'communication_diff',
                    'source_id': 'prior-diff-uuid',
                    'dimension': dimension,
                    'category': category,
                    'default_value': {'value': 3},
                    'observed_value': {'value': observed_value},
                    'support_n': support_n,
                    'payload': {
                        'consumer': 'both',
                        'callio': {'dimension': dimension, 'value': observed_value},
                        'leadbridge': {'section': 'x', 'custom_instruction_text': 'y'},
                    },
                    'approved_at': (now - timedelta(hours=6)).isoformat(),
                    'approved_by': 'owner-original@example.com',
                    'was_edited': False,
                },
            ],
            generated_at=now - timedelta(hours=6),
            approved_at=now - timedelta(hours=6),
        )

    # ─── Case 1 ──────────────────────────────────────────────────

    def test_same_value_carries_approval_forward_as_accepted(self):
        """When fresh run reproduces the previously-approved value, the
        new diff is created as ACCEPTED and carries the prior payload."""
        self._seed_tbp_with_override(
            dimension='response_style.typical_agent_sentences',
            observed_value=2,
        )
        self.run.profile_json = {
            'response_style': {
                'typical_agent_sentences': {
                    'value': 2,
                    'support_n': 3765,
                    'confidence': 'HIGH',
                    'evidence_conversation_ids': [],
                },
            },
        }
        self.run.save()

        diffs = build_diffs(run=self.run)
        d = next(x for x in diffs
                 if x.dimension == 'response_style.typical_agent_sentences')
        self.assertEqual(
            d.review_state, CommunicationProfileDiff.ReviewState.ACCEPTED,
        )
        self.assertIn('Previously approved', d.narrative)
        self.assertIn('Confirmed again', d.narrative)
        # Prior payload copied for provenance chain.
        self.assertEqual(
            d.owner_edited_payload.get('callio', {}).get('value'), 2,
        )

    # ─── Case 2 ──────────────────────────────────────────────────

    def test_changed_value_forces_fresh_pending(self):
        """Fresh evidence changes the recommendation → do NOT silently
        carry approval. Force fresh review."""
        self._seed_tbp_with_override(
            dimension='response_style.typical_agent_sentences',
            observed_value=2,   # owner previously approved 2 sentences
            support_n=3765,
        )
        self.run.profile_json = {
            'response_style': {
                'typical_agent_sentences': {
                    'value': 4,   # NEW analysis: 4 sentences
                    'support_n': 500,
                    'confidence': 'HIGH',
                    'evidence_conversation_ids': [],
                },
            },
        }
        self.run.save()

        diffs = build_diffs(run=self.run)
        d = next(x for x in diffs
                 if x.dimension == 'response_style.typical_agent_sentences')
        self.assertEqual(
            d.review_state, CommunicationProfileDiff.ReviewState.PENDING,
        )
        # Narrative surfaces the prior decision so owner has context.
        self.assertIn('Previously approved as 2', d.narrative)
        self.assertIn('4', d.narrative)
        self.assertIsNone(d.owner_edited_payload)

    # ─── Case 3 ──────────────────────────────────────────────────

    def test_same_as_default_with_active_approval_flags_for_review(self):
        """New run says value now matches default. Preserve the TBP
        approval and mark the fresh diff as PENDING with a
        revert-candidate narrative."""
        self._seed_tbp_with_override(
            dimension='response_style.asks_one_question_at_a_time',
            observed_value=True,
        )
        # Fresh run: value is now the default (True from the LB default).
        self.run.profile_json = {
            'response_style': {
                'asks_one_question_at_a_time': {
                    'value': True,
                    'support_n': 100,
                    'confidence': 'HIGH',
                    'evidence_conversation_ids': [],
                },
            },
        }
        self.run.save()

        diffs = build_diffs(run=self.run)
        d = next(x for x in diffs
                 if x.dimension == 'response_style.asks_one_question_at_a_time')
        self.assertEqual(d.category,
                         CommunicationProfileDiff.Category.SAME_AS_DEFAULT)
        self.assertEqual(
            d.review_state, CommunicationProfileDiff.ReviewState.PENDING,
        )
        self.assertIn('revert', d.narrative.lower())
        # TBP is untouched: approval still active on the profile.
        tbp = TenantBehaviorProfile.objects.filter(
            tenant_external_id=TENANT).order_by('-profile_version').first()
        self.assertEqual(len(tbp.communication_overrides), 1)

    # ─── Case 4 ──────────────────────────────────────────────────

    def test_insufficient_evidence_with_active_approval_preserves_flag(self):
        """Fresh analysis lost evidence for a previously-approved
        dimension. Approval stays; fresh diff flagged."""
        self._seed_tbp_with_override(
            dimension='objection_style.acknowledge_before_responding',
            observed_value=False,
            support_n=40,
        )
        self.run.profile_json = {
            'objection_style': {
                'acknowledge_before_responding': {
                    'value': None,  # extractor couldn't classify
                    'support_n': 0,
                    'confidence': 'INSUFFICIENT',
                    'evidence_conversation_ids': [],
                },
            },
        }
        self.run.save()

        diffs = build_diffs(run=self.run)
        d = next(x for x in diffs
                 if x.dimension == 'objection_style.acknowledge_before_responding')
        self.assertEqual(d.category,
                         CommunicationProfileDiff.Category.INSUFFICIENT_EVIDENCE)
        self.assertEqual(
            d.review_state, CommunicationProfileDiff.ReviewState.PENDING,
        )
        self.assertIn('Previously approved', d.narrative)
        self.assertIn('does not have enough evidence', d.narrative)

    # ─── Case 5 ──────────────────────────────────────────────────

    def test_no_prior_approval_creates_pending_diff(self):
        """No active TBP override → fresh diff is normal PENDING."""
        # No TBP at all — tenant has never approved anything.
        self.run.profile_json = {
            'response_style': {
                'typical_agent_sentences': {
                    'value': 2, 'support_n': 500, 'confidence': 'HIGH',
                    'evidence_conversation_ids': [],
                },
            },
        }
        self.run.save()
        diffs = build_diffs(run=self.run)
        d = next(x for x in diffs
                 if x.dimension == 'response_style.typical_agent_sentences')
        self.assertEqual(
            d.review_state, CommunicationProfileDiff.ReviewState.PENDING,
        )
        # No carry-forward narrative — this is a first-time proposal.
        self.assertNotIn('Previously approved', d.narrative)
        self.assertIsNone(d.owner_edited_payload)

    # ─── Case 6 — Boolean edge ───────────────────────────────────

    def test_boolean_value_equality_carries_forward(self):
        """False == False (boolean edge case — not a NULL-vs-value trap)."""
        self._seed_tbp_with_override(
            dimension='objection_style.acknowledge_before_responding',
            observed_value=False,
            support_n=40,
        )
        self.run.profile_json = {
            'objection_style': {
                'acknowledge_before_responding': {
                    'value': False, 'support_n': 60,
                    'confidence': 'HIGH', 'evidence_conversation_ids': [],
                },
            },
        }
        self.run.save()
        diffs = build_diffs(run=self.run)
        d = next(x for x in diffs
                 if x.dimension == 'objection_style.acknowledge_before_responding')
        self.assertEqual(
            d.review_state, CommunicationProfileDiff.ReviewState.ACCEPTED,
        )
        self.assertIn('Confirmed again from 60', d.narrative)
