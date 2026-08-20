"""EffectiveBehaviorConfig v1 — cross-repo hashing contract.

This module is the AUTHORITATIVE definition of the effective-behavior-
config surface that source systems (LeadBridge today, Callio later) MUST
hash and stamp onto each new conversation. BehaviorOS's outcome
measurement pipeline consumes those hashes to distinguish clean
post-treatment cohorts from environment-contaminated ones.

The reason it lives here (not in LB) is that the CONSUMER defines the
contract: LB, Callio, and any future producer implement it, but the
semantic invariants are owned by the measurement system.


CONTRACT SUMMARY
================

Schema version:
    'lb-effective-config-v1'   (this module)

Stamped on each new Conversation, atomically inside the same
transaction that persists the conversation row:

    effective_config_hash_at_start        — sha256(canonical_json(FULL))
    behavior_os_managed_hash_at_start     — sha256(canonical_json(MANAGED_SUBSET))
    effective_config_schema_version       — 'lb-effective-config-v1'
    config_provenance_status              — 'OK' | 'HASH_FAILED'

If hashing raises for any reason, the conversation is still persisted
(customer messaging must not be blocked); the row gets both hashes
= null and status='HASH_FAILED'. Such rows are ineligible for clean
measurement cohorts.


THE CANONICAL SURFACE (v1)
==========================

The surface is the effective RUNTIME configuration for one (tenant,
serviceGroup) pair — NOT raw DB rows. Defaults, inheritance, and
overrides are resolved into a flat effective view first, then hashed.

    EffectiveBehaviorConfigV1
    ├── ai
    │   ├── global_prompt                       (string, nullable)
    │   ├── global_chat_instructions_json       (json)
    │   └── service_profile_ai_instructions     (dict[profile_id → json])
    ├── qualification
    │   └── service_profile_schemas             (dict[profile_id → json])
    ├── service_options
    │   └── by_profile                          (dict[profile_id → json])
    ├── pricing
    │   ├── by_profile                          (dict[profile_id → json])
    │   └── saved_account_overrides             (dict[account_id → json])
    ├── scheduling
    │   ├── availability_policy                 (json)
    │   ├── voice_availability_mode             (string)
    │   └── booking_strategy                    (json)
    ├── follow_up
    │   ├── mode_by_saved_account               (dict[account_id → string])
    │   ├── settings_by_saved_account           (dict[account_id → json])
    │   └── global_follow_up_policy             (json)
    ├── workflow_strategy
    │   ├── per_service_mode                    (dict[profile_id → string])
    │   └── saved_account_service_overrides     (dict[account_id → json])
    └── behavior_os_managed_rules
        └── by_profile                          (dict[profile_id → list[rule]])

EXCLUDED (do NOT contribute to either hash):
- UI/cosmetic fields (colors, labels, display names of profiles)
- Billing / subscription metadata that does not gate runtime behavior
- OAuth tokens, connection secrets, provider credentials
- Timestamps (createdAt/updatedAt) — these are metadata, not behavior
- Purely-diagnostic counters (webhook_stats, monitoring health rows)
- Free-form notes fields (adminNotes, internalMemo)


THE MANAGED SUBSET
==================

`behavior_os_managed_hash_at_start` is computed from the
`behavior_os_managed_rules` subtree ONLY. This lets BehaviorOS
distinguish:

    full_hash unchanged AND managed_hash unchanged
        → clean environment, treatment stable
    managed_hash changed
        → BehaviorOS-managed rule changed (treatment itself moved;
          new measurement cohort begins)
    managed_hash unchanged BUT full_hash changed
        → non-BehaviorOS config drift (environment contamination;
          exclude from clean primary cohort, track separately)


CANONICALIZATION
================

Both hashes are SHA-256 of a canonical JSON serialization defined by
these rules:

  - Object keys sorted lexicographically at every depth.
  - No whitespace between tokens (separators=(',', ':') in JSON).
  - Nested arrays preserve order (arrays are semantic, not sets).
  - null values are included (do NOT omit null keys — presence of the
    key matters).
  - Strings encoded as UTF-8; no unicode escaping unless required by
    JSON spec.
  - Numbers preserved as-is; do NOT normalize integer vs float
    (LB and BehaviorOS must agree on this at ingestion time).

Producer + consumer test vectors (below) MUST agree byte-for-byte.


VERSIONING
==========

The schema_version string is the on-disk contract identifier. NEVER
edit v1 semantics in place. If the surface needs a new field or a
removed field:

  1. Define 'lb-effective-config-v2' in a NEW module or as a NEW
     constant here.
  2. LB stamps v2 on new conversations after cutover.
  3. Historical conversations retain their v1 hashes.
  4. Measurements pointing at v1 continue to score against v1 hashes.

BehaviorOS's evaluator dispatches on `effective_config_schema_version`,
so mixing versions in one tenant's timeline is safe but the evaluator
should refuse to cross-compare v1 and v2 hashes for the same
measurement's cohort.
"""

