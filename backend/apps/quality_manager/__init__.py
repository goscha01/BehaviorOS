"""Quality Manager — V1 compliance analyzer for BehaviorOS.

V1 scope (per operator directive 2026-08-22):
  * Consumer #2 of the Canonical Context Resolution Layer.
  * Compliance / conformance analyzer — NOT a strategic optimizer.
    Compliance finding: "Agent quoted $185, effective configured
    price was $159."
    NOT: "The $159 rule itself is suboptimal." (that belongs to
    a different analytical surface — BehaviorOS recommendations.)
  * Four evaluation states per (conversation, dimension, subject):
      PASS | FAIL | UNKNOWN_NOT_EVALUABLE | NOT_APPLICABLE
    UNKNOWN and NOT_APPLICABLE are first-class results, safer than
    manufacturing a warning from incomplete context.
  * NO overall grade in V1 — report per-dimension counts + severity.
  * V1 ships ONLY the Pricing Correctness dimension.
    Roadmap order: Pricing → Timing → Question Answered →
    Required Actions → Contradiction → Follow-up → Missed Opportunity.

QM reuses the existing Pricing 1D deterministic matcher — it never
duplicates the ±10% tolerance logic. Reads
`ReconstructedBusinessFact.relationship_to_config` and per-cell
comparison output, does not re-compute.
"""

default_app_config = 'apps.quality_manager.apps.QualityManagerConfig'
