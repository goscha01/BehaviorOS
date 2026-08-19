"""ServiceFlow entity resolver.

Same shape as the LeadBridge resolver — in-memory backend for tests,
HTTP stub for production. The SF read endpoint does not exist yet.

A conversation may map to multiple SF entities (customer, opportunity,
job, appointment) — the resolver returns them all, each as a separate
ResolutionResult so EntityLink rows are per-entity.

Required ServiceFlow endpoint (not yet implemented on SF side):

    POST /api/v1/learning/entities/lookup
    Authorization: Bearer <SERVICEFLOW_LEARNING_TOKEN>
    Content-Type: application/json

    Body:
      {
        "workspace_id": "<sf-workspace-uuid>",  # optional if scoped-by-token
        "identifiers": {
          "leadbridge_lead_id": "<lb-lead-uuid>",  # strongest match
          "phone_e164":         "+18135551234",
          "email":              "customer@example.com"
        }
      }

    Response 200:
      {
        "entities": [
          {"type": "customer",     "id": "<sf-cust-uuid>",  "matched_on": "leadbridge_lead_id"},
          {"type": "opportunity",  "id": "<sf-opp-uuid>",   "matched_on": "customer"},
          {"type": "job",          "id": "<sf-job-uuid>",   "matched_on": "opportunity"},
          {"type": "appointment",  "id": "<sf-appt-uuid>",  "matched_on": "job"}
        ]
      }
      — empty `entities` list means no match. Never a 404.

Precedence: leadbridge_lead_id > phone_e164 > email > (no match).
Downstream entities (opportunity, job, appointment) are joined off the
customer once identity is established — SF side determines the join.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from django.conf import settings

from apps.conversations.models import (
    Conversation,
    MatchMethod,
    TargetSystem,
    TargetType,
)
from apps.conversations.resolvers.base import BaseResolver, ResolutionResult

logger = logging.getLogger(__name__)


class InMemoryServiceFlowResolver(BaseResolver):
    """Test / dev backend. Callers register SF entity graphs keyed by
    the identifiers a caller can supply (LB lead id, phone, email)."""

    target_system = TargetSystem.SERVICEFLOW

    def __init__(self):
        # Each key maps to a list of (type, id) tuples representing the
        # full downstream entity graph for that customer.
        self._by_lb_lead_id: dict[str, list[tuple[str, str]]] = {}
        self._by_phone: dict[str, list[tuple[str, str]]] = {}
        self._by_email: dict[str, list[tuple[str, str]]] = {}

    def register_entity_graph(
        self,
        entities: list[tuple[str, str]],
        *,
        lb_lead_id: Optional[str] = None,
        phone_e164: Optional[str] = None,
        email: Optional[str] = None,
    ) -> None:
        """Register the full [(type, id), ...] entity list for a customer
        keyed by any / all of the identifiers supplied.
        """
        if lb_lead_id:
            self._by_lb_lead_id[lb_lead_id] = list(entities)
        if phone_e164:
            self._by_phone[phone_e164] = list(entities)
        if email:
            self._by_email[email.lower()] = list(entities)

    def resolve(
        self, conversation: Conversation, *,
        leadbridge_lead_id: Optional[str] = None,
    ) -> Iterable[ResolutionResult]:
        # Precedence: LB lead ID (strongest — inherited from LB resolution),
        # then phone, then email.
        chosen: Optional[list[tuple[str, str]]] = None
        match_method: Optional[str] = None
        matched_value: Optional[str] = None

        if leadbridge_lead_id and leadbridge_lead_id in self._by_lb_lead_id:
            chosen = self._by_lb_lead_id[leadbridge_lead_id]
            match_method = MatchMethod.EXTERNAL_ID
            matched_value = leadbridge_lead_id
        elif conversation.customer_phone and conversation.customer_phone in self._by_phone:
            chosen = self._by_phone[conversation.customer_phone]
            match_method = MatchMethod.PHONE_EXACT
            matched_value = conversation.customer_phone
        elif conversation.customer_email:
            key = conversation.customer_email.lower()
            if key in self._by_email:
                chosen = self._by_email[key]
                match_method = MatchMethod.EMAIL_EXACT
                matched_value = key

        if not chosen:
            return

        for entity_type, entity_id in chosen:
            if entity_type not in _VALID_TARGET_TYPES:
                logger.warning(
                    'sf resolver: unknown entity type %r, skipping', entity_type,
                )
                continue
            yield ResolutionResult(
                target_system=self.target_system,
                target_type=entity_type,
                target_id=entity_id,
                match_method=match_method,
                confidence=1.0,
                metadata={
                    'matched_value': matched_value,
                    'lb_lead_id_provided': leadbridge_lead_id or None,
                },
            )


class HttpServiceFlowResolver(BaseResolver):
    """HTTP client for the SF /learning/entities/lookup endpoint.

    Stub until the SF-side endpoint exists. Returns no matches today.
    """

    target_system = TargetSystem.SERVICEFLOW
    ENDPOINT_PATH = '/api/v1/learning/entities/lookup'

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        workspace_id: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self._base_url = base_url or getattr(
            settings, 'SERVICEFLOW_LEARNING_URL', '',
        )
        self._token = token or getattr(
            settings, 'SERVICEFLOW_LEARNING_TOKEN', '',
        )
        self._workspace_id = workspace_id
        self._timeout = timeout

    def resolve(
        self, conversation: Conversation, *,
        leadbridge_lead_id: Optional[str] = None,
    ) -> Iterable[ResolutionResult]:
        if not self._base_url or not self._token:
            logger.debug('sf resolver: not configured (base_url/token missing)')
            return

        if not any((leadbridge_lead_id, conversation.customer_phone, conversation.customer_email)):
            return

        try:
            import requests

            payload = {
                'workspace_id': self._workspace_id,
                'identifiers': {
                    'leadbridge_lead_id': leadbridge_lead_id or None,
                    'phone_e164': conversation.customer_phone or None,
                    'email': conversation.customer_email or None,
                },
            }
            resp = requests.post(
                self._base_url.rstrip('/') + self.ENDPOINT_PATH,
                json=payload,
                headers={
                    'Authorization': f'Bearer {self._token}',
                    'X-BehaviorOS-Client': 'conversations-sf-resolver',
                    'Accept': 'application/json',
                },
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                'sf resolver: request failed (%s), treating as unmatched', exc,
            )
            return

        if resp.status_code == 404:
            logger.warning(
                'sf resolver: %s returned 404 — endpoint not yet '
                'implemented on ServiceFlow side. All conversations will '
                'be SF-unmatched until it ships. Contract in this file.',
                self._base_url + self.ENDPOINT_PATH,
            )
            return
        if resp.status_code >= 400:
            logger.warning(
                'sf resolver: %s → HTTP %d, treating as unmatched',
                self._base_url + self.ENDPOINT_PATH, resp.status_code,
            )
            return

        try:
            body = resp.json()
        except ValueError:
            logger.warning('sf resolver: response not JSON, treating as unmatched')
            return

        # Which identifier scored the lookup — used to record match_method.
        # SF's response documents this per-entity via `matched_on`, but the
        # first entity's `matched_on` also tells us which top-level
        # identifier landed the match.
        base_method: Optional[str] = None
        for entity in body.get('entities', []):
            if not isinstance(entity, dict):
                continue
            etype = entity.get('type')
            eid = entity.get('id')
            if not etype or not eid or etype not in _VALID_TARGET_TYPES:
                logger.warning(
                    'sf resolver: skipping entity with type=%r id=%r',
                    etype, eid,
                )
                continue
            if base_method is None:
                base_method = _method_from_matched_on(entity.get('matched_on'))
            method = _method_from_matched_on(entity.get('matched_on')) or base_method
            if method is None:
                # Downstream entities inherit the top-level match. If the
                # very first entity had an unknown matched_on, refuse to
                # persist rather than fabricate.
                continue
            yield ResolutionResult(
                target_system=self.target_system,
                target_type=etype,
                target_id=str(eid),
                match_method=method,
                confidence=1.0,
                metadata={
                    'matched_on': entity.get('matched_on'),
                    'source': 'serviceflow_http',
                },
            )


_VALID_TARGET_TYPES = {
    TargetType.CUSTOMER,
    TargetType.OPPORTUNITY,
    TargetType.JOB,
    TargetType.APPOINTMENT,
    TargetType.LEAD,
}


def _method_from_matched_on(matched_on: Optional[str]) -> Optional[str]:
    mapping = {
        'leadbridge_lead_id': MatchMethod.EXTERNAL_ID,
        'external_id':        MatchMethod.EXTERNAL_ID,
        'phone_e164':         MatchMethod.PHONE_EXACT,
        'email':              MatchMethod.EMAIL_EXACT,
        # Join keys (opportunity/customer/job) inherit the top-level method
        # per HttpServiceFlowResolver.resolve() logic above.
        'customer':           MatchMethod.EXTERNAL_ID,
        'opportunity':        MatchMethod.EXTERNAL_ID,
        'job':                MatchMethod.EXTERNAL_ID,
    }
    if not matched_on:
        return None
    return mapping.get(matched_on)
