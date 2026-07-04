"""Provider-agnostic LLM client for the learning engine.

Design goals:
- Analyzers hand in prompts + a model name; the client picks a provider
  based on the model name prefix (`claude-*` → Anthropic, `gpt-*` →
  OpenAI, else Stub).
- When no API key is configured OR the SDK isn't installed, the client
  falls back to `StubProvider` so Phase 1 can be developed and tested
  without spending tokens. Matches the adapter-fixture pattern.
- Cost is computed here, not in the analyzer. The client is the single
  source of truth for per-token pricing (from settings.LEARNING_MODEL_PRICING).
- Response shape (`LLMResult`) is stable across providers so budget +
  persistence code doesn't care which provider was used.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

try:
    from anthropic import Anthropic  # type: ignore
    ANTHROPIC_AVAILABLE = True
except ImportError:
    Anthropic = None  # type: ignore
    ANTHROPIC_AVAILABLE = False


@dataclass
class LLMResult:
    raw_response: str
    parsed_json: dict
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal
    model_used: str
    provider: str  # 'anthropic' | 'openai' | 'stub'


class LLMProviderError(RuntimeError):
    """Raised when a provider fails in a way analyzers should surface."""


class BaseProvider:
    name: str = 'base'

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
    ) -> LLMResult:
        raise NotImplementedError


class StubProvider(BaseProvider):
    """Deterministic canned response matching the schema.

    Used when no API key is configured. The returned analysis is a
    plausible but generic structure — enough to exercise downstream
    persistence, clustering, and dashboard code without spending money.
    """

    name = 'stub'

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
    ) -> LLMResult:
        # Detect which caller is asking based on the system prompt.
        # The synthesis prompt asks for one consolidated recommendation
        # across candidates; the analyzer prompt asks for structured
        # learnings from one piece of evidence.
        if 'synthesize' in system_prompt.lower():
            payload = self._stub_synthesis(user_prompt)
        else:
            payload = self._stub_analysis(user_prompt)
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        return LLMResult(
            raw_response=raw,
            parsed_json=payload,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=Decimal('0'),
            model_used=model,
            provider=self.name,
        )

    @staticmethod
    def _stub_analysis(user_prompt: str) -> dict:
        signal_pets = any(
            token in user_prompt.lower() for token in ('pet', 'dog', 'cat')
        )
        return {
            'summary': '[stub] Analysis produced by StubProvider (no ANTHROPIC_API_KEY configured).',
            'category': 'pricing' if 'price' in user_prompt.lower() else 'other',
            'subcategory': 'stub subcategory',
            'confidence': 0.5,
            'customer_intent': 'unknown',
            'outcome_analysis': '[stub] Reasoning not available without a real LLM.',
            'candidate_playbook_rules': [
                {
                    'title': 'Mention that we bring all supplies including pet-safe products',
                    'description': 'When a customer mentions pets, proactively note we bring pet-safe supplies.',
                    'confidence': 0.6,
                }
            ] if signal_pets else [],
            'candidate_faq': [],
            'signals': ['[stub] pets mentioned'] if signal_pets else ['[stub] no notable signals'],
        }

    @staticmethod
    def _stub_synthesis(user_prompt: str) -> dict:
        return {
            'title': '[stub] Consolidated recommendation from cluster',
            'description': (
                '[stub] StubProvider consolidated the cluster into one recommendation. '
                'Configure ANTHROPIC_API_KEY for real synthesis.'
            ),
            'confidence': 0.5,
            'why_this_matters': '[stub] Business impact not evaluated without a real LLM.',
            'supporting_evidence_summary': '[stub] Multiple candidates across evidence supported this pattern.',
            'suggested_playbook_change': '[stub] Playbook change not proposed.',
            'suggested_faq_addition': '',
        }


class AnthropicProvider(BaseProvider):
    """Calls Anthropic Messages API. Applies prompt caching to the system block."""

    name = 'anthropic'

    def __init__(self, api_key: str):
        if not ANTHROPIC_AVAILABLE:
            raise LLMProviderError('anthropic SDK not installed')
        self._client = Anthropic(api_key=api_key)

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
    ) -> LLMResult:
        # Prefill with `{` to strongly bias output toward JSON. The
        # analyzer's schema validator handles the rest.
        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=1500,
                system=[
                    {
                        'type': 'text',
                        'text': system_prompt,
                        'cache_control': {'type': 'ephemeral'},
                    }
                ],
                messages=[
                    {'role': 'user', 'content': user_prompt},
                    {'role': 'assistant', 'content': '{'},
                ],
            )
        except Exception as exc:
            raise LLMProviderError(f'Anthropic call failed: {exc}') from exc

        text_parts = [block.text for block in response.content if getattr(block, 'type', '') == 'text']
        raw = '{' + ''.join(text_parts)
        parsed = _try_parse_json(raw)
        usage = response.usage
        input_tokens = getattr(usage, 'input_tokens', 0) or 0
        output_tokens = getattr(usage, 'output_tokens', 0) or 0
        cache_read_tokens = getattr(usage, 'cache_read_input_tokens', 0) or 0
        cache_write_tokens = getattr(usage, 'cache_creation_input_tokens', 0) or 0
        cost = compute_cost(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        logger.info(
            'anthropic call model=%s input=%d output=%d cache_read=%d cache_write=%d cost_usd=%s',
            model, input_tokens, output_tokens,
            cache_read_tokens, cache_write_tokens, cost,
        )
        return LLMResult(
            raw_response=raw,
            parsed_json=parsed,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cost_usd=cost,
            model_used=model,
            provider=self.name,
        )


def compute_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """USD cost for one Anthropic call, cache-aware.

    Anthropic bills cache reads at ~10% and cache writes at ~125% of the
    base input rate. Multipliers live in LEARNING_MODEL_PRICING per model
    so unusual pricing can be overridden without touching this code.
    """
    pricing = settings.LEARNING_MODEL_PRICING.get(model)
    if not pricing:
        logger.warning('No pricing configured for model %s; cost will be zero', model)
        return Decimal('0')

    input_rate = Decimal(str(pricing['input_per_mtok']))
    output_rate = Decimal(str(pricing['output_per_mtok']))
    cache_read_mult = Decimal(str(pricing.get('cache_read_multiplier', 0.10)))
    cache_write_mult = Decimal(str(pricing.get('cache_write_multiplier', 1.25)))
    per_mtok = Decimal(1_000_000)

    input_cost = input_rate * Decimal(input_tokens) / per_mtok
    output_cost = output_rate * Decimal(output_tokens) / per_mtok
    cache_read_cost = input_rate * cache_read_mult * Decimal(cache_read_tokens) / per_mtok
    cache_write_cost = input_rate * cache_write_mult * Decimal(cache_write_tokens) / per_mtok
    total = input_cost + output_cost + cache_read_cost + cache_write_cost
    return total.quantize(Decimal('0.0001'))


def _try_parse_json(raw: str) -> dict:
    """Extract a JSON object from the model output.

    The prefill trick means the model *starts* with `{`, but it might
    include trailing prose. Grab the outermost JSON object with a greedy
    brace match; fall back to raising if that fails.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{.*\}', raw, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMProviderError(f'Model output was not valid JSON: {exc}') from exc
    raise LLMProviderError(f'No JSON object found in model output: {raw[:200]!r}')