from __future__ import annotations

# ------------------------------------------------------------------
# The single source of truth for the contract version. LB stamps this
# string; BehaviorOS's evaluator dispatches on it.
# ------------------------------------------------------------------
EFFECTIVE_CONFIG_SCHEMA_VERSION = 'lb-effective-config-v1'


# ------------------------------------------------------------------
# Provenance status tokens persisted on the Conversation row.
# ------------------------------------------------------------------
class ProvenanceStatus:
    OK = 'OK'                 # both hashes computed + stored
    HASH_FAILED = 'HASH_FAILED'   # resolution/hashing raised; hashes are null
    PENDING = 'PENDING'       # row created before v1 rollout / migration backfill


# ------------------------------------------------------------------
# The canonical surface, expressed as a nested field spec. Purely
# declarative — used for shape validation and for producer/consumer
# audits. Producer implementations (LB TS resolver) are expected to
# emit exactly these top-level keys with the described substructure.
#
# NOT_STAMPED entries below are the EXPLICITLY EXCLUDED surface — they
# are listed here so a future engineer adding new config fields can
# quickly decide whether the field is behavior-affecting (must be
# added to v1's next iteration → v2) or exempt (add to NOT_STAMPED).
# ------------------------------------------------------------------

SURFACE_V1_TOPLEVEL_KEYS = (
    'ai',
    'qualification',
    'service_options',
    'pricing',
    'scheduling',
    'follow_up',
    'workflow_strategy',
    'behavior_os_managed_rules',
)


# Path inside the surface whose contents form the "managed subset."
# Used by producers to derive the second hash without duplicating logic.
MANAGED_SUBSET_KEY = 'behavior_os_managed_rules'


NOT_STAMPED_V1 = (
    # UI-only
    'display_name', 'color', 'icon', 'sort_order', 'is_hidden_in_ui',
    # Billing/subscription — LB gates access, not runtime behavior
    'subscription_tier', 'billing_status', 'trial_ends_at',
    # Credentials & tokens
    'oauth_access_token', 'oauth_refresh_token', 'client_secret',
    'webhook_signing_secret', 'api_key',
    # Timestamps
    'created_at', 'updated_at', 'last_seen_at',
    # Diagnostics/monitoring
    'webhook_stats', 'monitoring_health_json', 'debug_flags_json',
    # Free-form operator notes
    'admin_notes', 'internal_memo',
)


# ------------------------------------------------------------------
# Test vectors — the STRUCTURED payload is the pinned reference. Any
# producer (LB TS resolver, later Callio, etc.) MUST hash these
# vectors to identical hex strings as `compute_hashes(vector)` below.
# The vectors themselves are the on-disk contract; the hashes are
# derived by rerunning compute_hashes at test time so we catch
# canonicalizer drift the moment it happens.
#
# Vector A: minimal payload (all top-level keys present but empty
# dicts / nulls). Locks the canonicalization of an "empty tenant."
#
# Vector B: representative payload with one BehaviorOS-managed rule.
# Locks (a) key-order normalization, (b) null-valued keys are
# preserved, (c) MANAGED_SUBSET_KEY isolates correctly.
# ------------------------------------------------------------------

