"""Deterministic configured-pricing parser for the standard LB
ServiceProfile.pricingJson shape.

Emits one `ConfiguredBusinessFact` row per (priceTable row × enabled
cleaningType × frequency) plus separate facts for extras (addons) and
the hourly-rate fallback.

Runs BEFORE the LLM-based parser in `config_parser.py`. When the
deterministic parser recognizes the shape (i.e. finds a `priceTable`
or `cleaningTypes` + `hourlyRate` + `extras`), it OWNS the parse for
that service profile and the LLM path is skipped for it. Unrecognized
shapes fall through to the LLM parser, so tenants who keep pricing in
prose still get facts.

Contract for the matcher:
  subject_key uses `sqft_min` / `sqft_max` (numeric interval) rather
  than a bucket label. `bedrooms` and `bathrooms` are integers.
  `frequency` uses LB keys (`once`, `weekly`, `biweekly`, `monthly`).
  `service_tier` is the LB `cleaningTypes[].key` (`regular` / `deep` /
  `airbnb` / etc). `pricing_basis` is `flat_job` for grid rows,
  `addon_flat` for extras, `hourly_per_cleaner` for hourly fallback.

Never invents data. If a field is absent on the LB row, the
corresponding key is omitted from subject_key (not stored as null).

Value payload for a grid row:
  {
    'currency': 'USD',
    'amount': <post-frequency-discount price for this cell>,
    'base_amount': <row[serviceType] before any discount>,
    'frequency_discount_pct': <0..100>,
    'sqft_scale_up': {
        'enabled': <bool>,
        'formula': 'when sqft > sqft_max: price = round5(sqft * base / midpoint(sqft_min, sqft_max)); floor = base',
    },
    'source_row_ref': {'row_index': <int>, 'service_tier': '<key>'},
  }
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

from django.utils import timezone

from apps.conversations.models import (
    ConfiguredBusinessFact, ConfiguredFactParserRun,
    ObservedBusinessFact, TenantConfigSnapshot,
)
from apps.conversations.observed_config.base import (
    canonical_subject_key,
)


logger = logging.getLogger(__name__)

DETERMINISTIC_PARSER_VERSION = 'observed-config-pricing-parser-deterministic-v1'

# LB frequency keys → BehaviorOS canonical frequency labels.
# BehaviorOS keeps LB's exact key strings so subject-key matching is
# lossless. `once` (LB) is stored verbatim; matcher treats it as the
# one-time counterpart of the observed extractor's `one-time`.
LB_FREQUENCY_KEYS = ('once', 'weekly', 'biweekly', 'monthly')


def parser_recognizes_shape(pricing_json: dict) -> bool:
    """True when the deterministic parser can handle this shape.

    Recognition rule: has EITHER a non-empty `priceTable` OR a
    non-empty `cleaningTypes` OR a positive `hourlyRate` OR a
    non-empty `extras`. Anything else (prose-only, custom schemas)
    is left for the LLM path.
    """
    if not isinstance(pricing_json, dict):
        return False
    price_table = pricing_json.get('priceTable') or []
    cleaning_types = pricing_json.get('cleaningTypes') or []
    hourly = pricing_json.get('hourlyRate')
    extras = pricing_json.get('extras') or []
    return (
        (isinstance(price_table, list) and len(price_table) > 0)
        or (isinstance(cleaning_types, list) and len(cleaning_types) > 0)
        or (isinstance(hourly, (int, float)) and hourly > 0)
        or (isinstance(extras, list) and len(extras) > 0)
    )


def parse_service_profile(
    *,
    run: ConfiguredFactParserRun,
    snapshot: TenantConfigSnapshot,
    service_profile: dict,
    pricing_json: dict,
) -> int:
    """Emit ConfiguredBusinessFact rows for one service profile.

    Returns the number of facts written. Idempotent per
    (parser_run, domain, fact_type, subject_key_hash) via
    update_or_create.
    """
    service_slug = _service_slug(service_profile)
    service_profile_id = service_profile.get('id')

    price_table = pricing_json.get('priceTable') or []
    cleaning_types = pricing_json.get('cleaningTypes') or []
    frequency_discounts = pricing_json.get('frequencyDiscounts') or []
    extras = pricing_json.get('extras') or []
    hourly_rate = pricing_json.get('hourlyRate')
    minimum_hours = pricing_json.get('minimumHours')
    minimum_charge = pricing_json.get('minimumCharge')
    sqft_scale_enabled = bool(pricing_json.get('sqftAdjustEnabled'))

    enabled_tiers = _enabled_tiers(cleaning_types)
    # Fallback for legacy tenants that populate priceTable but not
    # cleaningTypes: infer tiers from row keys.
    if not enabled_tiers and price_table:
        enabled_tiers = _infer_tiers_from_rows(price_table)
    freq_by_key = {
        (fd.get('key') or '').lower(): fd
        for fd in frequency_discounts
        if isinstance(fd, dict)
    }
    # Always emit a `once` variant even if LB doesn't declare it.
    if 'once' not in freq_by_key:
        freq_by_key['once'] = {'key': 'once', 'label': 'One Time', 'discount': 0}

    written = 0

    # Grid rows × tiers × frequencies.
    for row_index, row in enumerate(price_table):
        if not isinstance(row, dict):
            continue
        bed = _as_int(row.get('bed'))
        bath = _as_int(row.get('bath'))
        sqft_min = _as_int(row.get('sqftMin'))
        sqft_max = _as_int(row.get('sqftMax'))
        if bed is None or bath is None:
            continue
        for tier_key in enabled_tiers:
            base_amount = _as_float(row.get(tier_key))
            if base_amount is None or base_amount <= 0:
                continue
            for freq_key, fd in freq_by_key.items():
                discount_pct = _as_float(fd.get('discount')) or 0.0
                amount = _apply_frequency_discount(
                    base_amount, discount_pct,
                )
                subject = {
                    'service': service_slug,
                    'service_tier': tier_key,
                    'bedrooms': bed,
                    'bathrooms': bath,
                    'frequency': freq_key,
                    'pricing_basis': 'flat_job',
                }
                # Preserve raw sqft interval — matcher does
                # `observed_sqft ∈ [sqft_min, sqft_max]`. When LB
                # rows omit sqft bounds, the fact still emits but
                # the matcher can only match on bed/bath (which is
                # correct — the config itself is coarser).
                if sqft_min is not None:
                    subject['sqft_min'] = sqft_min
                if sqft_max is not None:
                    subject['sqft_max'] = sqft_max
                value = {
                    'currency': 'USD',
                    'amount': amount,
                    'base_amount': base_amount,
                    'frequency_discount_pct': discount_pct,
                    'sqft_scale_up': {
                        'enabled': sqft_scale_enabled,
                        'formula': (
                            'when sqft > sqft_max: '
                            'price = round5(sqft * base / midpoint(sqft_min, sqft_max)); '
                            'floor = base'
                        ),
                    },
                    'source_row_ref': {
                        'row_index': row_index,
                        'service_tier': tier_key,
                    },
                }
                source_pointer = {
                    'source': 'service_profiles[*].pricing_json.priceTable[]',
                    'service_profile_id': service_profile_id,
                    'row_index': row_index,
                    'field': tier_key,
                    'frequency_key': freq_key,
                }
                written += _upsert_fact(
                    run=run, snapshot=snapshot,
                    fact_type='quoted_price',
                    subject=subject, value=value,
                    source_pointer=source_pointer,
                )

    # Extras (addons) — one fact per priced addon.
    for extra in extras:
        if not isinstance(extra, dict):
            continue
        key = (extra.get('key') or '').strip().lower()
        price = _as_float(extra.get('price'))
        if not key or price is None or price <= 0:
            continue
        subject = {
            'service': service_slug,
            'addons': [key],
            'pricing_basis': 'addon_flat',
        }
        value = {
            'currency': 'USD',
            'amount': price,
            'addon_key': key,
            'addon_label': extra.get('label'),
        }
        source_pointer = {
            'source': 'service_profiles[*].pricing_json.extras[]',
            'service_profile_id': service_profile_id,
            'addon_key': key,
        }
        written += _upsert_fact(
            run=run, snapshot=snapshot,
            fact_type='quoted_price',
            subject=subject, value=value,
            source_pointer=source_pointer,
        )

    # Hourly-rate fallback — only emitted when LB configured a real
    # positive rate. Represents the "no bed/bath grid match" price
    # ceiling/floor the AI is expected to quote for hourly services.
    if isinstance(hourly_rate, (int, float)) and hourly_rate > 0:
        subject = {
            'service': service_slug,
            'pricing_basis': 'hourly_per_cleaner',
        }
        value: dict = {
            'currency': 'USD',
            'amount': float(hourly_rate),
        }
        if isinstance(minimum_hours, (int, float)) and minimum_hours > 0:
            value['minimum_hours'] = float(minimum_hours)
        if isinstance(minimum_charge, (int, float)) and minimum_charge > 0:
            value['minimum_charge'] = float(minimum_charge)
        source_pointer = {
            'source': 'service_profiles[*].pricing_json.hourlyRate',
            'service_profile_id': service_profile_id,
        }
        written += _upsert_fact(
            run=run, snapshot=snapshot,
            fact_type='quoted_price',
            subject=subject, value=value,
            source_pointer=source_pointer,
        )

    return written


# ─── helpers ────────────────────────────────────────────────────────

def _service_slug(service_profile: dict) -> str:
    """Prefer the LB slug; fall back to service_group; then name."""
    return (
        (service_profile.get('slug') or '').strip().lower()
        or (service_profile.get('service_group') or '').strip().lower()
        or (service_profile.get('name') or 'service').strip().lower().replace(' ', '_')
    )


def _enabled_tiers(cleaning_types: list) -> list[str]:
    """Return enabled tier keys in insertion order."""
    out: list[str] = []
    for ct in cleaning_types or []:
        if not isinstance(ct, dict):
            continue
        if ct.get('enabled') is False:
            continue
        key = (ct.get('key') or '').strip().lower()
        if key and key not in out:
            out.append(key)
    return out


def _infer_tiers_from_rows(price_table: list) -> list[str]:
    """When cleaningTypes is absent, discover tier keys from priceTable
    row values (any numeric-valued key other than bed/bath/sqft*)."""
    known_reserved = {'bed', 'bath', 'sqftmin', 'sqftmax', 'sqft_min', 'sqft_max'}
    tiers: list[str] = []
    for row in price_table[:5]:
        if not isinstance(row, dict):
            continue
        for k, v in row.items():
            if k.lower() in known_reserved:
                continue
            if isinstance(v, (int, float)) and v > 0 and k not in tiers:
                tiers.append(k)
    return tiers


def _apply_frequency_discount(base: float, discount_pct: float) -> float:
    """LB rounds to the nearest $5 on recurring discounts. Preserve
    that so a matcher tolerance of ±10% doesn't paper over a mismatch
    that's really a rounding artifact."""
    if discount_pct <= 0:
        return float(base)
    raw = base * (1.0 - (discount_pct / 100.0))
    return float(round(raw / 5.0) * 5.0)


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _upsert_fact(
    *, run, snapshot,
    fact_type: str,
    subject: dict,
    value: dict,
    source_pointer: dict,
) -> int:
    """Persist one ConfiguredBusinessFact. Returns 1 on success, 0 on
    persistence error (logged, not raised)."""
    canonical_subject = _normalize_subject(subject)
    _, sha, dims = canonical_subject_key(canonical_subject)
    try:
        ConfiguredBusinessFact.objects.update_or_create(
            parser_run=run,
            domain=ObservedBusinessFact.Domain.PRICING,
            fact_type=fact_type,
            subject_key_hash=sha,
            defaults={
                'snapshot': snapshot,
                'subject_key_json': canonical_subject,
                'subject_key_dimensions': dims,
                'value_json': value,
                'source_pointer': source_pointer,
                'parser_confidence': 1.0,
            },
        )
        return 1
    except Exception as exc:
        logger.warning(
            'deterministic pricing parser: persist skipped '
            'sha=%s subject=%s err=%s',
            sha[:12], json.dumps(canonical_subject, sort_keys=True), exc,
        )
        return 0


