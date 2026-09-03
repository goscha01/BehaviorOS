"""BusinessConfigProposal synthesizer.

Reads a tenant's historical EvidenceInsight corpus (source_system starting
with 'leadbridge-historical'), calls the LLM once per requested domain to
extract observed values, then compares those observations against both the
template baseline AND the current tenant configuration provided in the
request. Emits a BusinessConfigProposal that LB can apply (or dry-run).

No models added by this module — all output is returned inline. Persistence
can come later if needed for audit.

Provenance rule (LB → BehaviorOS contract):
  status = f(templateValue, currentTenantValue, currentTenantProvenance, historicalObservedValue)

  historicalObservedValue is None  → 'insufficient_evidence' (unless tenant
                                     has a value and it equals template →
                                     'template_default_retained')
  tenant absent, history has value → 'proposed_new_from_history'
  tenant present, history matches  → 'confirmed_by_history'
  tenant present, history differs  → 'contradicted_by_history'
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
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


# Fields we ask the LLM about in the pricing prompt. Kept centralized so the
# prompt and the comparator can't drift out of sync.
PRICING_FIELD_KEYS = ('hourly_rate', 'minimum_hours', 'minimum_charge', 'quote_required')


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
        # Only pricing + faq for Slice 1. Others are reserved.
        valid = {'pricing', 'faq'}
        unknown = [d for d in self.domains if d not in valid]
        if unknown:
            raise ValueError(f'unsupported domains for Slice 1: {unknown}')


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

        # 1. Load historical evidence for this tenant. LB backfill posts via
        # /api/context/v1/context (mode=report), which persists as
        # EvidenceEvent (apps/context/). We filter to LB historical rows via
        # runtime='leadbridge' AND payload.sourceSystem starting with
        # 'leadbridge-historical' — this excludes live runtime events from
        # the same tenant.
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
                'Nothing to compare against; all fields will default to '
                'template_default_retained or insufficient_evidence.'
            )

        if 'pricing' in req.domains:
            pricing_changes, cost = self._synthesize_pricing(req, transcripts, synthesizer_notes)
            changes.extend(pricing_changes)
            total_cost += cost

        if 'faq' in req.domains:
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

    # ---- pricing --------------------------------------------------------

    def _synthesize_pricing(
        self,
        req: ProposalRequest,
        transcripts: list[dict],
        notes: list[str],
    ) -> tuple[list[dict], Decimal]:
        template_pricing = req.template_snapshot.get('pricing') or {}
        tenant_pricing = req.current_tenant_snapshot.get('pricing') or {}

        if not transcripts:
            # Emit stub changes so LB always sees the same shape.
            return (
                [self._emit_pricing_no_evidence(field, template_pricing, tenant_pricing)
                 for field in PRICING_FIELD_KEYS],
                Decimal('0'),
            )

        user_prompt = _format_pricing_user_prompt(template_pricing, tenant_pricing, transcripts)
        result = self.llm.analyze(
            system_prompt=prompts.PRICING_SYSTEM,
            user_prompt=user_prompt,
            model=self.model,
            max_tokens=2500,
        )
        parsed = result.parsed_json or {}

        # If the stub returned analyzer-shape output (candidate_playbook_rules etc.),
        # detect it and note — but still emit correctly-shaped Change objects.
        if 'hourly_rate' not in parsed and 'candidate_playbook_rules' in parsed:
            notes.append(
                '[stub LLM detected] No real Anthropic/OpenAI key configured; '
                'pricing observations will be empty. Configure API key for real synthesis.'
            )
            parsed = {}

        changes = []
        for field_key in PRICING_FIELD_KEYS:
            observation = parsed.get(field_key) or {}
            change = self._build_pricing_change(field_key, observation, template_pricing, tenant_pricing)
            changes.append(change)

        # Non-fixed "other_pricing_notes" — surfaced as synthesizer notes, not as a Change.
        other = parsed.get('other_pricing_notes') or {}
        if isinstance(other, dict) and other.get('observed_value'):
            notes.append(f"[pricing] Additional pattern observed: {other.get('observed_value')} "
                         f"(confidence={other.get('confidence', 0)})")

        return changes, result.cost_usd

    def _emit_pricing_no_evidence(
        self,
        field_key: str,
        template_pricing: dict,
        tenant_pricing: dict,
    ) -> dict:
        template_val, tenant_val, tenant_prov = _resolve_pricing_field(
            field_key, template_pricing, tenant_pricing
        )
        status = _compute_status(tenant_val, tenant_prov, None, template_val)
        return _change_object(
            domain='pricing',
            field_key=field_key,
            human_label=_pricing_human_label(field_key),
            template_value=template_val,
            tenant_value=tenant_val,
            tenant_provenance=tenant_prov,
            historical_value=None,
            status=status,
            evidence=None,
        )

    def _build_pricing_change(
        self,
        field_key: str,
        observation: dict,
        template_pricing: dict,
        tenant_pricing: dict,
    ) -> dict:
        template_val, tenant_val, tenant_prov = _resolve_pricing_field(
            field_key, template_pricing, tenant_pricing
        )
        observed = observation.get('observed_value') if isinstance(observation, dict) else None
        confidence = float(observation.get('confidence', 0.0)) if isinstance(observation, dict) else 0.0
        support_ids = observation.get('supporting_conversation_ids', []) if isinstance(observation, dict) else []
        snippet = observation.get('representative_snippet', '') if isinstance(observation, dict) else ''
        reasoning = observation.get('reasoning', '') if isinstance(observation, dict) else ''

        historical_value = _coerce_pricing_type(field_key, observed)
        # `insufficient_evidence` if the LLM explicitly said so (null value) or
        # confidence is very low (<0.35).
        has_signal = historical_value is not None and confidence >= 0.35
        status = _compute_status(
            tenant_val,
            tenant_prov,
            historical_value if has_signal else None,
            template_val,
        )

        evidence = None
        if has_signal:
            evidence = {
                'confidence': confidence,
                'supportingConversationCount': len(support_ids) if isinstance(support_ids, list) else 0,
                'representativeExcerpts': _snippet_to_excerpts(snippet, support_ids),
                'source': 'historical_analysis',
                'reasoning': reasoning or None,
            }

        return _change_object(
            domain='pricing',
            field_key=field_key,
            human_label=_pricing_human_label(field_key),
            template_value=template_val,
            tenant_value=tenant_val,
            tenant_provenance=tenant_prov,
            historical_value=historical_value if has_signal else None,
            status=status,
            evidence=evidence,
        )

    # ---- faq ------------------------------------------------------------

    def _synthesize_faq(
        self,
        req: ProposalRequest,
        transcripts: list[dict],
        notes: list[str],
    ) -> tuple[list[dict], Decimal]:
        template_faq = req.template_snapshot.get('faq') or {}
        tenant_faq = req.current_tenant_snapshot.get('faq') or {}

        if not transcripts:
            return ([], Decimal('0'))

        user_prompt = _format_faq_user_prompt(template_faq, tenant_faq, transcripts)
        result = self.llm.analyze(
            system_prompt=prompts.FAQ_SYSTEM,
            user_prompt=user_prompt,
            model=self.model,
            max_tokens=3000,
        )
        parsed = result.parsed_json or {}

        if 'candidates' not in parsed and 'candidate_faq' in parsed:
            notes.append(
                '[stub LLM detected] Fallback stub used for FAQ synthesis; '
                'no real candidates emitted.'
            )
            return ([], result.cost_usd)

        raw_candidates = parsed.get('candidates') or []
        if not isinstance(raw_candidates, list):
            notes.append(f'[faq] Synthesizer returned non-list candidates; dropping. Got: {type(raw_candidates).__name__}')
            return ([], result.cost_usd)

        tenant_customqa = _get_tenant_field(tenant_faq, 'customQA', default=[])
        template_customqa = _get_tenant_field(template_faq, 'customQA', default=[])
        existing_questions = {(qa.get('question') or '').strip().lower()
                              for qa in (tenant_customqa or [])
                              if isinstance(qa, dict)}

        changes = []
        for cand in raw_candidates[:15]:  # hard cap
            if not isinstance(cand, dict):
                continue
            field_key = str(cand.get('field_key') or '').strip()
            if not field_key:
                field_key = f'faq_{uuid.uuid4().hex[:8]}'
            question = str(cand.get('question') or '').strip()
            answer = str(cand.get('answer') or '').strip()
            if not question or not answer:
                continue
            confidence = float(cand.get('confidence', 0.0))
            support_ids = cand.get('supporting_conversation_ids', []) if isinstance(cand.get('supporting_conversation_ids'), list) else []
            snippet = str(cand.get('representative_snippet') or '')
            reasoning = str(cand.get('reasoning') or '')
            human_label = str(cand.get('human_label') or question)[:120]

            already_answered = question.lower() in existing_questions
            proposed_value = {'question': question, 'answer': answer}

            if already_answered:
                # We have an entry already — treat as confirmed_by_history
                # (we're not proposing to overwrite an existing FAQ in Slice 1;
                # no fallback / merging).
                status = 'confirmed_by_history'
                action = {'kind': 'no_op', 'reason': 'Tenant already has an FAQ entry matching this question.'}
                tenant_value = proposed_value
                tenant_prov = 'explicit_owner_input'
            else:
                # New entry.
                status = 'proposed_new_from_history'
                action = {'kind': 'set_value', 'value': proposed_value}
                tenant_value = None
                tenant_prov = 'absent'

            change = {
                'id': str(uuid.uuid4()),
                'domain': 'faq',
                'fieldKey': f'faq:{field_key}',
                'humanLabel': human_label,
                'templateValue': None,  # template FAQs are unstructured; treat as null baseline
                'currentTenantValue': tenant_value,
                'currentTenantProvenance': tenant_prov,
                'historicalObservedValue': proposed_value,
                'status': status,
                'evidence': {
                    'confidence': confidence,
                    'supportingConversationCount': len(support_ids),
                    'representativeExcerpts': _snippet_to_excerpts(snippet, support_ids),
                    'source': 'historical_analysis',
                    'reasoning': reasoning or None,
                },
                # Slice 1 policy: all FAQ candidates require review even
                # if the schema supports auto_apply_eligible for high-confidence
                # cases. The must_review vs auto_apply_eligible distinction
                # is stored so the applier can honor it later.
                'reviewPolicy': 'must_review',
                'proposedAction': action,
            }
            changes.append(change)

        # Also mention template default customQA that the tenant hasn't
        # populated, as a note (not a Change).
        if template_customqa and not tenant_customqa:
            notes.append(
                f'[faq] Template has {len(template_customqa)} default customQA entries; '
                'tenant has none. Consider whether template defaults should backfill '
                'in a separate flow.'
            )

        return changes, result.cost_usd


# ---- comparators ----------------------------------------------------------

def _compute_status(
    tenant_value: Any,
    tenant_provenance: str,
    historical_value: Any,
    template_value: Any,
) -> str:
    tenant_present = tenant_value is not None and tenant_value != ''
    historical_present = historical_value is not None

    if not historical_present:
        if tenant_present and _values_equal(tenant_value, template_value):
            return 'template_default_retained'
        if tenant_present:
            # Tenant has an explicit non-template value; history didn't cover it.
            return 'insufficient_evidence'
        return 'template_default_retained' if template_value is not None else 'insufficient_evidence'

    if not tenant_present:
        return 'proposed_new_from_history'

    if _values_equal(tenant_value, historical_value):
        return 'confirmed_by_history'
    return 'contradicted_by_history'


def _values_equal(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if type(a) is bool or type(b) is bool:
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        # Small tolerance for hourly rates / minimum charges scraped from prose.
        return abs(float(a) - float(b)) < 0.01
    return a == b


def _resolve_pricing_field(field_key: str, template_pricing: dict, tenant_pricing: dict):
    """Extract (template_value, tenant_value, tenant_provenance) for a pricing field."""
    camel_map = {
        'hourly_rate': 'hourlyRate',
        'minimum_hours': 'minimumHours',
        'minimum_charge': 'minimumCharge',
        'quote_required': 'quoteRequired',
    }
    key = camel_map[field_key]
    template_val = template_pricing.get(key)
    tenant_field = tenant_pricing.get(key)
    if isinstance(tenant_field, dict):
        return template_val, tenant_field.get('value'), tenant_field.get('provenance', 'absent')
    return template_val, tenant_field, 'absent'


def _coerce_pricing_type(field_key: str, value: Any) -> Any:
    if value is None:
        return None
    if field_key == 'quote_required':
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {'true', 'yes', 'y', '1'}
        return None
    # Numeric fields
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace('$', '').replace(',', '').strip())
        except ValueError:
            return None
    return None


def _pricing_human_label(field_key: str) -> str:
    return {
        'hourly_rate': 'Hourly rate (USD/hr)',
        'minimum_hours': 'Minimum billable hours',
        'minimum_charge': 'Minimum charge (USD)',
        'quote_required': 'Quote required before booking',
    }[field_key]


def _change_object(*, domain, field_key, human_label, template_value, tenant_value,
                   tenant_provenance, historical_value, status, evidence):
    action = _proposed_action(status, historical_value, tenant_value)
    return {
        'id': str(uuid.uuid4()),
        'domain': domain,
        'fieldKey': field_key,
        'humanLabel': human_label,
        'templateValue': template_value,
        'currentTenantValue': tenant_value,
        'currentTenantProvenance': tenant_provenance,
        'historicalObservedValue': historical_value,
        'status': status,
        'evidence': evidence,
        'reviewPolicy': 'must_review',  # Slice 1: everything requires review
        'proposedAction': action,
    }


def _proposed_action(status: str, historical_value: Any, tenant_value: Any) -> dict:
    if status == 'confirmed_by_history':
        return {'kind': 'no_op', 'reason': 'Tenant value matches historical observation.'}
    if status == 'template_default_retained':
        return {'kind': 'no_op', 'reason': 'No historical evidence; template default retained.'}
    if status == 'insufficient_evidence':
        return {'kind': 'no_op', 'reason': 'Historical corpus did not support a defensible value.'}
    if status == 'contradicted_by_history':
        return {'kind': 'set_value', 'value': historical_value}
    if status == 'proposed_new_from_history':
        return {'kind': 'set_value', 'value': historical_value}
    return {'kind': 'no_op', 'reason': f'Unknown status: {status}'}


def _snippet_to_excerpts(snippet: str, support_ids: list) -> list:
    if not snippet:
        return []
    first_id = support_ids[0] if support_ids and isinstance(support_ids, list) else 'unknown'
    return [{'conversationId': str(first_id), 'snippet': snippet[:500]}]


def _get_tenant_field(tenant_faq: dict, key: str, default=None):
    field = tenant_faq.get(key)
    if isinstance(field, dict) and 'value' in field:
        return field.get('value')
    return field if field is not None else default


# ---- evidence I/O ---------------------------------------------------------

def _ensure_org(tenant_id: str) -> Organization:
    """Get-or-create the Organization row for this tenant.

    LB uses its User.id as the Organization.id. Auto-provisioning here means
    Slice 1 doesn't need a separate onboarding call.
    """
    try:
        return Organization.objects.get(pk=tenant_id)
    except Organization.DoesNotExist:
        return Organization.objects.create(
            id=uuid.UUID(tenant_id),
            name=f'LB tenant {tenant_id[:8]}',
        )


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
    # LB sets conversationId at top of request body AND in metadata.externalId
    # (see behavior-os-client.ts). Prefer the conversationId field, then
    # metadata.externalId, then event.conversation_id, else '<unknown>'.
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
    lines.append('# Template baseline (starting shape; usually zero/unset)')
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
