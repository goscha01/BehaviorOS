"""Required Actions dimension tests.

Covers four-state matrix + corpus-level CONFIGURED_NOT_OBSERVED pattern.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from django.test import TestCase

from apps.accounts.models import Organization
from apps.conversations.models import (
    ConfiguredBusinessFact,
    ConfiguredFactParserRun,
    Conversation,
    IngestionStatus,
    LearningCorpus,
    ObservedBusinessFact,
    ObservedFactExtractionRun,
    ReconstructedBusinessFact,
    TenantConfigSnapshot,
    UnifiedBusinessReconstructionRun,
)
from apps.quality_manager.dimensions.base import State
from apps.quality_manager.dimensions.required_actions import (
    RequiredActionsDimension,
    _field_key,
)


BASE = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


def _org():
    return Organization.objects.create(name='ReqActions Test Org')


def _snapshot(org):
    return TenantConfigSnapshot.objects.create(
        org=org, source_system='leadbridge',
        tenant_external_id='t', contract_version='v1',
        raw_config={}, raw_config_sha256='h' * 64,
    )


def _parser_run(snap):
    return ConfiguredFactParserRun.objects.create(
        snapshot=snap, domain='qualification',
        parser_version='test', status='completed',
    )


def _extraction_run(org):
    corpus = LearningCorpus.objects.create(org=org, name='c', version='v')
    return ObservedFactExtractionRun.objects.create(
        org=org, corpus=corpus,
        domain='qualification', extractor_version='test',
        model='test', status='completed',
    )


def _recon(org, snap):
    return UnifiedBusinessReconstructionRun.objects.create(
        org=org, tenant_external_id='t',
        snapshot=snap, reconstruction_version='test',
        status='completed',
    )


def _configured_question(parser, field, required):
    return ConfiguredBusinessFact.objects.create(
        snapshot=parser.snapshot, parser_run=parser,
        domain='qualification', fact_type='configured_question',
        subject_key_json={'field': field},
        subject_key_dimensions=['field'],
        subject_key_hash=str(uuid.uuid4()),
        value_json={'required': required, 'collection_kind': 'structured_field'},
    )


def _observed_qual(org, ext_run, field, conv_ids, fact_type='question_asked'):
    return ObservedBusinessFact.objects.create(
        org=org, corpus=ext_run.corpus, extraction_run=ext_run,
        domain='qualification', fact_type=fact_type,
        subject_key_json={'field': field},
        subject_key_dimensions=['field'],
        subject_key_hash=str(uuid.uuid4()),
        value_json={'sample_phrasings': ['(test)']},
        support_n=len(conv_ids),
        evidence_conversation_ids=[str(c.id) for c in conv_ids],
        evidence_turn_ids=[
            {'conversation_id': str(c.id), 'turn_id': f't{i:02d}'}
            for i, c in enumerate(conv_ids)
        ],
    )


def _conv(org, seq=0):
    return Conversation.objects.create(
        org=org, source='quo',
        source_conversation_id=f'quo:ra-{seq}',
        customer_phone='+18135550000',
        started_at=BASE,
        ingestion_status=IngestionStatus.LINKED,
    )


class FieldKeyTests(TestCase):
    def test_normal_field(self):
        self.assertEqual(_field_key({'field': 'bedrooms'}), 'bedrooms')

    def test_other_topic_disambiguated(self):
        self.assertEqual(
            _field_key({'field': 'other', 'other_topic': 'hoa_rules'}),
            'other:hoa_rules',
        )

    def test_missing_field(self):
        self.assertIsNone(_field_key({}))
        self.assertIsNone(_field_key(None))


class RequiredActionsEvaluateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = _org()
        cls.snap = _snapshot(cls.org)
        cls.parser = _parser_run(cls.snap)
        cls.ext_run = _extraction_run(cls.org)
        cls.recon = _recon(cls.org, cls.snap)

        # Required config: bedrooms, bathrooms. Non-required: frequency.
        cls.cbf_bed = _configured_question(cls.parser, 'bedrooms', True)
        cls.cbf_bath = _configured_question(cls.parser, 'bathrooms', True)
        cls.cbf_freq = _configured_question(cls.parser, 'frequency', False)

    def test_not_applicable_when_no_required_config(self):
        """Different org, no required qualification config → NOT_APPLICABLE."""
        other = _org()
        other_snap = _snapshot(other)
        other_recon = _recon(other, other_snap)
        conv = _conv(other, 99)
        # Give conv some qualification observations so it can't be UNKNOWN
        other_ext = _extraction_run(other)
        _observed_qual(other, other_ext, 'anything', [conv])
        dim = RequiredActionsDimension()
        results = list(dim.evaluate(
            reconstruction_run=other_recon, conversation=conv,
        ))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].state, State.NOT_APPLICABLE)
        self.assertEqual(results[0].reason_code, 'no_required_config')

    def test_unknown_when_no_qualification_observations(self):
        """Conversation exists but no qualification extractor output at all."""
        conv = _conv(self.org, 1)
        # No ObservedBusinessFact created for this conv.
        dim = RequiredActionsDimension()
        results = list(dim.evaluate(
            reconstruction_run=self.recon, conversation=conv,
        ))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].state, State.UNKNOWN_NOT_EVALUABLE)
        self.assertEqual(results[0].reason_code, 'no_qualification_data')

    def test_pass_when_all_required_satisfied(self):
        conv = _conv(self.org, 2)
        _observed_qual(self.org, self.ext_run, 'bedrooms', [conv])
        _observed_qual(self.org, self.ext_run, 'bathrooms', [conv])
        dim = RequiredActionsDimension()
        results = list(dim.evaluate(
            reconstruction_run=self.recon, conversation=conv,
        ))
        # 2 required, both PASS
        pass_results = [r for r in results if r.state == State.PASS]
        self.assertEqual(len(pass_results), 2)
        pass_fields = sorted(r.subject_key.get('field') for r in pass_results)
        self.assertEqual(pass_fields, ['bathrooms', 'bedrooms'])
        # Evidence includes configured_rule + at least one conversation_turn
        for r in pass_results:
            kinds = [e.kind for e in r.evidence]
            self.assertIn('configured_rule', kinds)
            self.assertIn('conversation_turn', kinds)

    def test_fail_when_required_field_missed(self):
        conv = _conv(self.org, 3)
        _observed_qual(self.org, self.ext_run, 'bedrooms', [conv])
        # bathrooms NOT observed for this conv
        dim = RequiredActionsDimension()
        results = list(dim.evaluate(
            reconstruction_run=self.recon, conversation=conv,
        ))
        states_by_field = {
            r.subject_key.get('field'): r.state for r in results
        }
        self.assertEqual(states_by_field.get('bedrooms'), State.PASS)
        self.assertEqual(states_by_field.get('bathrooms'), State.FAIL)
        bath_fail = next(
            r for r in results
            if r.subject_key.get('field') == 'bathrooms'
            and r.state == State.FAIL
        )
        self.assertEqual(bath_fail.severity, 'warning')
        self.assertEqual(bath_fail.reason_code, 'required_item_skipped')

    def test_non_required_config_ignored(self):
        """Non-required fields don't generate PASS or FAIL."""
        conv = _conv(self.org, 4)
        # Only satisfies frequency (which is NOT required).
        _observed_qual(self.org, self.ext_run, 'frequency', [conv])
        dim = RequiredActionsDimension()
        results = list(dim.evaluate(
            reconstruction_run=self.recon, conversation=conv,
        ))
        # bedrooms + bathrooms both FAIL, frequency ignored entirely
        fields_seen = {r.subject_key.get('field') for r in results}
        self.assertNotIn('frequency', fields_seen)

    def test_multiple_fact_types_touch_field(self):
        """answer_provided OR volunteered_before_question also count as
        touched — not just question_asked."""
        conv = _conv(self.org, 5)
        _observed_qual(
            self.org, self.ext_run, 'bedrooms', [conv],
            fact_type='answer_provided',
        )
        _observed_qual(
            self.org, self.ext_run, 'bathrooms', [conv],
            fact_type='volunteered_before_question',
        )
        dim = RequiredActionsDimension()
        results = list(dim.evaluate(
            reconstruction_run=self.recon, conversation=conv,
        ))
        pass_fields = sorted(
            r.subject_key.get('field') for r in results if r.state == State.PASS
        )
        self.assertEqual(pass_fields, ['bathrooms', 'bedrooms'])

    def test_corpus_level_pattern_for_never_observed(self):
        """CONFIGURED_NOT_OBSERVED verdict at reconstruction → corpus
        FAIL for that field."""
        ReconstructedBusinessFact.objects.create(
            reconstruction_run=self.recon,
            domain='qualification',
            canonical_subject_json={'field': 'square_footage'},
            canonical_subject_hash=str(uuid.uuid4()),
            observed_value_json={},
            configured_equivalent_json={'id': str(uuid.uuid4())},
            support_n=0,
            relationship_to_config='CONFIGURED_NOT_OBSERVED',
            onboarding_class='NEEDS_OWNER_CONFIRMATION',
            evidence_conversation_ids=[],
            evidence_turn_ids=[],
        )
        dim = RequiredActionsDimension()
        corpus_results = list(dim.evaluate_corpus(
            reconstruction_run=self.recon,
        ))
        self.assertEqual(len(corpus_results), 1)
        r = corpus_results[0]
        self.assertEqual(r.state, State.FAIL)
        self.assertIsNone(r.conversation_id)
        self.assertEqual(r.reason_code, 'required_item_never_observed')
        self.assertEqual(r.subject_key.get('field'), 'square_footage')