def _normalize_subject(subject: dict) -> dict:
    """Drop empty / None values; lowercase string values; sort addons."""
    out: dict = {}
    for k, v in subject.items():
        if v is None:
            continue
        if isinstance(v, str):
            s = v.strip().lower()
            if s:
                out[k] = s
        elif isinstance(v, list):
            if v:
                out[k] = sorted([str(x).strip().lower() for x in v])
        elif isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = v
        else:
            out[k] = v
    return out


def parse_all_profiles(
    *,
    run: ConfiguredFactParserRun,
    snapshot: TenantConfigSnapshot,
    service_profiles: Iterable[dict],
) -> tuple[int, set[str]]:
    """Run the deterministic parser across every service profile in
    the snapshot. Returns (total_facts_written, ids_handled).

    `ids_handled` = the set of service_profile_ids the deterministic
    parser CLAIMED. The LLM path in config_parser.py should skip
    these to avoid double-writing.
    """
    total = 0
    handled: set[str] = set()
    for profile in service_profiles or []:
        pricing_json = profile.get('pricing_json')
        if not isinstance(pricing_json, dict):
            continue
        if not parser_recognizes_shape(pricing_json):
            continue
        written = parse_service_profile(
            run=run, snapshot=snapshot,
            service_profile=profile, pricing_json=pricing_json,
        )
        if written > 0:
            handled.add(str(profile.get('id') or ''))
            total += written
    return total, handled
