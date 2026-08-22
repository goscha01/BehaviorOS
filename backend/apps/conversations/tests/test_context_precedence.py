"""Unit tests for the source/time/authority-aware precedence engine.

Covers the 12 required properties from the canonical-context spec:
  * LB > conversation for stable attributes (structured wins)
  * later customer correction beats older LB
  * conversation-LLM never overrides LB for stable dims
  * conflicts preserved
  * missing stays missing
  * deterministic ordering
  * MANUAL beats everything
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from apps.conversations.context.precedence import resolve_precedence
from apps.conversations.context.types import (
    Attr,
    Authority,
    Observation,
)


def _obs(
    attribute: str,
    value,
    source: str,
    authority: Authority,
    *,
    observed_at: datetime,
    source_field: str = 'test:field',
    text: str | None = None,
) -> Observation:
    return Observation(
        attribute=attribute,
        value=value,
        source=source,
        source_field=source_field,
        observed_at=observed_at,
        authority=authority,
        text=text,
    )


BASE = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


class ResolvePrecedenceTests(SimpleTestCase):
    def test_single_observation_wins_by_default(self):
        obs = _obs(
            Attr.BEDROOMS, 3, 'leadbridge',
            Authority.SOURCE_STRUCTURED, observed_at=BASE,
        )
        winner, conflict, all_obs = resolve_precedence(Attr.BEDROOMS, [obs])
        self.assertIsNotNone(winner)
        self.assertEqual(winner.value, 3)
        self.assertIsNone(conflict)
        self.assertEqual(len(all_obs), 1)

    def test_empty_observations_returns_none(self):
        winner, conflict, all_obs = resolve_precedence(Attr.BEDROOMS, [])
        self.assertIsNone(winner)
        self.assertIsNone(conflict)
        self.assertEqual(all_obs, [])

    def test_lb_structured_beats_conversation_llm_for_stable_attribute(self):
        lb = _obs(
            Attr.BEDROOMS, 3, 'leadbridge',
            Authority.SOURCE_STRUCTURED, observed_at=BASE,
        )
        conv_llm = _obs(
            Attr.BEDROOMS, 5, 'conversation',
            Authority.CONVERSATION_LLM, observed_at=BASE + timedelta(days=90),
        )
        winner, conflict, _ = resolve_precedence(
            Attr.BEDROOMS, [lb, conv_llm],
        )
        # LB wins even though the LLM observation is 90 days newer —
        # STABLE attribute protection.
        self.assertEqual(winner.value, 3)
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.losing_values, [5])

    def test_conversation_explicit_correction_beats_older_lb_structured(self):
        # A customer explicitly correcting the survey answer 30 days
        # after the lead was captured should win — the current state
        # is what matters, not the initial request-form answer.
        lb = _obs(
            Attr.BEDROOMS, 3, 'leadbridge',
            Authority.SOURCE_STRUCTURED, observed_at=BASE,
        )
        correction = _obs(
            Attr.BEDROOMS, 4, 'conversation',
            Authority.CONVERSATION_EXPLICIT,
            observed_at=BASE + timedelta(days=30),
            text='Actually we have 4 bedrooms, I mis-typed the request',
        )
        winner, conflict, _ = resolve_precedence(
            Attr.BEDROOMS, [lb, correction],
        )
        self.assertEqual(winner.value, 4)
        self.assertIsNotNone(conflict)

    def test_recent_lb_beats_older_lb_when_same_authority(self):
        old = _obs(
            Attr.BEDROOMS, 3, 'leadbridge',
            Authority.SOURCE_STRUCTURED, observed_at=BASE,
        )
        new = _obs(
            Attr.BEDROOMS, 4, 'leadbridge',
            Authority.SOURCE_STRUCTURED,
            observed_at=BASE + timedelta(days=1),
        )
        winner, _, _ = resolve_precedence(Attr.BEDROOMS, [old, new])
        self.assertEqual(winner.value, 4)

    def test_manual_override_beats_everything(self):
        lb = _obs(
            Attr.BEDROOMS, 3, 'leadbridge',
            Authority.SOURCE_STRUCTURED, observed_at=BASE + timedelta(days=30),
        )
        manual = _obs(
            Attr.BEDROOMS, 5, 'manual',
            Authority.MANUAL, observed_at=BASE,
        )
        winner, _, _ = resolve_precedence(Attr.BEDROOMS, [lb, manual])
        self.assertEqual(winner.value, 5)

    def test_agreeing_observations_produce_no_conflict(self):
        lb = _obs(
            Attr.BEDROOMS, 3, 'leadbridge',
            Authority.SOURCE_STRUCTURED, observed_at=BASE,
        )
        conv = _obs(
            Attr.BEDROOMS, 3, 'conversation',
            Authority.CONVERSATION_EXPLICIT,
            observed_at=BASE + timedelta(days=1),
        )
        winner, conflict, _ = resolve_precedence(Attr.BEDROOMS, [lb, conv])
        self.assertEqual(winner.value, 3)
        self.assertIsNone(conflict)

    def test_conflict_severity_escalate_when_identical_authority_and_time(self):
        a = _obs(
            Attr.BEDROOMS, 3, 'leadbridge',
            Authority.SOURCE_STRUCTURED, observed_at=BASE,
            source_field='lb_lead:A#lead_details:Bedrooms',
        )
        b = _obs(
            Attr.BEDROOMS, 4, 'leadbridge',
            Authority.SOURCE_STRUCTURED, observed_at=BASE,
            source_field='lb_lead:B#lead_details:Bedrooms',
        )
        _, conflict, _ = resolve_precedence(Attr.BEDROOMS, [a, b])
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.severity, 'escalate')

    def test_conflict_severity_informational_when_old_and_weaker_loser(self):
        winner_ob = _obs(
            Attr.BEDROOMS, 4, 'leadbridge',
            Authority.SOURCE_STRUCTURED,
            observed_at=BASE + timedelta(days=90),
        )
        loser_ob = _obs(
            Attr.BEDROOMS, 3, 'conversation',
            Authority.CONVERSATION_LLM, observed_at=BASE,
        )
        _, conflict, _ = resolve_precedence(
            Attr.BEDROOMS, [winner_ob, loser_ob],
        )
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.severity, 'informational')

    def test_non_stable_attribute_lets_conversation_correct_source(self):
        # Frequency is not in STABLE_ATTRIBUTES — a fresh conversation
        # observation should be able to override an older source
        # answer since customers change cadence preferences.
        lb = _obs(
            Attr.FREQUENCY, 'monthly', 'leadbridge',
            Authority.SOURCE_STRUCTURED, observed_at=BASE,
        )
        conv = _obs(
            Attr.FREQUENCY, 'weekly', 'conversation',
            Authority.CONVERSATION_EXPLICIT,
            observed_at=BASE + timedelta(days=30),
        )
        winner, _, _ = resolve_precedence(Attr.FREQUENCY, [lb, conv])
        self.assertEqual(winner.value, 'weekly')

    def test_determinism_same_inputs_same_output(self):
        inputs = [
            _obs(Attr.BEDROOMS, 3, 'leadbridge',
                 Authority.SOURCE_STRUCTURED, observed_at=BASE),
            _obs(Attr.BEDROOMS, 4, 'conversation',
                 Authority.CONVERSATION_LLM,
                 observed_at=BASE + timedelta(days=5)),
            _obs(Attr.BEDROOMS, 3, 'leadbridge',
                 Authority.SOURCE_STRUCTURED,
                 observed_at=BASE + timedelta(days=1)),
        ]
        w1, c1, sorted1 = resolve_precedence(Attr.BEDROOMS, inputs)
        w2, c2, sorted2 = resolve_precedence(Attr.BEDROOMS, inputs)
        self.assertEqual(w1.value, w2.value)
        self.assertEqual(
            [o.value for o in sorted1], [o.value for o in sorted2],
        )
        self.assertEqual(
            [o.source_field for o in sorted1],
            [o.source_field for o in sorted2],
        )

    def test_string_attribute_case_insensitive_agreement(self):
        # Two enum observations that differ only in casing agree.
        a = _obs(
            Attr.FREQUENCY, 'Weekly', 'leadbridge',
            Authority.SOURCE_STRUCTURED, observed_at=BASE,
        )
        b = _obs(
            Attr.FREQUENCY, 'weekly', 'conversation',
            Authority.CONVERSATION_LLM,
            observed_at=BASE + timedelta(days=1),
        )
        _, conflict, _ = resolve_precedence(Attr.FREQUENCY, [a, b])
        self.assertIsNone(conflict)

    def test_list_attribute_order_insensitive_agreement(self):
        a = _obs(
            Attr.ADDONS, ['Oven', 'Fridge'], 'leadbridge',
            Authority.SOURCE_STRUCTURED, observed_at=BASE,
        )
        b = _obs(
            Attr.ADDONS, ['fridge', 'oven'], 'conversation',
            Authority.CONVERSATION_LLM,
            observed_at=BASE + timedelta(days=1),
        )
        _, conflict, _ = resolve_precedence(Attr.ADDONS, [a, b])
        self.assertIsNone(conflict)

    def test_all_losing_observations_preserved_in_sorted_output(self):
        obs_list = [
            _obs(Attr.BEDROOMS, 3, 'leadbridge',
                 Authority.SOURCE_STRUCTURED, observed_at=BASE),
            _obs(Attr.BEDROOMS, 4, 'conversation',
                 Authority.CONVERSATION_LLM,
                 observed_at=BASE + timedelta(days=1)),
            _obs(Attr.BEDROOMS, 5, 'conversation',
                 Authority.CONVERSATION_LLM,
                 observed_at=BASE + timedelta(days=2)),
        ]
        _, _, sorted_out = resolve_precedence(Attr.BEDROOMS, obs_list)
        # All 3 preserved.
        self.assertEqual(len(sorted_out), 3)
