"""Parse the LB TenantConfigSnapshot's pricing content into
ConfiguredBusinessFact rows keyed by canonical subject_key.

LB stores pricing in tenant-editable JSON on ServiceProfile.pricingJson
(and per-account overrides on SavedAccount.servicePricingJson). Shape
varies per tenant, so an LLM normalizes each ServiceProfile's payload
into the shared subject_key + value schema. Deterministic diff logic
lives elsewhere — this parser only structures the raw config.

Never invents fields. Anything the LB JSON doesn't specify is left
null (partial key). Retains a source_pointer per fact so the audit
can point back at the exact ServiceProfile.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

from django.utils import timezone

from apps.conversations.models import (
    ConfiguredBusinessFact, ConfiguredFactParserRun,
    ObservedBusinessFact, TenantConfigSnapshot,
)
from apps.conversations.observed_config.base import (
    canonical_subject_key,
)
from apps.conversations.observed_config.pricing.aggregator import (
    _normalize_subject_key,
)

logger = logging.getLogger(__name__)


PRICING_CONFIG_PARSER_VERSION = 'observed-config-pricing-parser-v1'


SYSTEM_PROMPT = '''You normalize a residential-service business's
manually-configured pricing table into a canonical fact schema.

You receive:
- The ServiceProfile's name / slug / serviceGroup
- The raw pricingJson content (tenant-authored — shape varies)

Emit one fact per distinct configured price entry. Do NOT invent
attributes: if the raw pricingJson does not specify a dimension
(bedrooms, bathrooms, etc.), leave it null in subject_key. Preserve
the JSON path in source_pointer so the audit can trace back.

Output schema:

  {
    "facts": [
      {
        "fact_type": "quoted_price" | "price_range",
        "subject_key": {
          "service": <string or null>,
          "bedrooms": <integer or null>,
          "bathrooms": <integer or null>,
          "square_footage_bucket": <"<1000"|"1000-2000"|"2000-3000"|">3000" or null>,
          "frequency": <"one-time"|"weekly"|"biweekly"|"monthly" or null>,
          "addons": [<string>...] or null
        },
        "value": {
          "amount": <number or null>,
          "min_amount": <number or null>,
          "max_amount": <number or null>,
          "currency": "USD"
        },
        "source_pointer": {
          "service_profile_id": "<given>",
          "json_path": "<dotted path into the raw JSON>"
        }
      }
    ]
  }

Return only the JSON object. If pricingJson is empty or unparseable,
return {"facts": []}.
'''


def parse_snapshot(
    *,
    snapshot: TenantConfigSnapshot,
    llm_client,
    model: str = 'gpt-4o-mini',
) -> ConfiguredFactParserRun:
    """Parse the LB pricing content out of `snapshot.raw_config` into
    ConfiguredBusinessFact rows."""
    run = ConfiguredFactParserRun.objects.create(
        snapshot=snapshot,
        domain=ObservedBusinessFact.Domain.PRICING,
        parser_version=PRICING_CONFIG_PARSER_VERSION,
        model=model,
        status=ConfiguredFactParserRun.Status.RUNNING,
        started_at=timezone.now(),
    )
    logger.info(
        f'pricing-parser: run_id={run.id} started snapshot={snapshot.id}'
    )

    raw = snapshot.raw_config or {}
    service_profiles = (raw.get('service_profiles') or [])

    total_in = 0
    total_out = 0
    total_cost = Decimal('0')
    facts_emitted = 0

    for profile in service_profiles:
        pricing_json = profile.get('pricing_json')
        if not pricing_json:
            continue
        # Skip stub / trivially-empty pricing entries.
        if isinstance(pricing_json, dict) and not pricing_json:
            continue
        payload = {
            'service_profile': {
                'id': profile.get('id'),
                'name': profile.get('name'),
                'slug': profile.get('slug'),
                'service_group': profile.get('service_group'),
            },
            'pricing_json': pricing_json,
        }
        user = (
            'Normalize this ServiceProfile\'s pricingJson into the '
            'schema in the system prompt.\n\n'
            f'{json.dumps(payload, indent=2, default=str)[:12000]}'
        )
        try:
            r = llm_client.analyze(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user,
                model=model,
                max_tokens=2000,
            )
        except Exception as exc:
            logger.exception(
                'pricing-parser: profile=%s failed: %s',
                profile.get('id'), exc,
            )
            continue
        total_in += getattr(r, 'input_tokens', 0)
        total_out += getattr(r, 'output_tokens', 0)
        total_cost += getattr(r, 'cost_usd', Decimal('0'))
        parsed = r.parsed_json or {}
        facts = (parsed.get('facts') or []) if isinstance(parsed, dict) else []
        for entry in facts:
            if not isinstance(entry, dict):
                continue
            fact_type = entry.get('fact_type')
            if fact_type not in ('quoted_price', 'price_range'):
                continue
            subject_key = _normalize_subject_key(
                entry.get('subject_key') or {}
            )
            _, sha, dims = canonical_subject_key(subject_key)
            value_json = entry.get('value') or {}
            source_pointer = entry.get('source_pointer') or {}
            source_pointer.setdefault(
                'service_profile_id', profile.get('id'),
            )
            ConfiguredBusinessFact.objects.update_or_create(
                parser_run=run,
                domain=ObservedBusinessFact.Domain.PRICING,
                fact_type=fact_type,
                subject_key_hash=sha,
                defaults={
                    'snapshot': snapshot,
                    'subject_key_json': subject_key,
                    'subject_key_dimensions': dims,
                    'value_json': value_json,
                    'source_pointer': source_pointer,
                    'parser_confidence': 1.0,
                },
            )
            facts_emitted += 1

    run.status = ConfiguredFactParserRun.Status.COMPLETED
    run.completed_at = timezone.now()
    run.facts_emitted = facts_emitted
    run.llm_input_tokens = total_in
    run.llm_output_tokens = total_out
    run.llm_cost_usd = total_cost
    run.save()
    logger.info(
        f'pricing-parser: run_id={run.id} completed; '
        f'facts={facts_emitted} cost=${total_cost}'
    )
    return run
