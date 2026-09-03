"""BusinessConfigProposal synthesizer (V2).

Reads a tenant's historical EvidenceEvent corpus (persisted via
/api/context/v1/context, mode=report), calls the LLM once per requested
domain-group to extract structured observations, then compares those against
BOTH the template baseline AND the current tenant configuration provided in
the request. Emits a BusinessConfigProposal.

V2 differences vs v1:
  - pricing_model is a first-class structured field
  - pricing_examples[] emit as separate observed_example Changes (never
    tenant-wide rules)
  - Commercial policies (materials_included, payment_methods) go to the
    'policies' domain
  - Services observed go to the 'services' domain
  - FAQ candidates are filtered by the LLM to durable Q&A only (see
    prompts.FAQ_SYSTEM REJECT list)
  - Each Change carries effectiveCurrentValue and factKind
  - status computation uses effectiveCurrentValue so template-default
    matches don't produce redundant proposed writes

Provenance rule (LB → BehaviorOS contract):
  status = f(templateValue, currentTenantValue, effectiveCurrentValue,
             historicalObservedValue)

  effectiveCurrentValue = currentTenantValue if present, else templateValue

  history absent  → 'insufficient_evidence' (if tenant has explicit) OR
                    'template_default_retained' (otherwise)
  history == effectiveCurrentValue  → 'confirmed_by_history'   (no-op)
  history != effectiveCurrentValue  →
      tenant absent   → 'proposed_new_from_history'   (set_value; must_review)
      tenant present  → 'contradicted_by_history'     (set_value; must_review)

factKind is:
  - explicit_rule       — pro explicitly stated a general rule
  - inferred_rule       — pattern across ≥N conversations
  - observed_example    — one-off concrete instance (never overwrites)
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Organization
from apps.context.models import EvidenceEvent
from apps.learning.services.llm_client import LearningLLMClient

from . import prompts

logger = logging.getLogger(__name__)


# Structured pricing fields the pricing prompt fills.
# fieldKey (semantic) → LLM key (same string for now).
PRICING_STRUCTURED_FIELDS = (
    'pricing_model',
    'hourly_rate',
    'minimum_hours',
    'minimum_charge',
    'quote_required',
)

# Commercial-policy fields (also filled by the pricing prompt but routed to
# the 'policies' domain).
POLICY_STRUCTURED_FIELDS = (
    'materials_included',
    'payment_methods',
)

VALID_FACT_KINDS = {'explicit_rule', 'inferred_rule', 'observed_example'}


@dataclass
class ProposalRequest:
    tenant_id: str
    template_key: str
    template_id: str
    template_snapshot: dict
    current_tenant_snapshot: dict
    domains: list[str]

    def __post_init__(self):
        if not self.tenant_id:
            raise ValueError('tenant_id required')
        if not self.template_key or not self.template_id:
            raise ValueError('template_key + template_id required')
        if not isinstance(self.template_snapshot, dict):
            raise ValueError('template_snapshot must be dict')
        if not isinstance(self.current_tenant_snapshot, dict):
            raise ValueError('current_tenant_snapshot must be dict')
        valid = {'pricing', 'faq', 'services', 'policies'}
        unknown = [d for d in self.domains if d not in valid]
        if unknown:
            raise ValueError(f'unsupported domains: {unknown} (valid: {sorted(valid)})')


@dataclass
class ProposalResult:
    proposal: dict
    llm_costs_usd: Decimal
    evidence_count: int


class BusinessConfigProposalSynthesizer:
    """Runs one synthesis pass and returns a proposal dict.

    Not persistent — instantiate per request.
    """

    def __init__(self, llm_client: LearningLLMClient | None = None):
        self.llm = llm_client or LearningLLMClient()
        self.model = getattr(
            settings,
            'BUSINESS_CONFIG_PROPOSAL_MODEL',
            getattr(settings, 'LEARNING_ANALYZER_MODEL', 'claude-haiku-4-5-20251001'),
        )

    def synthesize(self, req: ProposalRequest) -> ProposalResult:
        org = _ensure_org(req.tenant_id)

        events = list(
            EvidenceEvent.objects
            .filter(org=org, runtime='leadbridge')
            .filter(Q(payload__sourceSystem__startswith='leadbridge-historical'))
            .order_by('occurred_at')
        )
        transcripts = [_transcript_for_event(e) for e in events if _event_has_transcript(e)]
        summary = _evidence_summary(events, transcripts)

        logger.info(
            'business_config synth tenant=%s org=%s events=%d transcripts=%d',
            req.tenant_id, org.id, len(events), len(transcripts),
        )

        changes: list[dict] = []
        synthesizer_notes: list[str] = []
        total_cost = Decimal('0')

        if not transcripts:
            synthesizer_notes.append(
                f'No historical evidence available for tenant {req.tenant_id}. '
                'All fields default to insufficient_evidence or '
                'template_default_retained.'
            )

        # ---- One LLM call covers pricing + commercial + services observations. ----
        pricing_llm_output = None
        if any(d in req.domains for d in ('pricing', 'services', 'policies')) and transcripts:
            pricing_llm_output, cost = self._call_pricing_prompt(req, transcripts, synthesizer_notes)
            total_cost += cost

        if 'pricing' in req.domains:
            changes.extend(self._build_pricing_changes(req, pricing_llm_output))

        if 'policies' in req.domains:
            changes.extend(self._build_policy_changes(req, pricing_llm_output))

        if 'services' in req.domains:
            changes.extend(self._build_services_changes(req, pricing_llm_output))

        if 'faq' in req.domains and transcripts:
            faq_changes, cost = self._synthesize_faq(req, transcripts, synthesizer_notes)
            changes.extend(faq_changes)
            total_cost += cost

        proposal = {
            'tenantId': req.tenant_id,
            'templateKey': req.template_key,
            'templateId': req.template_id,
            'generatedAt': timezone.now().isoformat(),
            'source': 'historical_analysis',
            'schemaVersion': 'business-config-proposal:v1',
            'evidence': summary,
            'changes': changes,
            'synthesizerNotes': synthesizer_notes,
        }

        return ProposalResult(
            proposal=proposal,
            llm_costs_usd=total_cost,
            evidence_count=len(transcripts),
        )

    # ---- pricing prompt call --------------------------------------------

    def _call_pricing_prompt(
        self,
        req: ProposalRequest,
        transcripts: list[dict],
        notes: list[str],
    ) -> tuple[dict, Decimal]:
        template_pricing = req.template_snapshot.get('pricing') or {}
        tenant_pricing = req.current_tenant_snapshot.get('pricing') or {}
        user_prompt = _format_pricing_user_prompt(template_pricing, tenant_pricing, transcripts)
        result = self.llm.analyze(
            system_prompt=prompts.PRICING_SYSTEM,
            user_prompt=user_prompt,
            model=self.model,
            max_tokens=4000,
        )
        parsed = result.parsed_json or {}
        # Detect stub response — analyzer-shape output (has candidate_playbook_rules).
        if 'pricing_model' not in parsed and 'candidate_playbook_rules' in parsed:
            notes.append(
                '[stub LLM detected] No real Anthropic/OpenAI key configured; '
                'pricing/commercial observations will be empty.'
            )
            return {}, result.cost_usd
        return parsed, result.cost_usd

    # ---- pricing domain -------------------------------------------------

    def _build_pricing_changes(self, req: ProposalRequest, llm_output: dict | None) -> list[dict]:
        template_pricing = req.template_snapshot.get('pricing') or {}
        tenant_pricing = req.current_tenant_snapshot.get('pricing') or {}
        changes = []
        for field_key in PRICING_STRUCTURED_FIELDS:
            observation = (llm_output or {}).get(field_key)
            change = self._build_structured_change(
                domain='pricing',
                field_key=field_key,
                human_label=_pricing_human_label(field_key),
                observation=observation,
                template_field=template_pricing,
                tenant_field=tenant_pricing,
                camel_map=_PRICING_CAMEL_MAP,
                coerce=lambda k, v: _coerce_pricing_type(k, v),
            )
            changes.append(change)

        # pricing_examples — each becomes its own observed_example Change.
        examples = (llm_output or {}).get('pricing_examples') or []
        if isinstance(examples, list):
            for ex in examples[:20]:  # hard cap
                if not isinstance(ex, dict):
                    continue
                item = str(ex.get('item') or '').strip()
                price = ex.get('price')
                if not item or price is None:
                    continue
                conv_id = str(ex.get('supporting_conversation_id') or '')
                snippet = str(ex.get('representative_snippet') or '')
                unit = str(ex.get('unit') or 'unknown')
                slug = _slugify(item)[:60] or f'example_{uuid.uuid4().hex[:8]}'
                changes.append({
                    'id': str(uuid.uuid4()),
                    'domain': 'pricing',
                    'fieldKey': f'pricing_example:{slug}',
                    'humanLabel': f'Observed example: {item}',
                    'templateValue': None,
                    'currentTenantValue': None,
                    'currentTenantProvenance': 'absent',
                    'effectiveCurrentValue': None,
                    'historicalObservedValue': {'item': item, 'price': price, 'unit': unit},
                    'status': 'proposed_new_from_history',
                    'factKind': 'observed_example',
                    'evidence': _mk_evidence(
                        confidence=0.9,  # examples are inherently concrete
                        support_ids=[conv_id] if conv_id else [],
                        snippet=snippet,
                        reasoning='Concrete observed price quote; NOT a tenant-wide rule.',
                    ),
                    'reviewPolicy': 'must_review',
                    # Examples are informational: apply is a no-op until we
                    # have a pricing_examples writer.
                    'proposedAction': {
                        'kind': 'no_op',
                        'reason': 'Observed example — informational only; not a tenant-wide rule.',
                    },
                })
        return changes

    # ---- policies domain ------------------------------------------------

    def _build_policy_changes(self, req: ProposalRequest, llm_output: dict | None) -> list[dict]:
        template_policies = req.template_snapshot.get('faq') or {}  # policies live under faq in template
        tenant_policies = req.current_tenant_snapshot.get('faq') or {}
        changes = []

        # materials_included
        obs = (llm_output or {}).get('materials_included')
        template_val = _extract_bool(template_policies.get('materialsIncluded'))
        tenant_val, tenant_prov = _tenant_field(tenant_policies, 'materialsIncluded')
        changes.append(self._structured_change_from_observation(
            domain='policies',
            field_key='materials_included',
            human_label='Materials included in estimates',
            observation=obs,
            template_value=template_val,
            tenant_value=tenant_val,
            tenant_provenance=tenant_prov,
            coerce=_coerce_bool,
        ))

        # payment_methods (array)
        obs = (llm_output or {}).get('payment_methods')
        template_val = template_policies.get('paymentMethods') if isinstance(template_policies.get('paymentMethods'), list) else []
        tenant_val, tenant_prov = _tenant_field(tenant_policies, 'paymentMethods', default=[])
        obs_value = obs.get('observed_value') if isinstance(obs, dict) else None
        # Normalize observed_value to a sorted deduped list.
        historical_value = _coerce_string_list(obs_value)
        effective = tenant_val if tenant_val else template_val
        status = _compute_status_array(tenant_val, tenant_prov, historical_value, template_val, effective)
        changes.append({
            'id': str(uuid.uuid4()),
            'domain': 'policies',
            'fieldKey': 'payment_methods',
            'humanLabel': 'Payment methods accepted',
            'templateValue': template_val,
            'currentTenantValue': tenant_val,
            'currentTenantProvenance': tenant_prov,
            'effectiveCurrentValue': effective,
            'historicalObservedValue': historical_value if historical_value else None,
            'status': status,
            'factKind': _extract_fact_kind(obs),
            'evidence': _mk_evidence_from_obs(obs) if historical_value else None,
            'reviewPolicy': 'must_review',
            'proposedAction': _proposed_action(status, historical_value, tenant_val),
        })

        return changes

    # ---- services domain ------------------------------------------------

    def _build_services_changes(self, req: ProposalRequest, llm_output: dict | None) -> list[dict]:
        obs = (llm_output or {}).get('services_observed')
        if not isinstance(obs, dict):
            return []
        historical_value = _coerce_string_list(obs.get('observed_value'))
        # Services baseline lives in template.serviceOptionsJson (not part of
        # our snapshot v1 — treat as null baseline; owner reviews the whole
        # list at once).
        changes = [{
            'id': str(uuid.uuid4()),
            'domain': 'services',
            'fieldKey': 'services_offered',
            'humanLabel': 'Services observed in historical work',
            'templateValue': None,
            'currentTenantValue': None,
            'currentTenantProvenance': 'absent',
            'effectiveCurrentValue': None,
            'historicalObservedValue': historical_value if historical_value else None,
            'status': 'proposed_new_from_history' if historical_value else 'insufficient_evidence',
            'factKind': _extract_fact_kind(obs) or 'inferred_rule',
            'evidence': _mk_evidence_from_obs(obs) if historical_value else None,
            'reviewPolicy': 'must_review',
            'proposedAction': (
                {'kind': 'set_value', 'value': historical_value}
                if historical_value
                else {'kind': 'no_op', 'reason': 'No specific services observed.'}
            ),
        }]
        return changes

    # ---- faq domain -----------------------------------------------------

    def _synthesize_faq(
        self,
        req: ProposalRequest,
        transcripts: list[dict],
        notes: list[str],
    ) -> tuple[list[dict], Decimal]:
        template_faq = req.template_snapshot.get('faq') or {}
        tenant_faq = req.current_tenant_snapshot.get('faq') or {}
        user_prompt = _format_faq_user_prompt(template_faq, tenant_faq, transcripts)
        result = self.llm.analyze(
            system_prompt=prompts.FAQ_SYSTEM,
            user_prompt=user_prompt,
            model=self.model,
            max_tokens=3000,
        )
        parsed = result.parsed_json or {}
        if 'candidates' not in parsed and 'candidate_faq' in parsed:
            notes.append('[stub LLM detected] Fallback stub used for FAQ.')
            return ([], result.cost_usd)
        raw_candidates = parsed.get('candidates') or []
        if not isinstance(raw_candidates, list):
            return ([], result.cost_usd)

        tenant_customqa = _get_tenant_field(tenant_faq, 'customQA', default=[])
        existing_questions = {(qa.get('question') or '').strip().lower()
                              for qa in (tenant_customqa or [])
                              if isinstance(qa, dict)}

        changes = []
        for cand in raw_candidates[:8]:
            if not isinstance(cand, dict):
                continue
            field_key = str(cand.get('field_key') or '').strip()
            if not field_key:
                field_key = f'faq_{uuid.uuid4().hex[:8]}'
            question = str(cand.get('question') or '').strip()
            answer = str(cand.get('answer') or '').strip()
            if not question or not answer:
                continue
            fact_kind = str(cand.get('fact_kind') or '').strip()
            if fact_kind not in {'explicit_rule', 'inferred_rule'}:
                # V2 FAQ prompt forbids observed_example for FAQ. If the LLM
                # emits one, drop it (should have gone into pricing_examples).
                notes.append(
                    f'[faq] Dropped candidate {field_key}: fact_kind={fact_kind!r} '
                    'not allowed for FAQ (only explicit_rule or inferred_rule).'
                )
                continue
            confidence = float(cand.get('confidence', 0.0))
            support_ids = cand.get('supporting_conversation_ids', [])
            if not isinstance(support_ids, list):
                support_ids = []
            snippet = str(cand.get('representative_snippet') or '')
            reasoning = str(cand.get('reasoning') or '')
            human_label = str(cand.get('human_label') or question)[:120]

            proposed_value = {'question': question, 'answer': answer}
            already_answered = question.lower() in existing_questions

            if already_answered:
                status = 'confirmed_by_history'
                action = {'kind': 'no_op', 'reason': 'Tenant already has this FAQ.'}
                tenant_value = proposed_value
                tenant_prov = 'explicit_owner_input'
                effective = proposed_value
            else:
                status = 'proposed_new_from_history'
                action = {'kind': 'set_value', 'value': proposed_value}
                tenant_value = None
                tenant_prov = 'absent'
                effective = None

            changes.append({
                'id': str(uuid.uuid4()),
                'domain': 'faq',
                'fieldKey': f'faq:{field_key}',
                'humanLabel': human_label,
                'templateValue': None,
                'currentTenantValue': tenant_value,
                'currentTenantProvenance': tenant_prov,
                'effectiveCurrentValue': effective,
                'historicalObservedValue': proposed_value,
                'status': status,
                'factKind': fact_kind,
                'evidence': _mk_evidence(
                    confidence=confidence,
                    support_ids=[str(s) for s in support_ids],
                    snippet=snippet,
                    reasoning=reasoning,
                ),
                'reviewPolicy': 'must_review',
                'proposedAction': action,
            })

        return changes, result.cost_usd

    # ---- structured-change builder used by pricing (and reusable) -------

    def _build_structured_change(
        self,
        *,
        domain: str,
        field_key: str,
        human_label: str,
        observation: dict | None,
        template_field: dict,
        tenant_field: dict,
        camel_map: dict,
        coerce,
    ) -> dict:
        camel = camel_map[field_key]
        template_val = template_field.get(camel)
        tenant_raw = tenant_field.get(camel)
        if isinstance(tenant_raw, dict) and 'value' in tenant_raw:
            tenant_value = tenant_raw.get('value')
            tenant_prov = tenant_raw.get('provenance', 'absent')
        else:
            tenant_value = tenant_raw
            tenant_prov = 'absent' if tenant_value in (None, '', 0, False) else 'explicit_owner_input'

        # For numeric zero from template, treat as "unset default" — so a
        # tenant zero is 'template_default'. But an EXPLICIT non-zero value
        # is explicit_owner_input.
        # (LB's tenant-snapshot builder already computes provenance; we
        # trust it when present as a dict.)

        return self._structured_change_from_observation(
            domain=domain,
            field_key=field_key,
            human_label=human_label,
            observation=observation,
            template_value=template_val,
            tenant_value=tenant_value,
            tenant_provenance=tenant_prov,
            coerce=lambda v: coerce(field_key, v),
        )

    def _structured_change_from_observation(
        self,
        *,
        domain: str,
        field_key: str,
        human_label: str,
        observation: dict | None,
        template_value: Any,
        tenant_value: Any,
        tenant_provenance: str,
        coerce,
    ) -> dict:
        if isinstance(observation, dict):
            observed_raw = observation.get('observed_value')
            confidence = float(observation.get('confidence', 0.0))
            support_ids = observation.get('supporting_conversation_ids', [])
            if not isinstance(support_ids, list):
                support_ids = []
            snippet = str(observation.get('representative_snippet') or '')
            reasoning = str(observation.get('reasoning') or '')
            fact_kind = observation.get('fact_kind')
        else:
            observed_raw = None
            confidence = 0.0
            support_ids = []
            snippet = ''
            reasoning = ''
            fact_kind = None

        historical_value = coerce(observed_raw)
        # Require confidence ≥0.35 AND non-null value for a "real" signal.
        has_signal = historical_value is not None and confidence >= 0.35

        effective = _effective_current(tenant_value, template_value)
        status = _compute_status_scalar(
            tenant_value=tenant_value,
            tenant_provenance=tenant_provenance,
            historical_value=historical_value if has_signal else None,
            template_value=template_value,
            effective_value=effective,
        )
        evidence = _mk_evidence(
            confidence=confidence,
            support_ids=[str(s) for s in support_ids],
            snippet=snippet,
            reasoning=reasoning,
        ) if has_signal else None

        return {
            'id': str(uuid.uuid4()),
            'domain': domain,
            'fieldKey': field_key,
            'humanLabel': human_label,
            'templateValue': template_value,
            'currentTenantValue': tenant_value,
            'currentTenantProvenance': tenant_provenance,
            'effectiveCurrentValue': effective,
            'historicalObservedValue': historical_value if has_signal else None,
            'status': status,
            'factKind': fact_kind if fact_kind in VALID_FACT_KINDS else None,
            'evidence': evidence,
            'reviewPolicy': 'must_review',
            'proposedAction': _proposed_action(status, historical_value if has_signal else None, tenant_value),
        }


# =============================================================================
# Helpers
# =============================================================================

_PRICING_CAMEL_MAP = {
    'pricing_model': 'pricingModel',
    'hourly_rate': 'hourlyRate',
    'minimum_hours': 'minimumHours',
    'minimum_charge': 'minimumCharge',
    'quote_required': 'quoteRequired',
}


def _pricing_human_label(field_key: str) -> str:
    return {
        'pricing_model':   'Pricing model',
        'hourly_rate':     'Hourly rate (USD/hr)',
        'minimum_hours':   'Minimum billable hours',
        'minimum_charge':  'Minimum charge (USD)',
        'quote_required':  'Quote required before booking',
    }[field_key]


def _tenant_field(tenant_faq: dict, camel_key: str, default=None):
    """Read tenant snapshot's {value, provenance} FAQ/policy field."""
    field = tenant_faq.get(camel_key)
    if isinstance(field, dict) and 'value' in field:
        return field.get('value'), field.get('provenance', 'absent')
    if field is None:
        return default, 'absent'
    return field, 'explicit_owner_input'


