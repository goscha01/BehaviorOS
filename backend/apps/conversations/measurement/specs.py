"""Versioned MeasurementSpec registry — deterministic outcome templates.

The problem this solves
-----------------------
When a recommendation is Applied, we need to know exactly WHAT will be
measured, HOW cohorts are defined, and WHAT counts as a positive
outcome — before the experiment runs. Otherwise:

- The LLM (or a well-meaning engineer) could choose the outcome after
  looking at post-application data (p-hacking).
- Attribution windows measured in "turns" would shift as a consequence
  of the intervention itself (an intervention that creates a longer
  useful conversation would accidentally hurt its own measurement).
- Different recommendations targeting the same customer state would be
  scored inconsistently.

A MeasurementSpec is a **versioned, code-defined template** frozen onto
the RecommendationApplication BEFORE Apply. Neither the LLM nor the
evaluator can retroactively change what is being measured.

Design invariants
-----------------
1. Attribution windows are FIXED ELAPSED TIME (days), never turn counts.
2. `spec_key` is stable and versioned via the module-level VERSION
   constant. If cohort/outcome semantics change, the spec gets a new
   spec_key (v2) and a new registry entry — old measurements keep
   pointing at their original spec_key.
3. `positive_terminal_events` and `negative_terminal_events` reference
   canonical outcome tokens that are source-agnostic (LB, SF, Callio all
   emit into the same normalized OutcomeSnapshot vocabulary).
4. Cohort entry references the customer signal ONTOLOGICALLY (not per
   source). The specific signal (e.g. DISCOUNT_REQUESTED) is
   instantiated at freeze time from the recommendation's
   `subject_signals`, so one spec covers a family of interventions.

v1 scope
--------
One spec: `high_intent_signal_coverage.v1` — covers any
STATE_COVERAGE_GAP / STATE_PARTIAL_COVERAGE recommendation whose target
signal is a HIGH_INTENT customer signal (DISCOUNT_REQUESTED,
BOOKING_REQUESTED, AVAILABILITY_REQUESTED, PRICE_REQUESTED). This is
exactly the set of recs that can already become RecommendationProposals
under proposal_synthesis v1, so every applyable rec has a spec.

Anything else raises NoMeasurementSpec at freeze time — the operator
sees "no measurement available for this rec class" rather than a
silently-broken measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from apps.conversations.models import BehaviorRecommendation


# Bump this when the semantics of ANY existing spec_key change.
# (Adding a new spec_key does NOT require a bump.)
MEASUREMENT_SPEC_MODULE_VERSION = 'measurement-spec-v1'


# Canonical outcome tokens — source-agnostic terminals we can score
# against without knowing whether the source was LB, SF, or Callio.
# Every source's OutcomeSnapshot maps its fields onto these tokens
# through the terminal_extractor (see terminals.py in a later step).
class OutcomeTerminal:
    LB_BOOKED = 'LB_BOOKED'
    LB_LOST = 'LB_LOST'
    LB_CANCELLED = 'LB_CANCELLED'
    SF_BOOKED = 'SF_BOOKED'
    SF_COMPLETED = 'SF_COMPLETED'
    SF_CANCELLED = 'SF_CANCELLED'


# HIGH_INTENT customer signals — matches the 1B-6 CustomerState v1 set.
HIGH_INTENT_SIGNALS = frozenset({
    'DISCOUNT_REQUESTED',
    'BOOKING_REQUESTED',
    'AVAILABILITY_REQUESTED',
    'PRICE_REQUESTED',
})


class NoMeasurementSpec(Exception):
    """Raised at freeze time when no MeasurementSpec applies to a
    recommendation. Deterministic — the operator sees "measurement not
    available for this recommendation class"."""


