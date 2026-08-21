"""LB service-scope config parser (Pipeline 1D Ship D).

Reads ServiceProfile.service_options_json + service_profile prose
(ai_instructions_json + faq_json + user.global_ai_prompt), LLM
normalizes into ConfiguredBusinessFact rows using the same
(service, scope_item, relationship) schema as agent_scope_statement
observed facts — so the deterministic diff can join them.
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
from apps.conversations.observed_config.service_scope.aggregator import (
    _normalize_subject_key,
)
from apps.conversations.observed_config.service_scope.prompt import (
    SCOPE_ITEMS, SCOPE_RELATIONSHIPS,
)

logger = logging.getLogger(__name__)


SERVICE_SCOPE_PARSER_VERSION = (
    'observed-config-service-scope-parser-v1'
)


SYSTEM_PROMPT = f'''You normalize a residential-service business's
configured service scope into a canonical fact schema. The same
schema is used to normalize agent scope statements observed in real
conversations, so BehaviorOS can compare what the business's
configuration DECLARES against what agents actually say.

You receive one of:
  - A ServiceProfile's service_options_json (structured)
  - A ServiceProfile's ai_instructions_json / faq_json (prose)
  - The tenant's global_ai_prompt (prose)

Emit ONE fact per (service, scope_item, relationship) configured
entry.

## Output schema

  {{
    "facts": [
      {{
        "fact_type": "configured_scope",
        "service": <string or null>,
        "scope_item": <one of the taxonomy tokens>,
        "other_topic": <snake_case when scope_item="other", else omit>,
        "relationship": <one of the relationship values below>,
        "context": {{
          "frequency": <"one-time"|"weekly"|"biweekly"|"monthly" or null>,
          "condition": <"first_cleaning"|"recurring_maintenance"|"heavy_condition" or null>
        }},
        "source_pointer": {{
          "source": "service_profile.service_options_json" |
                    "service_profile.ai_instructions_json" |
                    "service_profile.faq_json" |
                    "user.global_ai_prompt",
          "service_profile_id": <string or null>,
          "json_path": <string>
        }}
      }}
    ]
  }}

## Scope-item taxonomy (exact tokens)

{sorted(SCOPE_ITEMS)}

Use `scope_item="other"` + `other_topic="<snake_case>"` for
anything outside the taxonomy.

## Relationship values

- INCLUDED             — part of the standard service, no extra charge
- EXCLUDED             — the business does not do this
- OPTIONAL_ADDON       — available on request, no explicit price
- EXTRA_CHARGE         — available for an additional fee
- CONDITION_DEPENDENT  — done only under specific conditions

## Hard rules

- ONLY emit configured_scope facts where the source clearly states
  a scope relationship. "We make your home sparkle" is marketing
  copy; skip it.
- Do NOT emit for entries that are just pricing without a scope
  claim ("$249 for deep clean" is pricing, not scope).
- Do NOT emit for entries that are just qualification questions
  ("we ask for square footage before quoting").
- `source_pointer.json_path` MUST identify the raw config location.

Return only the JSON object. Empty source → {{"facts": []}}.
'''


def parse_snapshot(
    *,
    snapshot: TenantConfigSnapshot,
    llm_client,
    model: str = 'gpt-4o-mini',
) -> ConfiguredFactParserRun:
    run = ConfiguredFactParserRun.objects.create(
        snapshot=snapshot,
        domain=ObservedBusinessFact.Domain.SERVICE_SCOPE,
        parser_version=SERVICE_SCOPE_PARSER_VERSION,
        model=model,
        status=ConfiguredFactParserRun.Status.RUNNING,
        started_at=timezone.now(),
    )
    logger.info(
        f'service-scope-parser: run_id={run.id} started '
        f'snapshot={snapshot.id}'
    )
    raw = snapshot.raw_config or {}
    user_block = raw.get('user') or {}
    global_ai_prompt = (user_block.get('global_ai_prompt') or '').strip()
    global_chat_json = user_block.get('global_ai_chat_instructions_json')
    service_profiles = raw.get('service_profiles') or []

    passes: list[dict] = []
    if global_ai_prompt or global_chat_json:
        passes.append({
            'source_kind': 'global_prompt',
            'payload': {
                'global_ai_prompt': global_ai_prompt or None,
                'global_chat_instructions_json': global_chat_json,
            },
            'source_pointer_default': {
                'source': 'user.global_ai_prompt',
                'service_profile_id': None,
            },
        })
    for profile in service_profiles:
        payload: dict = {
            'service_profile': {
                'id': profile.get('id'),
                'name': profile.get('name'),
                'slug': profile.get('slug'),
                'service_group': profile.get('service_group'),
            },
        }
        has_content = False
        for f in ('service_options_json', 'ai_instructions_json',
                   'faq_json'):
            v = profile.get(f)
            if v is not None and v != {} and v != []:
                payload[f] = v
                has_content = True
        if not has_content:
            continue
        passes.append({
            'source_kind': 'service_profile',
            'payload': payload,
            'source_pointer_default': {
                'source': 'service_profile.service_options_json',
                'service_profile_id': profile.get('id'),
            },
        })

    total_in = 0
    total_out = 0
    total_cost = Decimal('0')
    facts_emitted = 0

    for p in passes:
        user_msg = (
            f'Source: {p["source_kind"]}\n\n'
            'Normalize configured scope per the system prompt.\n\n'
            f'{json.dumps(p["payload"], indent=2, default=str)[:12000]}'
        )
        try:
            r = llm_client.analyze(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_msg,
                model=model,
                max_tokens=2500,
            )
        except Exception as exc:
            logger.exception(
                'service-scope-parser: pass=%s failed: %s',
                p['source_kind'], exc,
            )
            continue
        total_in += getattr(r, 'input_tokens', 0)
        total_out += getattr(r, 'output_tokens', 0)
        total_cost += getattr(r, 'cost_usd', Decimal('0'))
        parsed = r.parsed_json or {}
        facts = (
            (parsed.get('facts') or [])
            if isinstance(parsed, dict) else []
        )
        for entry in facts:
            if not isinstance(entry, dict):
                continue
            if entry.get('fact_type') != 'configured_scope':
                continue
            rel = entry.get('relationship')
            if rel not in SCOPE_RELATIONSHIPS:
                continue
            subject_key = _normalize_subject_key(entry)
            if 'scope_item' not in subject_key:
                continue
            _canon, sha, dims = canonical_subject_key(subject_key)
            source_pointer = dict(p['source_pointer_default'])
            entry_pointer = entry.get('source_pointer') or {}
            if isinstance(entry_pointer, dict):
                source_pointer.update(entry_pointer)
            ctx = entry.get('context') or {}
            clean_ctx = {
                k: (str(v).strip().lower() if v else None)
                for k, v in ctx.items() if k in ('frequency', 'condition')
            }
            value_json = {
                'relationship': rel,
                'context': {k: v for k, v in clean_ctx.items() if v},
            }
            try:
                ConfiguredBusinessFact.objects.update_or_create(
                    parser_run=run,
                    domain=ObservedBusinessFact.Domain.SERVICE_SCOPE,
                    fact_type='configured_scope',
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
            except Exception as exc:
                logger.warning(
                    f'service-scope-parser: persist skipped '
                    f'sha={sha[:12]} err={exc}'
                )

    run.status = ConfiguredFactParserRun.Status.COMPLETED
    run.completed_at = timezone.now()
    run.facts_emitted = facts_emitted
    run.llm_input_tokens = total_in
    run.llm_output_tokens = total_out
    run.llm_cost_usd = total_cost
    run.save()
    logger.info(
        f'service-scope-parser: run_id={run.id} completed; '
        f'facts={facts_emitted} cost=${total_cost}'
    )
    return run
