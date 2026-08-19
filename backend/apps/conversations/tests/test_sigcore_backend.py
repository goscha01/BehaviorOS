"""Regression tests for the QuoAdapter's Sigcore HTTP backend.

Exercises the two bugs surfaced by the 2026-08-19 smoke import:

1. Message cursor pagination — Sigcore's `before` parameter is
   inclusive on the cursor value AND sorts messages ascending. Using
   `batch[-1]` (newest) as cursor causes a near-duplicate loop.
   Correct cursor is `batch[0]` (oldest) with ID-based dedupe.

2. Transcript N+1 — `GET /calls/:id/transcript` per call is expensive.
   Gated by SIGCORE_FETCH_TRANSCRIPTS (default OFF); OFF path preserves
   the call as a marker turn without the body.

Both tests use `responses` … but rather than add a dep, we monkey-patch
`requests.Session.get` via a stub class. Local to this module, no
extra imports.
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.conversations.adapters.quo import QuoAdapter


# ---------------------------------------------------------------------------
# Sigcore stub
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self):
        return self._payload


class _StubSession:
    """Records every GET so tests can assert on call counts + params.
    `route_map` is {path_suffix: [payload_page1, payload_page2, ...]}
    keyed by URL path. Matching is EXACT-suffix on the URL path (before
    the query string), so `/conversations` doesn't accidentally match
    `/conversations/:id/messages`.
    """

    def __init__(self, route_map):
        self._route_map = {k: list(v) for k, v in route_map.items()}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        # Strip query string if requests appended one — the adapter uses
        # `params=` so this is usually clean, but be defensive.
        path = url.split('?', 1)[0]
        for suffix, payloads in self._route_map.items():
            if path.endswith(suffix):
                if not payloads:
                    return _StubResponse({'data': [], 'meta': {'hasMore': False}})
                payload = payloads.pop(0)
                return _StubResponse(payload)
        return _StubResponse({'data': [], 'meta': {'hasMore': False}})


def _configured_adapter(**kwargs):
    return QuoAdapter(
        sigcore_url='https://sigcore.example/api',
        sigcore_api_key='sc_test',
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Message pagination regression
# ---------------------------------------------------------------------------


class MessagePaginationTests(SimpleTestCase):
    """Confirms the fix: cursor advances by batch[0] (oldest), dedupes by ID,
    and cannot infinite-loop on inclusive-cursor overlap.
    """

    LIST_CONV = {
        'data': [{
            'id': 'sc-conv-1',
            'externalId': 'ext-conv-1',
            'provider': 'openphone',
            'phoneNumber': '+18139212100',
            'participantPhoneNumber': '+19045550101',
            'createdAt': '2026-06-01T10:00:00Z',
            'lastMessageAt': '2026-06-01T12:00:00Z',
        }],
        'meta': {'total': 1, 'totalPages': 1},
    }

    # Simulate Sigcore's inclusive-cursor behavior: page 2 with
    # `before=<page1[0].createdAt>` returns 3 older messages PLUS the
    # cursor message itself. Client-side dedupe should skip the overlap.
    MSGS_PAGE_1 = {
        'data': [
            # Ascending order (oldest → newest) — this is what Sigcore returns.
            {'id': 'm-005', 'body': 'e', 'direction': 'in',
             'fromNumber': '+19045550101', 'toNumber': '+18139212100',
             'createdAt': '2026-06-01T09:05:00Z', 'status': 'delivered'},
            {'id': 'm-006', 'body': 'f', 'direction': 'in',
             'fromNumber': '+19045550101', 'toNumber': '+18139212100',
             'createdAt': '2026-06-01T09:06:00Z', 'status': 'delivered'},
            {'id': 'm-007', 'body': 'g', 'direction': 'in',
             'fromNumber': '+19045550101', 'toNumber': '+18139212100',
             'createdAt': '2026-06-01T09:07:00Z', 'status': 'delivered'},
        ],
        'meta': {'hasMore': True},
    }

    MSGS_PAGE_2 = {
        'data': [
            {'id': 'm-002', 'body': 'b', 'direction': 'in',
             'fromNumber': '+19045550101', 'toNumber': '+18139212100',
             'createdAt': '2026-06-01T09:02:00Z', 'status': 'delivered'},
            {'id': 'm-003', 'body': 'c', 'direction': 'in',
             'fromNumber': '+19045550101', 'toNumber': '+18139212100',
             'createdAt': '2026-06-01T09:03:00Z', 'status': 'delivered'},
            {'id': 'm-004', 'body': 'd', 'direction': 'in',
             'fromNumber': '+19045550101', 'toNumber': '+18139212100',
             'createdAt': '2026-06-01T09:04:00Z', 'status': 'delivered'},
            # ← this is m-005 again — inclusive-cursor overlap the adapter must skip
            {'id': 'm-005', 'body': 'e', 'direction': 'in',
             'fromNumber': '+19045550101', 'toNumber': '+18139212100',
             'createdAt': '2026-06-01T09:05:00Z', 'status': 'delivered'},
        ],
        'meta': {'hasMore': True},
    }

    MSGS_PAGE_3 = {
        'data': [
            {'id': 'm-001', 'body': 'a', 'direction': 'in',
             'fromNumber': '+19045550101', 'toNumber': '+18139212100',
             'createdAt': '2026-06-01T09:01:00Z', 'status': 'delivered'},
            # ← m-002 overlap
            {'id': 'm-002', 'body': 'b', 'direction': 'in',
             'fromNumber': '+19045550101', 'toNumber': '+18139212100',
             'createdAt': '2026-06-01T09:02:00Z', 'status': 'delivered'},
        ],
        'meta': {'hasMore': False},
    }

    CALLS_EMPTY = {'data': []}

    def _run_adapter(self, fetch_transcripts=False):
        stub = _StubSession({
            '/conversations': [self.LIST_CONV, {'data': []}],
            '/conversations/sc-conv-1/messages':
                [self.MSGS_PAGE_1, self.MSGS_PAGE_2, self.MSGS_PAGE_3],
            '/conversations/sc-conv-1/calls': [self.CALLS_EMPTY],
        })
        with mock.patch('requests.Session', return_value=stub):
            adapter = _configured_adapter(fetch_transcripts=fetch_transcripts)
            records = list(adapter.fetch_records())
        return records, stub

    def test_cursor_advances_and_dedupes_no_infinite_loop(self):
        records, stub = self._run_adapter()
        self.assertEqual(len(records), 1)
        env = records[0]

        # After 3 pages with overlap on m-005 (page1↔2) and m-002 (page2↔3),
        # the deduped set should be exactly 7 unique messages, not 10.
        msg_ids = [m['id'] for m in env['messages']]
        self.assertEqual(len(msg_ids), 7)
        self.assertEqual(len(set(msg_ids)), 7)
        # All 7 unique IDs present.
        self.assertSetEqual(
            set(msg_ids),
            {'m-001', 'm-002', 'm-003', 'm-004', 'm-005', 'm-006', 'm-007'},
        )

    def test_cursor_uses_batch_first_not_last(self):
        _, stub = self._run_adapter()
        message_calls = [
            (u, p) for (u, p) in stub.calls
            if '/conversations/sc-conv-1/messages' in u
        ]
        # 3 calls to messages endpoint.
        self.assertEqual(len(message_calls), 3)
        # Page 1: no `before` cursor.
        self.assertNotIn('before', message_calls[0][1])
        # Page 2's cursor MUST be page 1's OLDEST (m-005 createdAt), NOT the newest.
        # If the adapter used batch[-1] (newest, m-007), the cursor would
        # be 09:07:00 — that's the bug we're regressing.
        self.assertEqual(message_calls[1][1]['before'], '2026-06-01T09:05:00Z')
        # Page 3's cursor: page 2's OLDEST (m-002 after dedupe... but
        # the raw batch[0] is m-002 too). Cursor should have advanced.
        self.assertEqual(message_calls[2][1]['before'], '2026-06-01T09:02:00Z')

    def test_stops_when_hasMore_false(self):
        _, stub = self._run_adapter()
        # Page 3 has hasMore=false → no page 4 request.
        message_calls = [
            (u, p) for (u, p) in stub.calls
            if '/conversations/sc-conv-1/messages' in u
        ]
        self.assertEqual(len(message_calls), 3)

    def test_stops_when_page_has_only_duplicates(self):
        """If Sigcore returns a page that's entirely duplicates (shouldn't
        happen with a well-formed cursor but defence-in-depth), the loop
        must break — no infinite retry.
        """
        all_dupes = {
            'data': [
                {'id': 'm-005', 'body': 'e', 'direction': 'in',
                 'fromNumber': '+19045550101', 'toNumber': '+18139212100',
                 'createdAt': '2026-06-01T09:05:00Z', 'status': 'delivered'},
            ],
            'meta': {'hasMore': True},  # server says more — client must still stop
        }
        stub = _StubSession({
            '/conversations': [self.LIST_CONV, {'data': []}],
            '/conversations/sc-conv-1/messages': [self.MSGS_PAGE_1, all_dupes],
            '/conversations/sc-conv-1/calls': [self.CALLS_EMPTY],
        })
        with mock.patch('requests.Session', return_value=stub):
            adapter = _configured_adapter()
            records = list(adapter.fetch_records())
        self.assertEqual(len(records), 1)
        # Adapter should have stopped after the all-dupes page. Exactly 2
        # messages calls (page 1 + all-dupes page) — no third call.
        message_calls = [
            (u, p) for (u, p) in stub.calls
            if '/conversations/sc-conv-1/messages' in u
        ]
        self.assertEqual(len(message_calls), 2)


# ---------------------------------------------------------------------------
# Transcript flag
# ---------------------------------------------------------------------------


class TranscriptFlagTests(SimpleTestCase):
    LIST_CONV = {
        'data': [{
            'id': 'sc-conv-2',
            'externalId': 'ext-conv-2',
            'provider': 'openphone',
            'phoneNumber': '+18139212100',
            'participantPhoneNumber': '+19045550202',
            'createdAt': '2026-06-01T10:00:00Z',
            'lastMessageAt': '2026-06-01T10:05:00Z',
        }],
        'meta': {'total': 1, 'totalPages': 1},
    }

    CALLS_ONE = {
        'data': [{
            'id': 'sc-call-1',
            'providerCallId': 'CALL_qw-1',
            'direction': 'in',
            'duration': 120,
            'recordingUrl': 'https://api.quo.com/rec/CALL_qw-1.mp3',
            'createdAt': '2026-06-01T10:00:00Z',
            'answeredAt': '2026-06-01T10:00:05Z',
            'endedAt': '2026-06-01T10:02:05Z',
        }],
    }

    TRANSCRIPT = {'data': {'transcript': 'Hello. This is the transcript body.',
                           'status': 'completed'}}

    def _make_stub(self):
        return _StubSession({
            '/conversations': [self.LIST_CONV, {'data': []}],
            '/conversations/sc-conv-2/messages':
                [{'data': [], 'meta': {'hasMore': False}}],
            '/conversations/sc-conv-2/calls': [self.CALLS_ONE],
            '/calls/sc-call-1/transcript': [self.TRANSCRIPT],
        })

    def test_flag_off_skips_transcript_endpoint(self):
        stub = self._make_stub()
        with mock.patch('requests.Session', return_value=stub):
            adapter = _configured_adapter(fetch_transcripts=False)
            records = list(adapter.fetch_records())

        transcript_calls = [u for (u, _) in stub.calls if '/transcript' in u]
        self.assertEqual(transcript_calls, [])

        # Call is still yielded — normalizer will emit a call_no_transcript turn.
        self.assertEqual(len(records), 1)
        env = records[0]
        self.assertEqual(len(env['calls']), 1)
        # No transcript key set on the call.
        self.assertNotIn('transcript', env['calls'][0])

    def test_flag_on_fetches_transcript_and_injects_single_segment(self):
        stub = self._make_stub()
        with mock.patch('requests.Session', return_value=stub):
            adapter = _configured_adapter(fetch_transcripts=True)
            records = list(adapter.fetch_records())

        transcript_calls = [u for (u, _) in stub.calls if '/transcript' in u]
        self.assertEqual(len(transcript_calls), 1)

        env = records[0]
        call = env['calls'][0]
        self.assertIn('transcript', call)
        self.assertEqual(len(call['transcript']), 1)
        seg = call['transcript'][0]
        self.assertEqual(seg['text'], 'Hello. This is the transcript body.')
        self.assertEqual(seg['speaker'], 'unknown')  # bulk text has no speaker

    @override_settings(SIGCORE_FETCH_TRANSCRIPTS=True)
    def test_settings_flag_default_used_when_ctor_arg_omitted(self):
        stub = self._make_stub()
        with mock.patch('requests.Session', return_value=stub):
            adapter = _configured_adapter()  # no fetch_transcripts arg
            list(adapter.fetch_records())
        # Setting was ON → transcript endpoint hit.
        transcript_calls = [u for (u, _) in stub.calls if '/transcript' in u]
        self.assertEqual(len(transcript_calls), 1)

    @override_settings(SIGCORE_FETCH_TRANSCRIPTS=False)
    def test_settings_flag_default_off_when_ctor_arg_omitted(self):
        stub = self._make_stub()
        with mock.patch('requests.Session', return_value=stub):
            adapter = _configured_adapter()
            list(adapter.fetch_records())
        transcript_calls = [u for (u, _) in stub.calls if '/transcript' in u]
        self.assertEqual(transcript_calls, [])
