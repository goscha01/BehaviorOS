"""Normalizer tests for Quo records."""

from django.test import SimpleTestCase

from apps.conversations.models import Channel, Direction, Speaker
from apps.conversations.normalization.quo import (
    QuoNormalizationError,
    normalize_quo_record,
)


VOICE_WITH_TRANSCRIPT = {
    'id': 'CN_voice_001',
    'channel': 'voice',
    'workspaceNumber': '+18139212100',
    'participantNumber': '(904) 555-0101',
    'createdAt': '2026-06-01T14:30:00Z',
    'lastActivityAt': '2026-06-01T14:34:12Z',
    'messages': [],
    'calls': [{
        'id': 'CALL_v001',
        'direction': 'in',
        'duration': 252,
        'recordingUrl': 'https://api.quo.com/recordings/CALL_v001.mp3',
        'createdAt': '2026-06-01T14:30:00Z',
        'answeredAt': '2026-06-01T14:30:04Z',
        'endedAt': '2026-06-01T14:34:12Z',
        'transcript': [
            {'id': 's1', 'speaker': 'agent', 'text': 'Hello',
             'startedAt': '2026-06-01T14:30:04Z', 'confidence': 0.97},
            {'id': 's2', 'speaker': 'customer', 'text': 'Hi',
             'startedAt': '2026-06-01T14:30:12Z', 'confidence': 0.94},
        ],
    }],
}

INBOUND_SMS = {
    'id': 'CN_sms_001',
    'channel': 'sms',
    'workspaceNumber': '+18139212100',
    'participantNumber': '+19045550202',
    'createdAt': '2026-06-02T10:15:00Z',
    'lastActivityAt': '2026-06-02T10:15:00Z',
    'messages': [{
        'id': 'MSG_i001',
        'body': 'Hi, do you clean move-out apartments?',
        'direction': 'in',
        'fromNumber': '+19045550202',
        'toNumber': '+18139212100',
        'createdAt': '2026-06-02T10:15:00Z',
        'status': 'delivered',
    }],
    'calls': [],
}


class NormalizeVoiceTests(SimpleTestCase):
    def test_channel_inferred_from_calls_when_missing(self):
        rec = dict(VOICE_WITH_TRANSCRIPT)
        rec.pop('channel')
        out = normalize_quo_record(rec)
        self.assertEqual(out.channel, Channel.VOICE)

    def test_transcript_produces_one_turn_per_segment(self):
        out = normalize_quo_record(VOICE_WITH_TRANSCRIPT)
        self.assertEqual(len(out.turns), 2)
        self.assertEqual(out.turns[0].speaker, Speaker.AGENT)
        self.assertEqual(out.turns[1].speaker, Speaker.CUSTOMER)
        # Both turns share the CALL direction.
        for t in out.turns:
            self.assertEqual(t.direction, Direction.INBOUND)
            self.assertEqual(t.metadata['kind'], 'call_transcript_segment')

    def test_transcript_turn_ids_are_stable(self):
        out1 = normalize_quo_record(VOICE_WITH_TRANSCRIPT)
        out2 = normalize_quo_record(VOICE_WITH_TRANSCRIPT)
        ids1 = [t.source_turn_id for t in out1.turns]
        ids2 = [t.source_turn_id for t in out2.turns]
        self.assertEqual(ids1, ids2)
        self.assertTrue(all(tid.startswith('call:CALL_v001:') for tid in ids1))

    def test_phone_normalized_to_e164(self):
        out = normalize_quo_record(VOICE_WITH_TRANSCRIPT)
        self.assertEqual(out.customer_phone, '+19045550101')


