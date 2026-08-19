"""QuoAdapter fixture-backend tests.

The Sigcore HTTP backend is exercised end-to-end during the smoke import
against real Sigcore; today we only test that the settings toggle
correctly selects fixture vs HTTP backend, not the HTTP cycle itself.
"""

from datetime import datetime, timezone

from django.test import SimpleTestCase, override_settings

from apps.conversations.adapters.quo import QuoAdapter


class QuoAdapterFixtureTests(SimpleTestCase):
    def test_fixture_backend_yields_expected_scenarios(self):
        adapter = QuoAdapter(sigcore_url='', sigcore_service_key='')
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
        adapter = QuoAdapter(sigcore_url='', sigcore_service_key='')
        records = list(adapter.fetch_records(limit=3))
        self.assertEqual(len(records), 3)

    def test_since_filters_fixture_records(self):
        adapter = QuoAdapter(sigcore_url='', sigcore_service_key='')
        # Only records with lastActivityAt >= 2026-06-05 should pass.
        cutoff = datetime(2026, 6, 5, tzinfo=timezone.utc)
        records = list(adapter.fetch_records(since=cutoff))
        ids = {r['id'] for r in records}
        self.assertIn('CN_no_transcript_001', ids)   # 2026-06-05
        self.assertIn('CN_no_phone_001', ids)        # 2026-06-06
        self.assertNotIn('CN_voice_transcript_001', ids)  # 2026-06-01
        self.assertNotIn('CN_inbound_sms_001', ids)       # 2026-06-02

    def test_until_filters_fixture_records(self):
        adapter = QuoAdapter(sigcore_url='', sigcore_service_key='')
        cutoff = datetime(2026, 6, 3, tzinfo=timezone.utc)
        records = list(adapter.fetch_records(until=cutoff))
        ids = {r['id'] for r in records}
        self.assertIn('CN_voice_transcript_001', ids)  # 2026-06-01
        self.assertIn('CN_inbound_sms_001', ids)       # 2026-06-02
        self.assertNotIn('CN_outbound_sms_001', ids)   # 2026-06-03 exact bound (exclusive)


class QuoAdapterBackendSelectionTests(SimpleTestCase):
    @override_settings(SIGCORE_URL='', SIGCORE_SERVICE_KEY='')
    def test_empty_sigcore_url_uses_fixture_backend(self):
        adapter = QuoAdapter()
        # Fixtures exist so we should get results.
        records = list(adapter.fetch_records(limit=1))
        self.assertEqual(len(records), 1)

    @override_settings(
        SIGCORE_URL='https://sigcore.example',
        SIGCORE_SERVICE_KEY='test-service-key',
    )
    def test_sigcore_selected_only_when_workspace_id_supplied(self):
        # All three of (url, key, workspace_id) must be present to switch
        # to the HTTP backend. Without a workspace_id we still fall through
        # to fixtures — matches the constructor contract.
        adapter_no_ws = QuoAdapter()
        # Fixture backend produces at least one record.
        self.assertGreater(len(list(adapter_no_ws.fetch_records(limit=1))), 0)

        # With a workspace_id, the adapter would take the HTTP path — we
        # verify the internal state, not the network call.
        adapter_ws = QuoAdapter(sigcore_workspace_id='ws-test-1')
        self.assertTrue(adapter_ws._sigcore_url)  # noqa: SLF001
        self.assertTrue(adapter_ws._sigcore_workspace_id)  # noqa: SLF001
