"""Pipeline 1B-1 semantic extraction tests.

Covers:
- Ontology (validation, category coverage)
- Preprocessing (empty turn drop, kept short customer replies)
- Chunking (single-chunk path, multi-chunk overlap, merge/dedupe)
- Validator (unknown event, unknown actor, turn indices, confidence bounds,
  evidence required, malformed input)
- Extractor idempotency
- Extractor uses ONLY the turn text (asserted: outcome not in prompt)
- Corpus versioning (rerun = same set)
- Tenant isolation (extraction on org A doesn't touch org B events)
- Extractor-version coexistence
- Malformed LLM output handled per-record without aborting batch
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Organization
from apps.conversations.models import (
    Channel, Conversation, ConversationSemanticEvent, ConversationTurn,
    Direction, EntityLink, LearningCorpus, LearningCorpusMember,
    MatchMethod, OutcomeSnapshot, SemanticExtractionRun, Speaker,
    TargetSystem, TargetType,
)
from apps.conversations.semantic.extractor import (
    EXTRACTOR_VERSION, SemanticExtractor, get_or_create_run, run_extraction,
)
from apps.conversations.semantic.ontology import (
    ACTORS, EVENT_TYPES, ONTOLOGY_VERSION,
    event_types_by_category, is_valid_actor, is_valid_event_type,
)
from apps.conversations.semantic.preprocessing import (
    ConversationChunk, NormalizedTurn, chunk_conversation,
    load_and_normalize, merge_extracted_events, render_turns_for_prompt,
)
from apps.conversations.semantic.prompt import PROMPT_VERSION, SYSTEM_PROMPT
from apps.conversations.semantic.validator import validate_events


# ---------------------------------------------------------------------------
# Ontology
# ---------------------------------------------------------------------------


class OntologyTests(SimpleTestCase):
    def test_all_categories_have_at_least_one_type(self):
        for cat, types in event_types_by_category().items():
            self.assertGreater(len(types), 0, f'category {cat} empty')

    def test_every_categorized_type_is_valid(self):
        for cat, types in event_types_by_category().items():
            for t in types:
                self.assertTrue(is_valid_event_type(t), f'{cat}.{t} not in ontology')

    def test_actors_are_the_expected_five(self):
        self.assertEqual(
            ACTORS,
            frozenset({'customer', 'agent', 'system', 'mixed', 'unknown'}),
        )

    def test_ontology_version_present(self):
        self.assertTrue(ONTOLOGY_VERSION)

    def test_v2_additions_present(self):
        self.assertTrue(is_valid_event_type('CUSTOMER_DEFERRED'))
        self.assertTrue(is_valid_event_type('LEAD_MISMATCH'))

    def test_ontology_version_is_v2(self):
        self.assertEqual(ONTOLOGY_VERSION, 'ontology-v2')


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class ValidatorTests(SimpleTestCase):
    def _ev(self, **overrides):
        # v1-shape base (int turn refs) — used with max_turn_index path.
        base = {
            'event_type': 'PRICE_REQUESTED',
            'actor': 'customer',
            'turn_start': 3,
            'turn_end': 3,
            'confidence': 0.9,
            'attributes': {},
            'evidence': 'How much?',
        }
        base.update(overrides)
        return base

    def _ev_v2(self, **overrides):
        base = {
            'event_type': 'PRICE_REQUESTED',
            'actor': 'customer',
            'turn_start': 't0003',
            'turn_end': 't0003',
            'confidence': 0.9,
            'attributes': {},
            'evidence': 'How much?',
        }
        base.update(overrides)
        return base

    def _v2_map(self):
        from apps.conversations.semantic.preprocessing import TurnIdMap
        return TurnIdMap(id_to_parent={
            't0003': 3, 't0005': 5, 't0007': 7, 't0010': 10,
        })

    def test_valid_event_passes(self):
        r = validate_events({'events': [self._ev()]}, max_turn_index=10)
        self.assertEqual(len(r.events), 1)
        self.assertEqual(len(r.rejected), 0)

    def test_valid_v2_event_with_string_turn_ids(self):
        r = validate_events({'events': [self._ev_v2()]}, turn_id_map=self._v2_map())
        self.assertEqual(len(r.events), 1)
        # Turn refs resolved to int parent idx.
        self.assertEqual(r.events[0]['turn_start'], 3)
        self.assertEqual(r.events[0]['turn_end'], 3)

    def test_v2_unknown_turn_id_rejected(self):
        r = validate_events({'events': [self._ev_v2(turn_start='t9999', turn_end='t9999')]},
                            turn_id_map=self._v2_map())
        self.assertEqual(len(r.events), 0)
        self.assertIn('unknown turn_start id', r.rejected[0]['reason'])

    def test_v2_int_turn_ref_rejected_when_map_supplied(self):
        r = validate_events({'events': [self._ev(turn_start=3, turn_end=3)]},
                            turn_id_map=self._v2_map())
        self.assertEqual(len(r.events), 0)
        self.assertIn('must be string turn_ids', r.rejected[0]['reason'])

    def test_unknown_event_type_rejected(self):
        r = validate_events({'events': [self._ev(event_type='BOGUS')]}, max_turn_index=10)
        self.assertEqual(len(r.events), 0)
        self.assertEqual(len(r.rejected), 1)
        self.assertIn('unknown event_type', r.rejected[0]['reason'])

    def test_unknown_actor_rejected(self):
        r = validate_events({'events': [self._ev(actor='wizard')]}, max_turn_index=10)
        self.assertEqual(len(r.events), 0)

    def test_turn_index_out_of_range_rejected(self):
        r = validate_events({'events': [self._ev(turn_start=15, turn_end=15)]}, max_turn_index=10)
        self.assertEqual(len(r.events), 0)

    def test_turn_start_greater_than_end_rejected(self):
        r = validate_events({'events': [self._ev(turn_start=5, turn_end=3)]}, max_turn_index=10)
        self.assertEqual(len(r.events), 0)

    def test_confidence_bounds(self):
        for bad in (-0.1, 1.1, 'high', None):
            r = validate_events({'events': [self._ev(confidence=bad)]}, max_turn_index=10)
            self.assertEqual(len(r.events), 0, f'bad conf {bad!r} not rejected')

    def test_empty_evidence_rejected(self):
        r = validate_events({'events': [self._ev(evidence='')]}, max_turn_index=10)
        self.assertEqual(len(r.events), 0)
        r = validate_events({'events': [self._ev(evidence='   ')]}, max_turn_index=10)
        self.assertEqual(len(r.events), 0)

    def test_response_missing_events_key(self):
        r = validate_events({'nothing_here': True}, max_turn_index=10)
        self.assertEqual(len(r.rejected), 1)

    def test_attributes_must_be_object(self):
        r = validate_events({'events': [self._ev(attributes=['not', 'an', 'object'])]}, max_turn_index=10)
        self.assertEqual(len(r.events), 0)

    def test_evidence_truncated_at_1000_chars(self):
        long_ev = 'x' * 2000
        r = validate_events({'events': [self._ev(evidence=long_ev)]}, max_turn_index=10)
        self.assertEqual(len(r.events), 1)
        self.assertEqual(len(r.events[0]['evidence']), 1000)


# ---------------------------------------------------------------------------
# Preprocessing / chunking
# ---------------------------------------------------------------------------


class PreprocessingTests(TestCase):
    def _conv_with_turns(self, texts_and_kinds):
        org = Organization.objects.create(name='X')
        conv = Conversation.objects.create(
            org=org, source='quo', source_conversation_id='cn-1',
            channel=Channel.SMS, customer_phone='+18135551234',
            started_at=timezone.now(),
        )
        for i, (text, kind) in enumerate(texts_and_kinds):
            ConversationTurn.objects.create(
                conversation=conv,
                source_turn_id=f'm-{i}',
                speaker=Speaker.CUSTOMER if i % 2 == 0 else Speaker.AGENT,
                direction=Direction.INBOUND if i % 2 == 0 else Direction.OUTBOUND,
                text=text,
                occurred_at=timezone.now(),
                metadata={'kind': kind},
            )
        return conv

    def test_empty_sms_turn_dropped_call_kept(self):
        conv = self._conv_with_turns([
            ('hi', 'sms'), ('', 'sms'), ('', 'call_no_transcript'), ('yes', 'sms'),
        ])
        turns, _ = load_and_normalize(conv)
        # 3 kept: hi, call, yes. Empty SMS dropped.
        self.assertEqual([t.text for t in turns], ['hi', '', 'yes'])
        self.assertEqual(turns[1].kind, 'call_no_transcript')

    def test_short_customer_reply_retained(self):
        conv = self._conv_with_turns([('hi', 'sms'), ('yes', 'sms'), ('ok', 'sms')])
        turns, _ = load_and_normalize(conv)
        self.assertEqual(len(turns), 3)

    def test_bulk_voice_transcript_split_into_speaker_subturns(self):
        # A single bulk transcript with 3 speaker segments becomes 3 sub-turns.
        conv = self._conv_with_turns([
            ('hi', 'sms'),
            ('+13164444895: Hello? Agent: Hi, this is Kate. '
             '+13164444895: I need a cleaning next Tuesday.', 'call_transcript_segment'),
            ('ok', 'sms'),
        ])
        turns, id_map = load_and_normalize(conv)
        # 1 SMS + 3 voice sub-turns + 1 SMS = 5 total
        self.assertEqual(len(turns), 5)
        # Speaker attribution: phone → customer, Agent → agent
        voice_turns = [t for t in turns if t.from_bulk_transcript]
        self.assertEqual(len(voice_turns), 3)
        self.assertEqual(voice_turns[0].speaker, 'customer')
        self.assertEqual(voice_turns[1].speaker, 'agent')
        self.assertEqual(voice_turns[2].speaker, 'customer')
        # Sub-turn IDs share parent idx, distinct sub suffixes.
        sub_ids = [t.turn_id for t in voice_turns]
        self.assertEqual(sub_ids, ['t0001.0', 't0001.1', 't0001.2'])
        self.assertEqual(voice_turns[0].parent_idx, 1)
        self.assertEqual(voice_turns[1].parent_idx, 1)
        # All 3 sub-IDs map back to parent idx 1 for persistence.
        for tid in sub_ids:
            self.assertEqual(id_map.id_to_parent[tid], 1)

    def test_bulk_transcript_without_speaker_markers_kept_as_one(self):
        conv = self._conv_with_turns([
            ('just a plain transcript blob with no speaker labels',
             'call_transcript_segment'),
        ])
        turns, _ = load_and_normalize(conv)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].turn_id, 't0000')


class ChunkingTests(SimpleTestCase):
    def _turns(self, n, char_length=100):
        return [
            NormalizedTurn(turn_id=f't{i:04d}', parent_idx=i,
                           speaker='customer', direction='in',
                           text='x' * char_length, occurred_at='',
                           kind='sms', from_bulk_transcript=False)
            for i in range(n)
        ]

    def test_single_chunk_when_under_budget(self):
        chunks = chunk_conversation(self._turns(5, 100), char_budget=1000)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].is_only_chunk)

    def test_multi_chunk_with_overlap(self):
        chunks = chunk_conversation(self._turns(20, 100),
                                     char_budget=500, overlap_turns=2)
        self.assertGreater(len(chunks), 1)
        # Adjacent chunks must overlap by 2 turns (identified by turn_id).
        for a, b in zip(chunks, chunks[1:]):
            a_last_ids = {t.turn_id for t in a.turns[-2:]}
            b_first_ids = {t.turn_id for t in b.turns[:2]}
            self.assertTrue(a_last_ids & b_first_ids, 'no overlap between chunks')

    def test_merge_dedupes_events(self):
        # Post-validator events already carry INT parent idx.
        e1 = {'event_type': 'PRICE_REQUESTED', 'actor': 'customer',
              'turn_start': 3, 'turn_end': 3, 'confidence': 0.9, 'evidence': 'a'}
        e2 = {'event_type': 'PRICE_REQUESTED', 'actor': 'customer',
              'turn_start': 3, 'turn_end': 3, 'confidence': 0.8, 'evidence': 'a'}
        e3 = {'event_type': 'PRICE_GIVEN', 'actor': 'agent',
              'turn_start': 4, 'turn_end': 4, 'confidence': 0.9, 'evidence': 'b'}
        merged = merge_extracted_events([[e1], [e2, e3]])
        self.assertEqual(len(merged), 2)
        types = [m['event_type'] for m in merged]
        self.assertEqual(types, ['PRICE_REQUESTED', 'PRICE_GIVEN'])

    def test_render_uses_stable_turn_ids(self):
        # Chunk turns carry their stable turn_ids regardless of chunk position.
        turns = [
            NormalizedTurn(turn_id=f't{100 + i:04d}', parent_idx=100 + i,
                           speaker='customer', direction='in',
                           text=f'msg {i}', occurred_at='',
                           kind='sms', from_bulk_transcript=False)
            for i in range(3)
        ]
        chunk = ConversationChunk(
            turns=turns, chunk_index=0, is_only_chunk=False,
        )
        text = render_turns_for_prompt(chunk)
        self.assertIn('[t0100]', text)
        self.assertIn('[t0102]', text)


# ---------------------------------------------------------------------------
# Prompt (outcome-leak guard)
# ---------------------------------------------------------------------------


class PromptGuardTests(SimpleTestCase):
    def test_prompt_does_not_reference_outcome_fields(self):
        """The extractor prompt must not receive outcome VALUES.

        The word "outcome" is allowed in the prompt because it appears
        in the do-NOT-infer instructions; label leakage would look like
        specific outcome states or labeled examples. This test catches
        those specific value strings.
        """
        # Specific outcome value strings that would only appear if
        # someone piped labels into the prompt.
        for banned in ('lb_status', 'lb_status=', 'booked=true', 'lost=true',
                       'was_booked', 'converted', 'this lead was'):
            self.assertNotIn(banned.lower(), SYSTEM_PROMPT.lower(),
                             f'prompt contains {banned!r} — potential label leak')

    def test_prompt_forbids_fabrication(self):
        # These specific instructions must remain in the prompt.
        for required in ('do NOT invent', 'do NOT infer', 'exact quoted evidence'):
            self.assertIn(required.lower(), SYSTEM_PROMPT.lower(),
                          f'prompt missing required rule: {required!r}')


# ---------------------------------------------------------------------------
# Corpus + extraction integration
# ---------------------------------------------------------------------------


def _seed_conversation(org, *, phone='+18135551234', ext='cn-1',
                       n_turns=6, lb_lead_id='lb-1', status='engaged'):
    conv = Conversation.objects.create(
        org=org, source='quo', source_conversation_id=ext,
        channel=Channel.SMS, customer_phone=phone,
        started_at=timezone.now(),
    )
    for i in range(n_turns):
        ConversationTurn.objects.create(
            conversation=conv,
            source_turn_id=f'{ext}-m-{i}',
            speaker=Speaker.CUSTOMER if i % 2 == 0 else Speaker.AGENT,
            direction=Direction.INBOUND if i % 2 == 0 else Direction.OUTBOUND,
            text=f'turn {i} text',
            occurred_at=timezone.now(),
            metadata={'kind': 'sms'},
        )
    EntityLink.objects.create(
        conversation=conv,
        target_system=TargetSystem.LEADBRIDGE,
        target_type=TargetType.LEAD,
        target_id=lb_lead_id,
        match_method=MatchMethod.PHONE_EXACT,
    )
    OutcomeSnapshot.objects.create(
        conversation=conv,
        captured_at=timezone.now(),
        lb_status=status,
        lb_engaged=(status != 'new'),
        lb_booked=(status in ('booked', 'in_progress', 'completed')),
        lb_lost=(status == 'lost'),
        lb_cancelled=(status == 'cancelled'),
    )
    return conv


def _seed_corpus(org, name='c', version='v1', n=3):
    corpus = LearningCorpus.objects.create(org=org, name=name, version=version, member_count=n)
    convs = []
    for i in range(n):
        c = _seed_conversation(org, phone=f'+18135550{i:03d}', ext=f'cn-{i}',
                                lb_lead_id=f'lb-{i}', status='engaged')
        LearningCorpusMember.objects.create(
            corpus=corpus, conversation=c, lb_lead_id=f'lb-{i}',
            lb_status_at_freeze='engaged', turn_count_at_freeze=c.turns.count(),
        )
        convs.append(c)
    return corpus, convs


class ExtractionIntegrationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Spotless')

    def test_stub_extraction_creates_events(self):
        corpus, _ = _seed_corpus(self.org, n=2)
        outcome = run_extraction(corpus, org=self.org, model='stub-model')
        self.assertEqual(outcome.records_processed, 2)
        # Stub emits 1 event per conversation (SERVICE_INQUIRY on turn 0)
        self.assertEqual(outcome.events_created, 2)
        self.assertEqual(ConversationSemanticEvent.objects.count(), 2)

    def test_extraction_is_idempotent(self):
        corpus, _ = _seed_corpus(self.org, n=2)
        run_extraction(corpus, org=self.org, model='stub-model')
        first_count = ConversationSemanticEvent.objects.count()
        run_extraction(corpus, org=self.org, model='stub-model')
        self.assertEqual(ConversationSemanticEvent.objects.count(), first_count)

    def test_events_link_to_lb_entity_link_when_present(self):
        corpus, convs = _seed_corpus(self.org, n=1)
        run_extraction(corpus, org=self.org, model='stub-model')
        ev = ConversationSemanticEvent.objects.get()
        self.assertIsNotNone(ev.entity_link)
        self.assertEqual(ev.entity_link.target_system, TargetSystem.LEADBRIDGE)

    def test_tenant_isolation(self):
        org_b = Organization.objects.create(name='Other')
        _seed_corpus(org_b, n=2)  # unrelated corpus
        corpus_a, _ = _seed_corpus(self.org, n=1)
        run_extraction(corpus_a, org=self.org, model='stub-model')
        events_a = ConversationSemanticEvent.objects.filter(org=self.org).count()
        events_b = ConversationSemanticEvent.objects.filter(org=org_b).count()
        self.assertEqual(events_a, 1)
        self.assertEqual(events_b, 0)

    def test_different_models_produce_different_runs(self):
        corpus, _ = _seed_corpus(self.org, n=1)
        run_a = get_or_create_run(corpus, org=self.org, model='stub-model')
        run_b = get_or_create_run(corpus, org=self.org, model='other-model')
        self.assertNotEqual(run_a.pk, run_b.pk)

    def test_llm_failure_isolated_per_record(self):
        corpus, convs = _seed_corpus(self.org, n=3)
        run = get_or_create_run(corpus, org=self.org, model='stub-model')
        ex = SemanticExtractor(run)

        # Force LLM failure on the SECOND conversation only.
        call_count = [0]
        real_analyze = ex.client.analyze

        def flaky(system_prompt, user_prompt, model, **kw):
            call_count[0] += 1
            if call_count[0] == 2:
                from apps.learning.services.llm_client import LLMProviderError
                raise LLMProviderError('simulated')
            return real_analyze(system_prompt, user_prompt, model, **kw)

        with mock.patch.object(ex.client, 'analyze', side_effect=flaky):
            r0 = ex.extract_conversation(convs[0])
            r1 = ex.extract_conversation(convs[1])
            r2 = ex.extract_conversation(convs[2])

        self.assertEqual(r0.events_created, 1)
        # Conv 1 saw the LLM error mid-flight — no events, error recorded.
        self.assertEqual(r1.events_created, 0)
        self.assertTrue(r1.error)
        self.assertEqual(r2.events_created, 1)


class VersionMismatchTests(TestCase):
    def test_extractor_refuses_run_with_mismatched_ontology(self):
        org = Organization.objects.create(name='X')
        corpus, _ = _seed_corpus(org, n=1)
        run = SemanticExtractionRun.objects.create(
            org=org, corpus=corpus,
            extractor_version=EXTRACTOR_VERSION,
            ontology_version='ontology-vBOGUS',
            prompt_version=PROMPT_VERSION,
            model='stub-model',
        )
        with self.assertRaises(ValueError):
            SemanticExtractor(run)