class NormalizeSmsTests(SimpleTestCase):
    def test_single_inbound_message(self):
        out = normalize_quo_record(INBOUND_SMS)
        self.assertEqual(out.channel, Channel.SMS)
        self.assertEqual(len(out.turns), 1)
        turn = out.turns[0]
        self.assertEqual(turn.speaker, Speaker.CUSTOMER)
        self.assertEqual(turn.direction, Direction.INBOUND)
        self.assertEqual(turn.source_turn_id, 'msg:MSG_i001')
        self.assertEqual(turn.metadata['status'], 'delivered')

    def test_outbound_message_maps_to_agent_speaker(self):
        rec = dict(INBOUND_SMS)
        rec['id'] = 'CN_sms_out'
        rec['messages'] = [{
            'id': 'MSG_o1', 'body': 'Reminder', 'direction': 'out',
            'fromNumber': '+18139212100', 'toNumber': '+19045550202',
            'createdAt': '2026-06-03T09:00:00Z',
        }]
        out = normalize_quo_record(rec)
        turn = out.turns[0]
        self.assertEqual(turn.speaker, Speaker.AGENT)
        self.assertEqual(turn.direction, Direction.OUTBOUND)


class NormalizeMissingDataTests(SimpleTestCase):
    def test_missing_id_raises(self):
        with self.assertRaises(QuoNormalizationError):
            normalize_quo_record({'messages': [], 'calls': []})

    def test_conversation_with_zero_turns_raises(self):
        with self.assertRaises(QuoNormalizationError):
            normalize_quo_record({
                'id': 'CN_empty', 'messages': [], 'calls': [],
            })

    def test_missing_transcript_emits_one_marker_turn(self):
        rec = {
            'id': 'CN_no_transcript',
            'channel': 'voice',
            'workspaceNumber': '+18139212100',
            'participantNumber': '+19045550505',
            'createdAt': '2026-06-05T11:00:00Z',
            'lastActivityAt': '2026-06-05T11:04:35Z',
            'messages': [],
            'calls': [{
                'id': 'CALL_nt', 'direction': 'in', 'duration': 275,
                'recordingUrl': 'https://api.quo.com/recordings/CALL_nt.mp3',
                'createdAt': '2026-06-05T11:00:00Z',
                'answeredAt': '2026-06-05T11:00:03Z',
                'endedAt': '2026-06-05T11:04:35Z',
            }],
        }
        out = normalize_quo_record(rec)
        self.assertEqual(len(out.turns), 1)
        self.assertEqual(out.turns[0].metadata['kind'], 'call_no_transcript')
        self.assertEqual(out.turns[0].speaker, Speaker.UNKNOWN)
        self.assertEqual(out.turns[0].text, '')

    def test_invalid_phone_leaves_customer_phone_empty(self):
        rec = dict(INBOUND_SMS)
        rec['id'] = 'CN_bad_phone'
        rec['participantNumber'] = 'call me maybe'
        out = normalize_quo_record(rec)
        self.assertEqual(out.customer_phone, '')
        # But the conversation is otherwise fine.
        self.assertEqual(len(out.turns), 1)

    def test_partial_records_skip_bad_turns(self):
        rec = {
            'id': 'CN_partial',
            'workspaceNumber': '+18139212100',
            'participantNumber': '+19045550606',
            'messages': [
                # missing createdAt → skipped
                {'id': 'MSG_p1', 'body': 'a', 'direction': 'in',
                 'fromNumber': '+19045550606'},
                # valid
                {'id': 'MSG_p2', 'body': 'b', 'direction': 'in',
                 'fromNumber': '+19045550606',
                 'createdAt': '2026-06-07T15:00:00Z'},
                # missing id → skipped
                {'body': 'c', 'direction': 'in',
                 'createdAt': '2026-06-07T15:01:00Z'},
            ],
        }
        out = normalize_quo_record(rec)
        self.assertEqual(len(out.turns), 1)
        self.assertEqual(out.turns[0].source_turn_id, 'msg:MSG_p2')


class UnknownFieldsRetainedTests(SimpleTestCase):
    def test_unknown_top_level_fields_stored_in_metadata(self):
        rec = dict(INBOUND_SMS)
        rec['id'] = 'CN_unknown'
        rec['some_new_field'] = 'future proofing'
        rec['nested_thing'] = {'a': 1}
        out = normalize_quo_record(rec)
        self.assertIn('unknown_fields', out.metadata)
        self.assertEqual(out.metadata['unknown_fields']['some_new_field'],
                         'future proofing')
        self.assertEqual(out.metadata['unknown_fields']['nested_thing'], {'a': 1})

    def test_workspace_number_captured(self):
        out = normalize_quo_record(INBOUND_SMS)
        self.assertEqual(out.metadata['workspace_number'], '+18139212100')