class LearningLLMClient:
    """Generic entry point analyzers call.

    Chooses a provider by model-name prefix and cached provider instances
    for reuse. Any analyzer can call this without knowing which SDK
    handles the request.
    """

    def __init__(self):
        self._providers: dict[str, BaseProvider] = {}

    def analyze(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
    ) -> LLMResult:
        provider = self._resolve(model)
        return provider.complete(system_prompt, user_prompt, model)

    def _resolve(self, model: str) -> BaseProvider:
        provider_key = self._provider_key(model)
        if provider_key in self._providers:
            return self._providers[provider_key]
        provider = self._instantiate(provider_key)
        self._providers[provider_key] = provider
        return provider

    @staticmethod
    def _provider_key(model: str) -> str:
        if model.startswith('claude-'):
            return 'anthropic'
        if model.startswith('gpt-') or model.startswith('o1-') or model.startswith('o3-'):
            return 'openai'
        return 'stub'

    def _instantiate(self, provider_key: str) -> BaseProvider:
        if provider_key == 'anthropic':
            api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
            if api_key and ANTHROPIC_AVAILABLE:
                try:
                    return AnthropicProvider(api_key=api_key)
                except LLMProviderError as exc:
                    logger.warning('Falling back to stub provider: %s', exc)
            else:
                logger.info(
                    'Anthropic provider unavailable (api_key=%s, sdk_installed=%s); using stub',
                    bool(api_key), ANTHROPIC_AVAILABLE,
                )
            return StubProvider()
        # OpenAI not wired in Phase 1 — fall through to stub.
        if provider_key == 'openai':
            logger.info('OpenAI provider not yet implemented; using stub')
        return StubProvider()
