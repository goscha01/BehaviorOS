"""Required Questions/Actions dimension — QM V1 fourth (final) dimension.

Semantic per operator directive (2026-08-23):

  1. Read the tenant's configured qualification requirements from
     ConfiguredBusinessFact rows (fact_type='configured_question',
     value_json.required=True) via the latest reconstruction.
  2. For each conversation, determine which required items were
     satisfied by looking at qualification ObservedBusinessFact rows
     that reference the conversation (evidence_conversation_ids).
     Question/answer/volunteer are all "touched the field."
  3. Per required field × conversation:
       Observed in this conv       → PASS
       Not observed in this conv   → FAIL (severity=warning)
     Per conversation:
       No qualification observations at all   → UNKNOWN
         (reason=no_qualification_data)
       Tenant has no required configured items → NOT_APPLICABLE
  4. Corpus-level: for each required field with reconstruction
     verdict CONFIGURED_NOT_OBSERVED (never asked anywhere),
     emit one corpus pattern FAIL.
  5. Evidence per per-conv PASS: configured_rule + at least one
     conversation_turn ref where the question was seen.
     Per per-conv FAIL: configured_rule + (no turn — that's the
     point). Per corpus FAIL: configured_rule + reconstruction fact.

Does NOT:
  * infer applicability from service_context or business hours —
    V1 treats every required item as applicable to every conversation
    that had qualification activity. Refinement is documented as a
    V2 note; produces FALSE POSITIVES only on conversations that
    genuinely have some qualification activity but should have been
    exempt from a specific field. In practice on Spotless where
    required fields are universal (bedrooms/bathrooms/frequency),
    this is fine.
  * judge whether the configured requirement itself is good.
  * add new LLM calls, config paths, or infrastructure.
"""

from __future__ import annotations

import logging
from typing import Iterable

from apps.quality_manager.dimensions import register
from apps.quality_manager.dimensions.base import (
    BaseDimension,
    DimensionResult,
    EvidenceRef,
    State,
)


logger = logging.getLogger(__name__)


VERSION = 'qm-v1-required-actions.1'


_TOUCHED_FACT_TYPES = frozenset({
    'question_asked',
    'answer_provided',
    'volunteered_before_question',
})


def _field_key(subject_key: dict | None) -> str | None:
    """Return the canonical field identifier for a qualification
    subject_key. Multiple observed facts for the same field
    (`question_asked` + `answer_provided`) collapse to one field
    for compliance purposes.
    """
    if not isinstance(subject_key, dict):
        return None
    field = subject_key.get('field')
    if not field:
        return None
    # For `other` fields, the specific `other_topic` differentiates.
    other = subject_key.get('other_topic')
    if field == 'other' and other:
        return f'other:{other}'
    return str(field)


def _describe_field(field_key: str) -> str:
    if field_key.startswith('other:'):
        return f"custom topic '{field_key.split(':', 1)[1]}'"
    return field_key