def _extract_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {'true', 'yes', 'y', '1'}:
            return True
        if s in {'false', 'no', 'n', '0'}:
            return False
    if isinstance(v, dict) and 'value' in v:
        return _extract_bool(v['value'])
    return None


def _coerce_bool(v: Any) -> bool | None:
    return _extract_bool(v)


def _coerce_pricing_type(field_key: str, value: Any) -> Any:
    if value is None:
        return None
    if field_key == 'pricing_model':
        if isinstance(value, str) and value.strip():
            v = value.strip().lower()
            allowed = {'hourly', 'flat_project', 'itemized', 'hybrid', 'unclear'}
            return v if v in allowed else None
        return None
    if field_key == 'quote_required':
        return _coerce_bool(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace('$', '').replace(',', '').strip())
        except ValueError:
            return None
    return None


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen = []
    for item in value:
        if isinstance(item, str) and item.strip():
            s = item.strip()
            if s not in seen:
                seen.append(s)
    return seen


def _extract_fact_kind(obs: Any) -> str | None:
    if not isinstance(obs, dict):
        return None
    fk = obs.get('fact_kind')
    return fk if fk in VALID_FACT_KINDS else None


def _mk_evidence(*, confidence: float, support_ids: list[str], snippet: str, reasoning: str) -> dict:
    excerpts = []
    if snippet:
        first = support_ids[0] if support_ids else '<unknown>'
        excerpts.append({'conversationId': str(first), 'snippet': snippet[:500]})
    return {
        'confidence': confidence,
        'supportingConversationCount': len(support_ids),
        'representativeExcerpts': excerpts,
        'source': 'historical_analysis',
        'reasoning': reasoning or None,
    }


