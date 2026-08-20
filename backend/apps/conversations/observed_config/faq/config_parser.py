"""Parse LB TenantConfigSnapshot's FAQ content into
ConfiguredBusinessFact rows (Pipeline 1D Ship C).

Both sides go through the SAME semantic normalization prompt
vocabulary — configured FAQ entries land under the same
{topic, intent} taxonomy as observed customer questions so the
deterministic diff can join them.

Sources within a snapshot:
  - ServiceProfile.faq_json  (structured, common shapes: list of
    {question, answer} objects, or a keyed dict)
  - User.global_ai_prompt / global_chat_instructions_json (prose
    may include FAQ-style Q&A)
  - SavedAccount.faq_json (per-account overrides — currently rare)

Idempotent per (snapshot, parser_version).
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
from apps.conversations.observed_config.faq.aggregator import (
    _normalize_subject_key,
)
from apps.conversations.observed_config.faq.prompt import (
    FAQ_BUSINESS_TOPICS,
)

logger = logging.getLogger(__name__)


FAQ_CONFIG_PARSER_VERSION = 'observed-config-faq-parser-v1'


SYSTEM_PROMPT = f'''You normalize a residential-service business's
manually-configured FAQ content into a canonical {{topic, intent}}
schema. The same schema is used to normalize customer FAQ questions
observed in real conversations, so BehaviorOS can compare "what the
business intends to answer" against "what customers actually ask."

You receive one of:
  - A ServiceProfile's faq_json (structured Q&A list or keyed dict)
  - The tenant's global_ai_prompt / chat instructions (prose)
  - A SavedAccount's faq_json (per-account overrides)

Emit ONE configured FAQ fact per distinct Q&A entry.

## Output schema

  {{
    "faqs": [
      {{
        "fact_type": "configured_faq",
        "topic": <one of the taxonomy tokens below>,
        "other_topic": <snake_case when topic="other", else omit>,
        "intent": <snake_case string like "asks_who_provides_supplies">,
        "configured_answer": <exact verbatim answer text>,
        "source_pointer": {{
          "source": "service_profile.faq_json" | "user.global_ai_prompt" | "saved_account.faq_json",
          "service_profile_id": <string or null>,
          "json_path": <string>
        }}
      }}
    ]
  }}

## Business topic taxonomy (exact tokens)

{sorted(FAQ_BUSINESS_TOPICS)}

If a configured FAQ doesn't match, use `"topic": "other"` and add
`"other_topic": "<snake_case>"`.

## Intent guidance (verb-first, snake_case)

Same convention as the observed side. Examples:
  supplies:
    asks_who_provides_supplies
    asks_what_products_used
    asks_if_eco_friendly_products
  pets:
    asks_if_pets_are_ok
  cancellation:
    asks_cancellation_policy
    asks_cancellation_fee
  recurring_service:
    asks_recurring_discount
    asks_same_cleaner_recurring
  included_services:
    asks_what_included_in_cleaning
    asks_inside_appliances_included

Use these exact strings when they fit. Coin new snake_case intents
when the configured Q&A doesn't map to an existing one.

## Hard rules

- Skip anything that is NOT a Q&A entry. A configured price row is
  pricing, not FAQ. An agent instruction ("always confirm the
  address") is not FAQ.
- Skip TRANSACTIONAL/operational entries — payment status,
  verification templates, address confirmation. These live on the
  operational side, not the business-FAQ side.
- Every entry MUST have a `configured_answer` verbatim string. If
  the config has a question without an answer, skip it.
- `source_pointer.json_path` MUST identify where in the raw config
  the entry lives so the audit can trace back.

Return only the JSON object. Empty source → {{"faqs": []}}.
'''


def parse_snapshot(
    *,
    snapshot: TenantConfigSnapshot,
    llm_client,
    model: str = 'gpt-4o-mini',
) -> ConfiguredFactParserRun:
    run = ConfiguredFactParserRun.objects.create(
        snapshot=snapshot,
        domain=ObservedBusinessFact.Domain.FAQ,
        parser_version=FAQ_CONFIG_PARSER_VERSION,
        model=model,
        status=ConfiguredFactParserRun.Status.RUNNING,
        started_at=timezone.now(),
    )
    logger.info(
        f'faq-parser: run_id={run.id} started snapshot={snapshot.id}'
    )
    raw = snapshot.raw_config or {}
    user_block = raw.get('user') or {}
    global_ai_prompt = (user_block.get('global_ai_prompt') or '').strip()
    global_chat_json = user_block.get('global_ai_chat_instructions_json')
    service_profiles = raw.get('service_profiles') or []
    saved_accounts = raw.get('saved_accounts') or []

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
        v = profile.get('faq_json')
        if v is None:
            continue
        if isinstance(v, dict) and not v:
            continue
        if isinstance(v, list) and not v:
            continue
        passes.append({
            'source_kind': 'service_profile',
            'payload': {
                'service_profile': {
                    'id': profile.get('id'),
                    'name': profile.get('name'),
                    'slug': profile.get('slug'),
                    'service_group': profile.get('service_group'),
                },
                'faq_json': v,
            },
            'source_pointer_default': {
                'source': 'service_profile.faq_json',
                'service_profile_id': profile.get('id'),
            },
        })
    for sa in saved_accounts:
        v = sa.get('faq_json')
        if v is None:
            continue
        if isinstance(v, dict) and not v:
            continue
        if isinstance(v, list) and not v:
            continue
        passes.append({
            'source_kind': 'saved_account',
            'payload': {
                'saved_account': {
                    'id': sa.get('id'),
                    'platform': sa.get('platform'),
                },
                'faq_json': v,
            },
            'source_pointer_default': {
                'source': 'saved_account.faq_json',
                'service_profile_id': None,
            },
        })

    total_in = 0
    total_out = 0
    total_cost = Decimal('0')
    facts_emitted = 0

    for p in passes:
        user_msg = (
            f'Source: {p["source_kind"]}\n\n'
            'Normalize FAQ entries per the system prompt.\n\n'
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
                'faq-parser: pass=%s failed: %s', p['source_kind'], exc,
            )
            continue
        total_in += getattr(r, 'input_tokens', 0)
        total_out += getattr(r, 'output_tokens', 0)
        total_cost += getattr(r, 'cost_usd', Decimal('0'))
        parsed = r.parsed_json or {}
        facts = (
            (parsed.get('faqs') or [])
            if isinstance(parsed, dict) else []
        )
        for entry in facts:
            if not isinstance(entry, dict):
                continue
            if entry.get('fact_type') != 'configured_faq':
                continue
            if not entry.get('configured_answer'):
                continue
            subject_key = _normalize_subject_key(entry)
            if 'topic' not in subject_key or 'intent' not in subject_key:
                continue
            _canon, sha, dims = canonical_subject_key(subject_key)
            source_pointer = dict(p['source_pointer_default'])
            entry_pointer = entry.get('source_pointer') or {}
            if isinstance(entry_pointer, dict):
                source_pointer.update(entry_pointer)
            value_json = {
                'configured_answer': (
                    (entry.get('configured_answer') or '')[:2000]
                ),
            }
            try:
                ConfiguredBusinessFact.objects.update_or_create(
                    parser_run=run,
                    domain=ObservedBusinessFact.Domain.FAQ,
                    fact_type='configured_faq',
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
                    f'faq-parser: persist skipped '
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
        f'faq-parser: run_id={run.id} completed; '
        f'facts={facts_emitted} cost=${total_cost}'
    )
    return run