@dataclass(frozen=True)
class CohortEntryPredicate:
    """Predicate describing when a conversation enters the cohort.

    v1: `signal_observed_in_conversation` — conversation contains a
    ConversationSemanticEvent whose event_type equals `signal`, and
    that event was extracted by an approved extractor version.

    `signal` is either literal ('DISCOUNT_REQUESTED') or the sentinel
    '<RECOMMENDATION_TARGET_SIGNAL>' meaning "fill from the
    recommendation's first subject_signal at freeze time."
    """
    kind: str  # 'signal_observed_in_conversation'
    signal: str = '<RECOMMENDATION_TARGET_SIGNAL>'


@dataclass(frozen=True)
class PrimaryOutcomeDefinition:
    """Deterministic outcome scoring rule.

    v1: `reaches_positive_terminal_within_days` — customer's linked
    OutcomeSnapshot (in ANY snapshot captured within
    `attribution_window_days` of conversation start) contains at least
    one of `positive_terminal_events`. Absence of a positive terminal
    within the window is scored as negative if a negative terminal was
    reached; otherwise the customer is UNRESOLVED and does not count
    toward either arm's outcome rate.

    `baseline_window_days` defines the elapsed-time lookback used to
    build the pre-application cohort. Larger windows accumulate more
    baseline data but risk mixing eras where the tenant's config was
    materially different. v1 defaults to 90 days — a conservative
    compromise given that most LB tenants' historical conversations
    predate provenance stamping (config_provenance_status=PENDING)
    and can't be filtered by hash.
    """
    kind: str  # 'reaches_positive_terminal_within_days'
    attribution_window_days: int
    baseline_window_days: int
    positive_terminal_events: tuple[str, ...]
    negative_terminal_events: tuple[str, ...]


@dataclass(frozen=True)
class ExclusionRule:
    """Deterministic exclusion — customers matching are dropped from
    BOTH arms (not scored as failures).

    v1 exclusion tokens:
    - LEAD_MISMATCH: the OutcomeSnapshot metadata flagged the lead as
      wrong-service, spam, or otherwise not a legitimate cohort member.
    """
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class VerdictGates:
    """Gates that must ALL pass to declare IMPROVED/WORSE.

    Any failure keeps status at COLLECTING (before READY) or leads to
    INCONCLUSIVE (at deadline).

    - min_sample_per_arm: neither arm may have fewer than this many
      cohort members with a resolved outcome (positive or negative).
    - min_effect_size_pp: absolute percentage-point difference between
      post-rate and pre-rate must exceed this. Prevents flattening a
      trivial 1-pp move into "IMPROVED."
    - uncertainty_significance_alpha: Fisher's exact two-sided p-value
      must be strictly less than this. NOT the only gate — see class
      docstring.
    - min_provenance_coverage: minimum fraction of target-signal
      conversations that must have clean provenance (eligible / total).
      Below this, READY is refused — silent PENDING/HASH_FAILED gaps
      would invalidate any verdict. v1: 0.60.
    - max_window_days_for_inconclusive: after this many days from
      application, if verdict gates have not passed, transition to
      INCONCLUSIVE rather than continuing to accumulate forever.
    """
    min_sample_per_arm: int
    min_effect_size_pp: float
    uncertainty_significance_alpha: float
    min_provenance_coverage: float
    max_window_days_for_inconclusive: int