def _mk_evidence_from_obs(obs: dict) -> dict:
    return _mk_evidence(
        confidence=float(obs.get('confidence', 0.0)),
        support_ids=[str(s) for s in (obs.get('supporting_conversation_ids') or [])],
        snippet=str(obs.get('representative_snippet') or ''),
        reasoning=str(obs.get('reasoning') or ''),
    )


# ---- status / effectiveCurrent -------------------------------------------

def _is_effectively_present(v: Any) -> bool:
    """Does this value count as 'the tenant has an explicit value'?

    Numeric zero / empty-string / empty-list / None all count as ABSENT.
    Booleans (including False) count as present.
    """
    if v is None:
        return False
    if isinstance(v, bool):
        return True
    if v == 0:
        return False
    if v == '':
        return False
    if isinstance(v, (list, dict)) and len(v) == 0:
        return False
    return True


def _effective_current(tenant_value: Any, template_value: Any) -> Any:
    if _is_effectively_present(tenant_value):
        return tenant_value
    if _is_effectively_present(template_value):
        return template_value
    return None


def _values_equal(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    # None ≠ anything else — including False (bool coercion would say
    # bool(None)==False, which is wrong for absence semantics).
    if a is None or b is None:
        return False
    if type(a) is bool or type(b) is bool:
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 0.01
    return a == b


def _compute_status_scalar(
    *,
    tenant_value: Any,
    tenant_provenance: str,
    historical_value: Any,
    template_value: Any,
    effective_value: Any,
) -> str:
    """Status computation for scalar fields.

    Uses effective_value so that when tenant is absent but template supplies
    a default, a matching history observation is confirmed (no redundant
    write) rather than proposed_new.
    """
    tenant_explicit = _is_effectively_present(tenant_value)
    historical_present = historical_value is not None

    if not historical_present:
        # No history signal.
        if tenant_explicit and not _values_equal(tenant_value, template_value):
            return 'insufficient_evidence'
        # Tenant absent OR tenant matches template default.
        if effective_value is not None:
            return 'template_default_retained'
        return 'insufficient_evidence'

    # History has a value.
    if _values_equal(historical_value, effective_value):
        return 'confirmed_by_history'
    if not tenant_explicit:
        return 'proposed_new_from_history'
    return 'contradicted_by_history'


def _compute_status_array(
    tenant_value: list | None,
    tenant_provenance: str,
    historical_value: list | None,
    template_value: list | None,
    effective_value: list | None,
) -> str:
    tenant_explicit = _is_effectively_present(tenant_value)
    historical_present = _is_effectively_present(historical_value)

    if not historical_present:
        if tenant_explicit:
            return 'insufficient_evidence'
        if _is_effectively_present(effective_value):
            return 'template_default_retained'
        return 'insufficient_evidence'

    if set(historical_value or []) == set(effective_value or []):
        return 'confirmed_by_history'
    if not tenant_explicit:
        return 'proposed_new_from_history'
    return 'contradicted_by_history'


def _proposed_action(status: str, historical_value: Any, tenant_value: Any) -> dict:
    if status == 'confirmed_by_history':
        return {'kind': 'no_op', 'reason': 'Effective value already matches history.'}
    if status == 'template_default_retained':
        return {'kind': 'no_op', 'reason': 'No historical evidence; template default retained.'}
    if status == 'insufficient_evidence':
        return {'kind': 'no_op', 'reason': 'Historical corpus did not support a defensible value.'}
    if status == 'contradicted_by_history':
        return {'kind': 'set_value', 'value': historical_value}
    if status == 'proposed_new_from_history':
        return {'kind': 'set_value', 'value': historical_value}
    return {'kind': 'no_op', 'reason': f'Unknown status: {status}'}


def _get_tenant_field(tenant_faq: dict, key: str, default=None):
    field = tenant_faq.get(key)
    if isinstance(field, dict) and 'value' in field:
        return field.get('value')
    return field if field is not None else default


# ---- evidence I/O + slugs ------------------------------------------------

def _ensure_org(tenant_id: str) -> Organization:
    try:
        return Organization.objects.get(pk=tenant_id)
    except Organization.DoesNotExist:
        return Organization.objects.create(
            id=uuid.UUID(tenant_id),
            name=f'LB tenant {tenant_id[:8]}',
        )


def _slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    return s.strip('_')


def _event_has_transcript(event: EvidenceEvent) -> bool:
    payload = event.payload or {}
    metadata = payload.get('metadata') if isinstance(payload, dict) else None
    return isinstance(metadata, dict) and bool(metadata.get('transcript'))


def _transcript_for_event(event: EvidenceEvent) -> dict:
    payload = event.payload or {}
    metadata = payload.get('metadata') if isinstance(payload, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    transcript = metadata.get('transcript') or []
    if not isinstance(transcript, list):
        transcript = []
    conv_id = (
        payload.get('conversationId')
        or metadata.get('externalId')
        or event.conversation_id
        or '<unknown>'
    )
    return {
        'conversation_id': str(conv_id),
        'occurred_at': (event.occurred_at or timezone.now()).isoformat(),
        'outcome': metadata.get('outcome') or 'unknown',
        'category': metadata.get('category') or '',
        'customer_name': metadata.get('customerName') or '',
        'turns': [
            {
                'role': str(t.get('role') or 'system'),
                'text': str(t.get('text') or ''),
                'senderType': t.get('senderType'),
            }
            for t in transcript if isinstance(t, dict)
        ],
    }


def _evidence_summary(events: list, transcripts: list[dict]) -> dict:
    total_messages = 0
    dates = []
    for t in transcripts:
        total_messages += len(t.get('turns') or [])
        if t.get('occurred_at'):
            dates.append(t['occurred_at'])
    dates.sort()
    return {
        'conversationsAnalyzed': len(transcripts),
        'totalMessages': total_messages,
        'dateRangeStart': dates[0] if dates else '',
        'dateRangeEnd': dates[-1] if dates else '',
    }


# ---- prompt formatting ---------------------------------------------------

def _format_pricing_user_prompt(template: dict, tenant: dict, transcripts: list[dict]) -> str:
    def get(field: dict, key: str):
        v = field.get(key)
        if isinstance(v, dict) and 'value' in v:
            return v.get('value')
        return v

    lines = []
    lines.append('# Template baseline pricing')
    for k in ('pricingModel', 'hourlyRate', 'minimumHours', 'minimumCharge', 'quoteRequired', 'currency'):
        lines.append(f'  {k}: {template.get(k)!r}')
    lines.append('')
    lines.append('# Tenant CURRENT configured pricing (explicit onboarding — authoritative unless contradicted)')
    for k in ('pricingModel', 'hourlyRate', 'minimumHours', 'minimumCharge', 'quoteRequired', 'currency'):
        val = get(tenant, k)
        prov = tenant.get(k, {}).get('provenance') if isinstance(tenant.get(k), dict) else 'unknown'
        lines.append(f'  {k}: {val!r}  (provenance={prov})')
    lines.append('')
    lines.append(f'# {len(transcripts)} historical conversation transcripts')
    lines.append('')
    for t in transcripts:
        lines.append(f'--- conversation_id={t["conversation_id"]}  outcome={t["outcome"]}  category={t["category"]}')
        for turn in t['turns']:
            role = turn['role']
            text = turn['text'].strip()
            if not text:
                continue
            lines.append(f'  {role}: {text}')
        lines.append('')
    return '\n'.join(lines)


def _format_faq_user_prompt(template: dict, tenant: dict, transcripts: list[dict]) -> str:
    def get(field: dict, key: str, default=None):
        v = field.get(key)
        if isinstance(v, dict) and 'value' in v:
            return v.get('value')
        return v if v is not None else default

    lines = []
    lines.append('# Template baseline FAQ (usually empty)')
    tpl_qa = template.get('customQA') or []
    for i, qa in enumerate(tpl_qa[:20]):
        if isinstance(qa, dict):
            lines.append(f'  {i+1}. Q: {qa.get("question")}')
            lines.append(f'     A: {qa.get("answer")}')
    if not tpl_qa:
        lines.append('  (none)')
    lines.append('')
    lines.append('# Tenant CURRENT FAQ')
    tenant_qa = get(tenant, 'customQA', default=[])
    if tenant_qa:
        for i, qa in enumerate(tenant_qa[:20]):
            if isinstance(qa, dict):
                lines.append(f'  {i+1}. Q: {qa.get("question")}')
                lines.append(f'     A: {qa.get("answer")}')
    else:
        lines.append('  (none)')
    lines.append('')
    lines.append(f'# {len(transcripts)} historical conversation transcripts')
    lines.append('')
    for t in transcripts:
        lines.append(f'--- conversation_id={t["conversation_id"]}  outcome={t["outcome"]}')
        for turn in t['turns']:
            role = turn['role']
            text = turn['text'].strip()
            if not text:
                continue
            lines.append(f'  {role}: {text}')
        lines.append('')
    return '\n'.join(lines)
