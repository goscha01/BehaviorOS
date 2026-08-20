"""Tests for the EffectiveBehaviorConfig v1 cross-repo hashing contract.

Pure-Python tests — no DB required. These are the tests that PRODUCERS
(LB, later Callio) will mirror to prove their canonicalizer agrees with
BehaviorOS's reference. Any divergence here means measurement hashes
computed by LB won't match hashes recomputed by BehaviorOS, silently
breaking every measurement.
"""

from __future__ import annotations

import json

from django.test import SimpleTestCase

from apps.conversations.measurement.effective_config_contract import (
    EFFECTIVE_CONFIG_SCHEMA_VERSION, MANAGED_SUBSET_KEY, NOT_STAMPED_V1,
    ProvenanceStatus, SURFACE_V1_TOPLEVEL_KEYS, VECTOR_A_MINIMAL,
    VECTOR_B_ONE_MANAGED_RULE, canonical_json_bytes, compute_hashes,
    sha256_hex, validate_shape,
)


class ContractShapeTests(SimpleTestCase):
    def test_schema_version_locked(self):
        # If this changes, every downstream measurement's hash contract
        # changes. Not editable in place — introduce v2 instead.
        self.assertEqual(EFFECTIVE_CONFIG_SCHEMA_VERSION,
                          'lb-effective-config-v1')

    def test_toplevel_keys_locked(self):
        expected = (
            'ai', 'qualification', 'service_options', 'pricing',
            'scheduling', 'follow_up', 'workflow_strategy',
            'behavior_os_managed_rules',
        )
        self.assertEqual(SURFACE_V1_TOPLEVEL_KEYS, expected)

    def test_managed_subset_key_is_toplevel(self):
        self.assertIn(MANAGED_SUBSET_KEY, SURFACE_V1_TOPLEVEL_KEYS)

    def test_provenance_status_tokens_locked(self):
        self.assertEqual(ProvenanceStatus.OK, 'OK')
        self.assertEqual(ProvenanceStatus.HASH_FAILED, 'HASH_FAILED')
        self.assertEqual(ProvenanceStatus.PENDING, 'PENDING')

    def test_not_stamped_v1_includes_credentials_and_ui(self):
        # Guardrail against accidental removal — these fields must
        # NEVER be part of the hash.
        for k in ('oauth_access_token', 'display_name',
                   'subscription_tier', 'created_at'):
            self.assertIn(k, NOT_STAMPED_V1)


class CanonicalizationTests(SimpleTestCase):
    def test_key_order_normalized(self):
        # Two payloads with different key insertion order must hash
        # identically.
        a = {'z': 1, 'a': {'y': 2, 'b': 3}}
        b = {'a': {'b': 3, 'y': 2}, 'z': 1}
        self.assertEqual(canonical_json_bytes(a),
                          canonical_json_bytes(b))
        self.assertEqual(sha256_hex(canonical_json_bytes(a)),
                          sha256_hex(canonical_json_bytes(b)))

    def test_no_whitespace(self):
        # Compact separators — any drift here breaks producer agreement.
        out = canonical_json_bytes({'a': 1, 'b': [2, 3]})
        self.assertEqual(out, b'{"a":1,"b":[2,3]}')
        self.assertNotIn(b' ', out)

    def test_null_keys_preserved(self):
        # A key with None must appear in the canonical output — its
        # presence is behaviorally meaningful (e.g. "global_prompt is
        # unset" vs "global_prompt key removed").
        with_null = {'global_prompt': None, 'other': 1}
        without = {'other': 1}
        self.assertNotEqual(canonical_json_bytes(with_null),
                             canonical_json_bytes(without))
        self.assertIn(b'null', canonical_json_bytes(with_null))

    def test_array_order_preserved(self):
        # Arrays are semantic (rule ordering matters).
        a = [1, 2, 3]
        b = [3, 2, 1]
        self.assertNotEqual(canonical_json_bytes({'x': a}),
                             canonical_json_bytes({'x': b}))

    def test_non_ascii_survives_utf8(self):
        # Non-ASCII characters (customer language, tenant names) must
        # canonicalize identically on producer and consumer.
        out = canonical_json_bytes({'greeting': 'Olá'})
        # ensure_ascii=False so the raw UTF-8 bytes are emitted.
        self.assertIn('Olá'.encode('utf-8'), out)


class HashComputationTests(SimpleTestCase):
    def test_vector_a_minimal_hashes_are_deterministic(self):
        # Rerunning must produce identical hashes — guards against
        # accidental non-determinism (dict-iteration order, etc.).
        full_1, managed_1 = compute_hashes(VECTOR_A_MINIMAL)
        full_2, managed_2 = compute_hashes(VECTOR_A_MINIMAL)
        self.assertEqual(full_1, full_2)
        self.assertEqual(managed_1, managed_2)
        # SHA-256 hex is 64 chars
        self.assertEqual(len(full_1), 64)
        self.assertEqual(len(managed_1), 64)

    def test_vector_b_full_and_managed_differ(self):
        full, managed = compute_hashes(VECTOR_B_ONE_MANAGED_RULE)
        # full must differ from managed (full includes everything;
        # managed is a proper subset).
        self.assertNotEqual(full, managed)

    def test_changing_managed_subtree_changes_both_hashes(self):
        full1, managed1 = compute_hashes(VECTOR_B_ONE_MANAGED_RULE)
        mutated = json.loads(json.dumps(VECTOR_B_ONE_MANAGED_RULE))
        mutated['behavior_os_managed_rules']['by_profile']['prof_a'].append({
            'recommendation_id': 'R0003',
            'condition': 'BOOKING_REQUESTED',
            'summary': 'Confirm booking within one message.',
        })
        full2, managed2 = compute_hashes(mutated)
        # Both hashes shift — the treatment itself changed.
        self.assertNotEqual(full1, full2)
        self.assertNotEqual(managed1, managed2)

    def test_changing_non_managed_subtree_shifts_full_but_not_managed(self):
        # This is the contamination signal the whole system relies on.
        full1, managed1 = compute_hashes(VECTOR_B_ONE_MANAGED_RULE)
        mutated = json.loads(json.dumps(VECTOR_B_ONE_MANAGED_RULE))
        mutated['pricing']['by_profile']['prof_a']['base_hourly_cents'] = 9000
        full2, managed2 = compute_hashes(mutated)
        self.assertNotEqual(full1, full2)
        self.assertEqual(managed1, managed2)


class ValidateShapeTests(SimpleTestCase):
    def test_full_vector_a_passes(self):
        # No return, no raise
        validate_shape(VECTOR_A_MINIMAL)

    def test_missing_toplevel_key_raises(self):
        bad = dict(VECTOR_A_MINIMAL)
        bad.pop('pricing')
        with self.assertRaises(ValueError) as e:
            validate_shape(bad)
        self.assertIn('pricing', str(e.exception))

    def test_unknown_toplevel_key_raises(self):
        bad = dict(VECTOR_A_MINIMAL)
        bad['unexpected_new_key'] = {}
        with self.assertRaises(ValueError) as e:
            validate_shape(bad)
        self.assertIn('unexpected_new_key', str(e.exception))