@dataclass(frozen=True)
class MeasurementSpec:
    """Frozen at Apply time. Never modified afterwards.

    `spec_key` is the stable identifier persisted on the
    RecommendationApplication (and later on RecommendationOutcomeMeasurement).
    Changing spec semantics requires a NEW spec_key, not editing an
    existing one — otherwise historical measurements would silently
    change meaning.
    """
    spec_key: str
    version: str  # matches MEASUREMENT_SPEC_MODULE_VERSION when defined
    family: str
    description: str
    applies_to_rec_classes: frozenset[str]
    applies_to_signals: frozenset[str]
    cohort_entry: CohortEntryPredicate
    primary_outcome: PrimaryOutcomeDefinition
    exclusions: ExclusionRule
    verdict_gates: VerdictGates

    def freeze_for_recommendation(
        self, rec: BehaviorRecommendation
    ) -> 'FrozenMeasurementSpec':
        """Instantiate the spec's target-signal sentinel from the
        recommendation. Called at Apply time by the application service.

        The returned FrozenMeasurementSpec is what gets stored on the
        RecommendationOutcomeMeasurement row (as JSON) so future
        evaluators know exactly what to score even if the code-side
        registry evolves.
        """
        if not rec.subject_signals:
            raise NoMeasurementSpec(
                f'Recommendation {rec.recommendation_id} has no '
                f'subject_signals; cannot freeze target signal.'
            )
        target = rec.subject_signals[0]
        # Sanity: rec's target signal must be in this spec's applicable
        # set. resolve_spec_for_recommendation should have gated this
        # already; we re-check here for defense-in-depth.
        if target not in self.applies_to_signals:
            raise NoMeasurementSpec(
                f'Recommendation target signal {target!r} not in '
                f'spec {self.spec_key} applies_to_signals '
                f'{sorted(self.applies_to_signals)}'
            )
        cohort_entry = replace(self.cohort_entry, signal=target)
        return FrozenMeasurementSpec(
            spec_key=self.spec_key,
            version=self.version,
            family=self.family,
            description=self.description,
            cohort_entry=cohort_entry,
            primary_outcome=self.primary_outcome,
            exclusions=self.exclusions,
            verdict_gates=self.verdict_gates,
        )


@dataclass(frozen=True)
class FrozenMeasurementSpec:
    """A MeasurementSpec with all target-signal sentinels resolved.

    This is the JSON-serializable snapshot persisted on the
    measurement row. `applies_to_*` sets are dropped because the
    recommendation is already fixed at this point — those fields only
    matter during spec resolution.
    """
    spec_key: str
    version: str
    family: str
    description: str
    cohort_entry: CohortEntryPredicate
    primary_outcome: PrimaryOutcomeDefinition
    exclusions: ExclusionRule
    verdict_gates: VerdictGates

    def to_dict(self) -> dict:
        """Serialize for persistence on the measurement row. Keys are
        stable — treat this as an on-disk contract, not an internal
        representation."""
        return {
            'spec_key': self.spec_key,
            'version': self.version,
            'family': self.family,
            'description': self.description,
            'cohort_entry': {
                'kind': self.cohort_entry.kind,
                'signal': self.cohort_entry.signal,
            },
            'primary_outcome': {
                'kind': self.primary_outcome.kind,
                'attribution_window_days': (
                    self.primary_outcome.attribution_window_days
                ),
                'baseline_window_days': (
                    self.primary_outcome.baseline_window_days
                ),
                'positive_terminal_events': list(
                    self.primary_outcome.positive_terminal_events
                ),
                'negative_terminal_events': list(
                    self.primary_outcome.negative_terminal_events
                ),
            },
            'exclusions': {'tokens': list(self.exclusions.tokens)},
            'verdict_gates': {
                'min_sample_per_arm': (
                    self.verdict_gates.min_sample_per_arm
                ),
                'min_effect_size_pp': (
                    self.verdict_gates.min_effect_size_pp
                ),
                'uncertainty_significance_alpha': (
                    self.verdict_gates.uncertainty_significance_alpha
                ),
                'min_provenance_coverage': (
                    self.verdict_gates.min_provenance_coverage
                ),
                'max_window_days_for_inconclusive': (
                    self.verdict_gates.max_window_days_for_inconclusive
                ),
            },
        }


# ---------------------------------------------------------------------------
# Registry — v1 has one entry. Additions do NOT bump
# MEASUREMENT_SPEC_MODULE_VERSION; semantic changes to an existing
# spec_key DO (and require a new spec_key).
# ---------------------------------------------------------------------------

