"""QuoAdapter fixture-backend tests.

The HTTP backend is covered separately in Phase 10 when the LB proxy
endpoint is available; today we only test that the URL toggle correctly
selects the backend, not the HTTP request/response cycle.
"""

from datetime import datetime, timezone

from django.test import SimpleTestCase, override_settings

from apps.conversations.adapters.quo import QuoAdapter


class QuoAdapterFixtureTests(SimpleTestCase):
    def test_fixture_backend_yields_expected_scenarios(self):
        adapter = QuoAdapter(proxy_url='', proxy_token='')
        records = list(adapter.fetch_records())
        ids = [r['id'] for r in records]

        # All 8 fixture files are expected. `duplicate_source_record.json`
        # deliberately reuses `CN_inbound_sms_001` — the adapter yields it
        # twice, letting the upsert layer downstream exercise its idempotency.
        self.assertIn('CN_voice_transcript_001', ids)
        self.assertIn('CN_inbound_sms_001', ids)
        self.assertIn('CN_outbound_sms_001', ids)
        self.assertIn('CN_multi_thread_001', ids)
        self.assertIn('CN_no_transcript_001', ids)
        self.assertIn('CN_no_phone_001', ids)
        self.assertIn('CN_partial_001', ids)
        # Duplicate scenario: ID appears twice in the stream.
        self.assertEqual(ids.count('CN_inbound_sms_001'), 2)

    def test_limit_caps_records(self):
        adapter = QuoAdapter(proxy_url='', proxy_token='')
        records = list(adapter.fetch_records(limit=3))
        self.assertEqual(len(records), 3)

    def test_since_filters_fixture_records(self):
        adapter = QuoAdapter(proxy_url='', proxy_token='')
        # Only records with lastActivityAt >= 2026-06-05 should pass.
        cutoff = datetime(2026, 6, 5, tzinfo=timezone.utc)
        records = list(adapter.fetch_records(since=cutoff))
        ids = {r['id'] for r in records}
        self.assertIn('CN_no_transcript_001', ids)   # 2026-06-05
        self.assertIn('CN_no_phone_001', ids)        # 2026-06-06
        self.assertNotIn('CN_voice_transcript_001', ids)  # 2026-06-01
        self.assertNotIn('CN_inbound_sms_001', ids)       # 2026-06-02

    def test_until_filters_fixture_records(self):
        adapter = QuoAdapter(proxy_url='', proxy_token='')
        cutoff = datetime(2026, 6, 3, tzinfo=timezone.utc)
        records = list(adapter.fetch_records(until=cutoff))
        ids = {r['id'] for r in records}
        self.assertIn('CN_voice_transcript_001', ids)  # 2026-06-01
        self.assertIn('CN_inbound_sms_001', ids)       # 2026-06-02
        self.assertNotIn('CN_outbound_sms_001', ids)   # 2026-06-03 exact bound (exclusive)


class QuoAdapterBackendSelectionTests(SimpleTestCase):
    @override_settings(
        LEADBRIDGE_QUO_PROXY_URL='',
        LEADBRIDGE_QUO_PROXY_TOKEN='',
    )
    def test_empty_url_uses_fixture_backend(self):
        adapter = QuoAdapter()
        # Fixtures exist so we should get results.
        records = list(adapter.fetch_records(limit=1))
        self.assertEqual(len(records), 1)

    @override_settings(
        LEADBRIDGE_QUO_PROXY_URL='https://leadbridge.example/api/v1/learning/quo/conversations',
        LEADBRIDGE_QUO_PROXY_TOKEN='test-token',
    )
    def test_configured_url_switches_backend_selection(self):
        # We don't actually make an HTTP call in this test — just confirm
        # the adapter would take the HTTP path. If it took the fixture path,
        # it'd yield without touching requests at all.
        adapter = QuoAdapter()
        self.assertTrue(adapter._proxy_url)  # noqa: SLF001 — testing the selector
