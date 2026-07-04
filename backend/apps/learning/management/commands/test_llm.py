"""Fire one real LLM call end-to-end. No DB writes.

Use this to verify Anthropic connectivity, model IDs, prompt caching,
and cost math before running the first real nightly synthesis. It
exercises the exact code path (LearningLLMClient + AnthropicProvider)
that production analysis and synthesis use.

Usage:
    # Analyzer path (default): mimics a per-conversation call
    python manage.py test_llm

    # Synthesis path: mimics a cluster synthesis call
    python manage.py test_llm --kind synthesis

    # Different model
    python manage.py test_llm --model claude-sonnet-4-6

    # Show the full raw response
    python manage.py test_llm --verbose

Exits non-zero on any error so it's safe in a smoke-test script.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.learning.analyzers.prompts import (
    CONVERSATION_SYSTEM_PROMPT,
    SYNTHESIS_SYSTEM_PROMPT,
)
from apps.learning.services.llm_client import (
    ANTHROPIC_AVAILABLE,
    LearningLLMClient,
    LLMProviderError,
)


_SAMPLE_CONVERSATION_USER = """Source system: leadbridge
Channel: thumbtack
Business-rules version at time of conversation: v3.2
Lead: {"name": "Sarah", "location": "Chicago, IL", "service": "weekly_house_cleaning"}
Business outcome: {"status": "booked", "revenue_estimate": 145}

--- Transcript ---
customer: Hi, I'm looking for weekly house cleaning. Do you handle homes with pets?
ai: Yes! We bring all our own supplies and are pet-friendly. What's the size of your home?
customer: 3 bedroom, 2 bath. Two dogs.
ai: Perfect — for a 3/2 with pets on a weekly cadence, we're looking at around $135–$160 per visit. Want me to hold a slot for Tuesday morning?
customer: Yes please, Tuesday works
"""


_SAMPLE_CLUSTER_USER = """Category: pricing
Cluster size: 3 candidates
Outcome distribution: negative=2, positive=1

--- Candidate recommendations ---
- [playbook_rule] Explain pricing tiers before quoting a range
  When a customer asks about price, walk through the tier structure so the quote makes sense in context.
  (source=leadbridge, outcome_signal=negative, llm_confidence=0.72)
- [playbook_rule] Anchor pricing to the service scope
  Tie the number to what's included (supplies, pets, deep vs standard) so the customer isn't comparing raw dollars.
  (source=leadbridge, outcome_signal=negative, llm_confidence=0.81)
- [playbook_rule] Offer a tier menu when the customer asks "how much"
  Give three tiers (basic / standard / premium) instead of a single number when they open with a price question.
  (source=callio, outcome_signal=positive, llm_confidence=0.68)
"""


class Command(BaseCommand):
    help = 'Fire one real LLM call end-to-end and print the response + token/cost breakdown.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--kind',
            choices=['analysis', 'synthesis'],
            default='analysis',
            help='Which prompt shape to use. analysis = per-conversation. synthesis = cluster consolidation.',
        )
        parser.add_argument(
            '--model',
            help='Override the model. Defaults to LEARNING_ANALYSIS_MODEL / LEARNING_SYNTHESIS_MODEL '
                 'depending on --kind.',
        )
        parser.add_argument(
            '--verbose', action='store_true',
            help='Print the raw model output (not just the parsed JSON).',
        )

    def handle(self, *args, **options):
        kind = options['kind']
        if options.get('model'):
            model = options['model']
        elif kind == 'synthesis':
            model = settings.LEARNING_SYNTHESIS_MODEL
        else:
            model = settings.LEARNING_ANALYSIS_MODEL

        if not getattr(settings, 'ANTHROPIC_API_KEY', ''):
            self.stdout.write(self.style.WARNING(
                'ANTHROPIC_API_KEY is not set — this call will hit the StubProvider, '
                'not the real Anthropic API. Set the env var to exercise the real path.'
            ))
        if not ANTHROPIC_AVAILABLE:
            self.stdout.write(self.style.WARNING(
                'anthropic SDK is not installed — falling back to StubProvider. '
                'Run `pip install anthropic` first.'
            ))

        if kind == 'synthesis':
            system_prompt = SYNTHESIS_SYSTEM_PROMPT
            user_prompt = _SAMPLE_CLUSTER_USER
        else:
            system_prompt = CONVERSATION_SYSTEM_PROMPT
            user_prompt = _SAMPLE_CONVERSATION_USER

        self.stdout.write(f'kind      = {kind}')
        self.stdout.write(f'model     = {model}')

        client = LearningLLMClient()
        try:
            result = client.analyze(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
            )
        except LLMProviderError as exc:
            raise CommandError(str(exc))

        self.stdout.write(f'provider  = {result.provider}')
        self.stdout.write(
            f'tokens    = input={result.input_tokens} output={result.output_tokens} '
            f'cache_read={result.cache_read_tokens} cache_write={result.cache_write_tokens}'
        )
        self.stdout.write(self.style.SUCCESS(f'cost_usd  = {result.cost_usd}'))
        self.stdout.write('parsed    = ' + json.dumps(
            result.parsed_json, ensure_ascii=False, indent=2
        ))
        if options.get('verbose'):
            self.stdout.write('raw       =')
            self.stdout.write(result.raw_response)