HIGH_INTENT_SIGNAL_COVERAGE_V1 = MeasurementSpec(
    spec_key='high_intent_signal_coverage.v1',
    version=MEASUREMENT_SPEC_MODULE_VERSION,
    family='state_coverage',
    description=(
        'Measures whether adding a behavior rule for an uncovered '
        'HIGH_INTENT customer signal changes the rate at which those '
        'customers subsequently reach a positive booking terminal. '
        'Cohort: customers whose conversation contains the target '
        'signal. Outcome: at least one positive terminal '
        '(LB_BOOKED / SF_BOOKED / SF_COMPLETED) observed in an '
        'OutcomeSnapshot captured within 14 days of conversation start.'
    ),
    applies_to_rec_classes=frozenset({
        BehaviorRecommendation.RecClass.STATE_COVERAGE_GAP,
        BehaviorRecommendation.RecClass.STATE_PARTIAL_COVERAGE,
    }),
    applies_to_signals=HIGH_INTENT_SIGNALS,
    cohort_entry=CohortEntryPredicate(
        kind='signal_observed_in_conversation',
        signal='<RECOMMENDATION_TARGET_SIGNAL>',
    ),
    primary_outcome=PrimaryOutcomeDefinition(
        kind='reaches_positive_terminal_within_days',
        attribution_window_days=14,
        baseline_window_days=90,
        positive_terminal_events=(
            OutcomeTerminal.LB_BOOKED,
            OutcomeTerminal.SF_BOOKED,
            OutcomeTerminal.SF_COMPLETED,
        ),
        negative_terminal_events=(
            OutcomeTerminal.LB_LOST,
            OutcomeTerminal.LB_CANCELLED,
            OutcomeTerminal.SF_CANCELLED,
        ),
    ),
    exclusions=ExclusionRule(tokens=('LEAD_MISMATCH',)),
    verdict_gates=VerdictGates(
        # Conservative for v1. Real experiments in tenant-scoped
        # cleaning corpora rarely see thousands of matches; 30 per arm
        # is small but statistically defensible for Fisher's exact
        # combined with a min-effect-size gate.
        min_sample_per_arm=30,
        min_effect_size_pp=10.0,
        uncertainty_significance_alpha=0.05,
        # Refuse to promote to READY if the coverage ratio drops
        # below this — most target-signal conversations should have
        # OK provenance before we score anything.
        min_provenance_coverage=0.60,
        # Stop accumulating after 90 days without a verdict — the
        # world has probably moved on and the measurement is stale.
        max_window_days_for_inconclusive=90,
    ),
)


MEASUREMENT_SPEC_REGISTRY: dict[str, MeasurementSpec] = {
    HIGH_INTENT_SIGNAL_COVERAGE_V1.spec_key: HIGH_INTENT_SIGNAL_COVERAGE_V1,
}


def resolve_spec_for_recommendation(
    rec: BehaviorRecommendation,
) -> MeasurementSpec:
    """Deterministically pick the MeasurementSpec for `rec`.

    Returns the spec (unfrozen — caller invokes freeze_for_recommendation
    to instantiate target signals). Raises NoMeasurementSpec when the rec
    is outside every registered spec's applicability envelope.

    v1: only HIGH_INTENT_SIGNAL_COVERAGE_V1 matches. This is
    intentionally the same envelope as proposal_synthesis's eligibility
    gate so every apply-able rec has a spec.
    """
    if not rec.subject_signals:
        raise NoMeasurementSpec(
            f'Recommendation {rec.recommendation_id} has no '
            f'subject_signals; no spec can bind a target signal.'
        )
    target_signal = rec.subject_signals[0]
    for spec in MEASUREMENT_SPEC_REGISTRY.values():
        if rec.rec_class not in spec.applies_to_rec_classes:
            continue
        if target_signal not in spec.applies_to_signals:
            continue
        return spec
    raise NoMeasurementSpec(
        f'No MeasurementSpec applies to rec_class={rec.rec_class} '
        f'target_signal={target_signal!r}. Registered specs: '
        f'{sorted(MEASUREMENT_SPEC_REGISTRY.keys())}'
    )


def get_spec(spec_key: str) -> Optional[MeasurementSpec]:
    """Look up a spec by its persisted key. Returns None if the spec_key
    was retired — callers should treat that as a hard error at evaluation
    time and mark the measurement INCONCLUSIVE with a retired-spec note
    rather than silently switching to a different spec."""
    return MEASUREMENT_SPEC_REGISTRY.get(spec_key)