VECTOR_A_MINIMAL = {
    'ai': {
        'global_prompt': None,
        'global_chat_instructions_json': {},
        'service_profile_ai_instructions': {},
    },
    'qualification': {'service_profile_schemas': {}},
    'service_options': {'by_profile': {}},
    'pricing': {'by_profile': {}, 'saved_account_overrides': {}},
    'scheduling': {
        'availability_policy': {},
        'voice_availability_mode': '',
        'booking_strategy': {},
    },
    'follow_up': {
        'mode_by_saved_account': {},
        'settings_by_saved_account': {},
        'global_follow_up_policy': {},
    },
    'workflow_strategy': {
        'per_service_mode': {},
        'saved_account_service_overrides': {},
    },
    'behavior_os_managed_rules': {'by_profile': {}},
}

VECTOR_B_ONE_MANAGED_RULE = {
    'ai': {
        'global_prompt': 'Answer politely.',
        'global_chat_instructions_json': {'tone': 'warm'},
        'service_profile_ai_instructions': {
            'prof_a': {'style': 'concise'},
        },
    },
    'qualification': {
        'service_profile_schemas': {
            'prof_a': {'fields': ['address', 'square_footage']},
        },
    },
    'service_options': {'by_profile': {'prof_a': {'trial': True}}},
    'pricing': {
        'by_profile': {'prof_a': {'base_hourly_cents': 8500}},
        'saved_account_overrides': {},
    },
    'scheduling': {
        'availability_policy': {'lead_hours_min': 24},
        'voice_availability_mode': 'dynamic',
        'booking_strategy': {'require_deposit': False},
    },
    'follow_up': {
        'mode_by_saved_account': {'sa_1': 'auto_send'},
        'settings_by_saved_account': {},
        'global_follow_up_policy': {'max_attempts': 3},
    },
    'workflow_strategy': {
        'per_service_mode': {'prof_a': 'ai_first'},
        'saved_account_service_overrides': {},
    },
    'behavior_os_managed_rules': {
        'by_profile': {
            'prof_a': [{
                'recommendation_id': 'R0002',
                'condition': 'DISCOUNT_REQUESTED',
                'summary': 'When asked for discount, share our special offer.',
            }],
        },
    },
}


def canonical_json_bytes(obj: object) -> bytes:
    """Reference canonicalization used by BehaviorOS-side verification.

    Producers (LB TS) MUST produce byte-identical output. Any deviation
    (extra whitespace, unsorted keys, integer vs float coercion) breaks
    hash agreement and silently invalidates measurements.
    """
    import json
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
    ).encode('utf-8')


def sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def compute_hashes(effective_config_payload: dict) -> tuple[str, str]:
    """Reference computation. Returns (full_hash, managed_hash).

    Producers do not call this — they implement their own canonicalizer
    in their native language. Consumers use it during verification /
    test-vector audits.
    """
    full = sha256_hex(canonical_json_bytes(effective_config_payload))
    managed_subset = effective_config_payload.get(MANAGED_SUBSET_KEY, {})
    managed = sha256_hex(canonical_json_bytes(managed_subset))
    return full, managed


def validate_shape(payload: dict) -> None:
    """Raise ValueError if `payload` violates the v1 top-level shape.

    Does NOT recurse — only checks the required top-level keys are
    present. Deep validation lives on the producer side (LB); if the
    producer sends a malformed payload, BehaviorOS logs + refuses to
    use the row for measurement cohorts.
    """
    missing = [k for k in SURFACE_V1_TOPLEVEL_KEYS if k not in payload]
    if missing:
        raise ValueError(
            f'EffectiveBehaviorConfigV1 payload missing top-level '
            f'keys: {missing}. Required: {list(SURFACE_V1_TOPLEVEL_KEYS)}'
        )
    unknown = [k for k in payload if k not in SURFACE_V1_TOPLEVEL_KEYS]
    if unknown:
        raise ValueError(
            f'EffectiveBehaviorConfigV1 payload has unexpected '
            f'top-level keys: {unknown}. Either add to SURFACE_V1 or '
            f'exclude them.'
        )
