"""Parse LB TenantConfigSnapshot's qualification content into
ConfiguredBusinessFact rows (Pipeline 1D Ship B).

Qualification schema in LB lives at:
  - ServiceProfile.qualification_schema_json (structured)
  - User.global_ai_prompt (may reference "always ask X")
  - ServiceProfile.ai_instructions_json (may include collection rules)

Shapes vary per tenant, so an LLM normalizes each source into the
same {field, service_context?, required, kind} schema used by the
observed side.

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
from apps.conversations.observed_config.qualification.aggregator import (
    _normalize_subject_key,
)
from apps.conversations.observed_config.qualification.prompt import (
    QUALIFICATION_FIELDS,
)

logger = logging.getLogger(__name__)


QUALIFICATION_CONFIG_PARSER_VERSION = (
    'observed-config-qualification-parser-v1'
)


SYSTEM_PROMPT = f'''You normalize a residential-service business's
manually-configured qualification schema into a canonical fact schema
so it can be compared to how the business actually collects
information in real conversations.

You receive one of two sources (indicated in the user message):
- A ServiceProfile's structured qualification_schema_json blob and/or
  ai_instructions_json, OR
- The tenant's global_ai_prompt (prose that may state "always ask X").

Emit ONE fact per distinct configured qualification field. Do NOT
invent fields.

## Field taxonomy

Use these EXACT tokens (matches the observed-side taxonomy):

{sorted(QUALIFICATION_FIELDS)}

If a configured field does not match, use `"field": "other"` with an
`"other_topic": "<snake_case label>"`.

## Output schema

  {{
    "facts": [
      {{
        "fact_type": "configured_question",
        "field": <one of taxonomy tokens>,
        "other_topic": <string when field=="other", else omit>,
        "service_context": <string or null>,
        "required": <bool>,
        "collection_kind": "structured_field" | "prose_rule" | "conditional",
        "source_pointer": {{
          "source": "service_profile.qualification_schema_json" | "user.global_ai_prompt" | "service_profile.ai_instructions_json",
          "service_profile_id": <string or null>,
          "json_path": <string>
        }}
      }}
    ]
  }}

## Rules

- Only emit a fact if the source explicitly requests the field.
  "The agent should be friendly" does NOT emit a `service_type` fact.
- `required=true` only when the schema explicitly marks the field
  required OR the prose says "always ask" / "must confirm before
  quoting". Ambiguous ask → `required=false`.
- `collection_kind`:
    "structured_field" — from qualification_schema_json entries
    "prose_rule"       — from a global_ai_prompt sentence
    "conditional"      — the rule is conditional on service or state
                         ("For deep cleans, ask about condition")
- `service_context` filled ONLY when the config explicitly scopes
  the field to a specific service.
- No pricing, no FAQ, no service scope in this parser — only the
  set of information fields the business intends to collect from
  customers.

Return only the JSON object. Empty source → {{"facts": []}}.
'''


def parse_snapshot(
    *,
    snapshot: TenantConfigSnapshot,
    llm_client,
    model: str = 'gpt-4o-mini',
) -> ConfiguredFactParserRun:
    """Parse qualification schema from `snapshot.raw_config` into
    ConfiguredBusinessFact rows. Runs one LLM pass over the global
    prompt/ai_instructions and one pass per ServiceProfile that has
    a qualification_schema_json."""
    run = ConfiguredFactParserRun.objects.create(
        snapshot=snapshot,
        domain=ObservedBusinessFact.Domain.QUALIFICATION,
        parser_version=QUALIFICATION_CONFIG_PARSER_VERSION,
        model=model,
        status=ConfiguredFactParserRun.Status.RUNNING,
        started_at=timezone.now(),
    )
    logger.info(
        f'qualification-parser: run_id={run.id} started '
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
        for f in ('qualification_schema_json', 'ai_instructions_json'):
            v = profile.get(f)
            if v is not None:
                payload[f] = v
                has_content = True
        if not has_content:
            continue
        passes.append({
            'source_kind': 'service_profile',
            'payload': payload,
            'source_pointer_default': {
                'source': 'service_profile.qualification_schema_json',
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
            'Extract qualification facts per the system prompt.\n\n'
            f'{json.dumps(p["payload"], indent=2, default=str)[:12000]}'
        )
        try:
            r = llm_client.analyze(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_msg,
                model=model,
                max_tokens=2000,
            )
        except Exception as exc:
            logger.exception(
                'qualification-parser: pass=%s failed: %s',
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
            if entry.get('fact_type') != 'configured_question':
                continue
            field_v = entry.get('field')
            if not field_v:
                continue
            # Normalize the subject_key using the same helper the
            # observed side uses, so hashes match byte-for-byte.
            subject_key = _normalize_subject_key(entry)
            if 'field' not in subject_key:
                continue
            _canon, sha, dims = canonical_subject_key(subject_key)
            source_pointer = dict(p['source_pointer_default'])
            entry_pointer = entry.get('source_pointer') or {}
            if isinstance(entry_pointer, dict):
                source_pointer.update(entry_pointer)
            value_json = {
                'required': bool(entry.get('required', False)),
                'collection_kind': (
                    entry.get('collection_kind') or 'structured_field'
                ),
            }
            try:
                ConfiguredBusinessFact.objects.update_or_create(
                    parser_run=run,
                    domain=ObservedBusinessFact.Domain.QUALIFICATION,
                    fact_type='configured_question',
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
                    f'qualification-parser: persist skipped '
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
        f'qualification-parser: run_id={run.id} completed; '
        f'facts={facts_emitted} cost=${total_cost}'
    )
    return run