@register
class RequiredActionsDimension(BaseDimension):
    name = 'required_actions'
    version = VERSION

    def evaluate(
        self, *,
        reconstruction_run,
        conversation,
    ) -> Iterable[DimensionResult]:
        from apps.conversations.models import (
            ConfiguredBusinessFact, ObservedBusinessFact,
        )
        conv_id = str(conversation.id)

        # 1. What's required for this tenant? Read the CBFs the
        # reconstruction used (via its snapshot).
        required_by_field = _load_required_fields(
            snapshot=reconstruction_run.snapshot,
        )
        if not required_by_field:
            yield DimensionResult(
                dimension=self.name,
                state=State.NOT_APPLICABLE,
                conversation_id=conv_id,
                reason_code='no_required_config',
                rationale_text=(
                    'Tenant has no configured qualification requirements.'
                ),
            )
            return

        # 2. What did this conversation touch? Reads
        # ObservedBusinessFact rows for qualification whose
        # evidence_conversation_ids contains this conversation.
        touched_field_to_evidence: dict[str, list[dict]] = {}
        observed_facts = ObservedBusinessFact.objects.filter(
            org=conversation.org,
            domain='qualification',
            evidence_conversation_ids__contains=[conv_id],
        )
        conv_has_any_qual_observation = False
        for fact in observed_facts:
            if fact.fact_type not in _TOUCHED_FACT_TYPES:
                continue
            conv_has_any_qual_observation = True
            fkey = _field_key(fact.subject_key_json)
            if not fkey:
                continue
            evidence_bucket = touched_field_to_evidence.setdefault(fkey, [])
            for tref in (fact.evidence_turn_ids or []):
                if isinstance(tref, dict) and tref.get('conversation_id') == conv_id:
                    evidence_bucket.append({
                        'turn_id': tref.get('turn_id') or '',
                        'observed_fact_id': str(fact.id),
                        'fact_type': fact.fact_type,
                    })

        if not conv_has_any_qual_observation:
            yield DimensionResult(
                dimension=self.name,
                state=State.UNKNOWN_NOT_EVALUABLE,
                conversation_id=conv_id,
                reason_code='no_qualification_data',
                rationale_text=(
                    'Conversation has no qualification observations '
                    '(neither asked, answered, nor volunteered). '
                    'Cannot determine whether required items applied.'
                ),
            )
            return

        # 3. Per required field: PASS if observed, FAIL if not.
        for fkey, cbf in required_by_field.items():
            desc = _describe_field(fkey)
            base_evidence = [
                EvidenceRef(
                    kind='configured_rule',
                    ref=str(cbf['id']),
                    description=(
                        f"required qualification field: {desc} "
                        f"(collection_kind={cbf['collection_kind']})"
                    ),
                ),
            ]
            if fkey in touched_field_to_evidence:
                turn_evidence = touched_field_to_evidence[fkey][:3]
                for te in turn_evidence:
                    base_evidence.append(EvidenceRef(
                        kind='conversation_turn',
                        ref=te['turn_id'],
                        description=(
                            f'qualification field {desc} touched via '
                            f'{te["fact_type"]}'
                        ),
                    ))
                yield DimensionResult(
                    dimension=self.name,
                    state=State.PASS,
                    conversation_id=conv_id,
                    subject_key={'field': fkey},
                    reason_code='required_item_completed',
                    rationale_text=(
                        f'Required qualification field {desc} was '
                        f'addressed in this conversation.'
                    ),
                    evidence=base_evidence,
                )
            else:
                yield DimensionResult(
                    dimension=self.name,
                    state=State.FAIL,
                    conversation_id=conv_id,
                    subject_key={'field': fkey},
                    severity='warning',
                    reason_code='required_item_skipped',
                    rationale_text=(
                        f'Required qualification field {desc} was not '
                        f'asked, answered, or volunteered in this '
                        f'conversation.'
                    ),
                    evidence=base_evidence,
                )

    def evaluate_corpus(self, *, reconstruction_run) -> Iterable[DimensionResult]:
        """Corpus-level FAIL for each required field that appears in the
        reconstruction as CONFIGURED_NOT_OBSERVED — the "never asked
        anywhere across the whole corpus" pattern.
        """
        from apps.conversations.models import (
            ReconstructedBusinessFact,
        )
        facts = ReconstructedBusinessFact.objects.filter(
            reconstruction_run=reconstruction_run,
            domain='qualification',
            relationship_to_config='CONFIGURED_NOT_OBSERVED',
        )
        for fact in facts:
            subject = fact.canonical_subject_json or {}
            fkey = _field_key(subject) or (subject.get('field') or 'unknown')
            desc = _describe_field(fkey)
            fact_id = str(fact.id)
            yield DimensionResult(
                dimension=self.name,
                state=State.FAIL,
                conversation_id=None,      # corpus-level
                subject_key=subject,
                severity='warning',
                reason_code='required_item_never_observed',
                rationale_text=(
                    f'PATTERN: Required qualification field {desc} was '
                    f'not asked/answered/volunteered in ANY conversation '
                    f'in this reconstruction.'
                ),
                evidence=[
                    EvidenceRef(
                        kind='reconstructed_fact',
                        ref=fact_id,
                        description=(
                            f'CONFIGURED_NOT_OBSERVED for {desc}'
                        ),
                    ),
                ],
                source_reconstructed_fact_id=fact_id,
            )


def _load_required_fields(*, snapshot) -> dict[str, dict]:
    """Return {field_key: {id, collection_kind}} for every REQUIRED
    configured qualification question on this snapshot.
    """
    from apps.conversations.models import ConfiguredBusinessFact
    out: dict[str, dict] = {}
    cbfs = ConfiguredBusinessFact.objects.filter(
        snapshot=snapshot,
        domain='qualification',
        fact_type='configured_question',
    ).only('id', 'subject_key_json', 'value_json')
    for cbf in cbfs:
        value = cbf.value_json or {}
        if not value.get('required'):
            continue
        fkey = _field_key(cbf.subject_key_json)
        if not fkey:
            continue
        # First-write-wins to keep field key deterministic when the
        # config lists the same field twice with different contexts.
        out.setdefault(fkey, {
            'id': cbf.id,
            'collection_kind': value.get('collection_kind') or 'structured_field',
        })
    return out
