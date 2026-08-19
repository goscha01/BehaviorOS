"""LeadBridge entity resolver.

Two backends today:

1. **InMemoryLeadBridgeResolver** — a plain dict-backed store used by
   tests + local dev. Callers pre-populate leads with known IDs / phones
   / emails, then run resolution.

2. **HttpLeadBridgeResolver** — a stub client for the LB read-only
   lookup endpoint. **The endpoint does not exist yet.** This class
   documents the exact request/response contract that Phase 9 (or a
   later LB-side task) needs to implement. Until it exists, the HTTP
   backend returns no matches for every call and logs a WARN so ops
   can see resolution silently degraded to unmatched.

Required LeadBridge endpoint (not yet implemented on LB side):

    POST /api/v1/learning/leads/lookup
    Authorization: Bearer <BEHAVIOR_LB_LEARNING_TOKEN>
    Content-Type: application/json

    Body:
      {
        "workspace_id": "<lb-workspace-uuid>",  # optional if scoped-by-token
        "identifiers": {
          "external_id":  "<optional stable ID from source system>",
          "phone_e164":   "+18135551234",
          "email":        "customer@example.com"
        }
      }

    Response 200:
      {
        "leads": [
          {"id": "<lb-lead-uuid>", "matched_on": "phone_e164"},
          ...
        ]
      }
      — empty `leads` list means no match. Never a 404 for "not found."

    Response 401/403: token invalid / not authorized for workspace
    Response 5xx: LB itself failed; caller treats as infrastructure error

Deterministic matching priority (LB side must enforce this order):
  external_id > phone_e164 > email > (no match)

BehaviorOS-side priority is the SAME — we call the endpoint with all
identifiers and trust the returned `matched_on` to record provenance.
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


class InMemoryLeadBridgeResolver(BaseResolver):
    """Test / dev backend. Callers populate leads before calling resolve()."""

    target_system = TargetSystem.LEADBRIDGE

    def __init__(self):
        # Each store is keyed by the identifier value (already normalized
        # to E.164 / lowercased email / raw external_id).
        self._by_external_id: dict[str, str] = {}
        self._by_phone: dict[str, str] = {}
        self._by_email: dict[str, str] = {}

    def register_lead(
        self,
        lead_id: str,
        *,
        external_id: Optional[str] = None,
        phone_e164: Optional[str] = None,
        email: Optional[str] = None,
    ) -> None:
        if external_id:
            self._by_external_id[external_id] = lead_id
        if phone_e164:
            self._by_phone[phone_e164] = lead_id
        if email:
            self._by_email[email.lower()] = lead_id

    def resolve(self, conversation: Conversation) -> Iterable[ResolutionResult]:
        # Deterministic priority: external_id, phone, email.
        external_id = _extract_external_id(conversation)
        if external_id and external_id in self._by_external_id:
            yield ResolutionResult(
                target_system=self.target_system,
                target_type=TargetType.LEAD,
                target_id=self._by_external_id[external_id],
                match_method=MatchMethod.EXTERNAL_ID,
                confidence=1.0,
                metadata={'matched_value': external_id},
            )
            return

        if conversation.customer_phone and conversation.customer_phone in self._by_phone:
            yield ResolutionResult(
                target_system=self.target_system,
                target_type=TargetType.LEAD,
                target_id=self._by_phone[conversation.customer_phone],
                match_method=MatchMethod.PHONE_EXACT,
                confidence=1.0,
                metadata={'matched_value': conversation.customer_phone},
            )
            return

        if conversation.customer_email:
            key = conversation.customer_email.lower()
            if key in self._by_email:
                yield ResolutionResult(
                    target_system=self.target_system,
                    target_type=TargetType.LEAD,
                    target_id=self._by_email[key],
                    match_method=MatchMethod.EMAIL_EXACT,
                    confidence=1.0,
                    metadata={'matched_value': key},
                )
                return

        # No deterministic match. Return nothing — the caller records the
        # conversation as unmatched. We never fabricate a fuzzy match.


class HttpLeadBridgeResolver(BaseResolver):
    """HTTP client for the LB /learning/leads/lookup endpoint.

    Stub until Phase 9 (or a follow-up on the LB side) implements the
    endpoint. Today it returns no matches and logs a warning.
    """

    target_system = TargetSystem.LEADBRIDGE
    ENDPOINT_PATH = '/api/v1/learning/leads/lookup'

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        workspace_id: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self._base_url = base_url or getattr(
            settings, 'LEADBRIDGE_LEARNING_URL', '',
        )
        self._token = token or getattr(
            settings, 'LEADBRIDGE_LEARNING_TOKEN', '',
        )
        self._workspace_id = workspace_id
        self._timeout = timeout

    def resolve(self, conversation: Conversation) -> Iterable[ResolutionResult]:
        if not self._base_url or not self._token:
            logger.debug(
                'leadbridge resolver: not configured (base_url/token missing)'
            )
            return

        external_id = _extract_external_id(conversation)
        if not any((external_id, conversation.customer_phone, conversation.customer_email)):
            return

        try:
            import requests

            payload = {
                'workspace_id': self._workspace_id,
                'identifiers': {
                    'external_id': external_id or None,
                    'phone_e164': conversation.customer_phone or None,
                    'email': conversation.customer_email or None,
                },
            }
            resp = requests.post(
                self._base_url.rstrip('/') + self.ENDPOINT_PATH,
                json=payload,
                headers={
                    'Authorization': f'Bearer {self._token}',
                    'X-BehaviorOS-Client': 'conversations-lb-resolver',
                    'Accept': 'application/json',
                },
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — resolver treats any HTTP failure as unmatched
            logger.warning(
                'leadbridge resolver: request failed (%s), treating as unmatched',
                exc,
            )
            return

        if resp.status_code == 404:
            # Endpoint does not exist yet — this is the current state.
            # Log once per call so ops see we're running against a stub.
            logger.warning(
                'leadbridge resolver: %s returned 404 — endpoint not yet '
                'implemented on LB side. All conversations will be unmatched '
                'until Phase 9 ships. Contract in this file.',
                self._base_url + self.ENDPOINT_PATH,
            )
            return
        if resp.status_code >= 400:
            logger.warning(
                'leadbridge resolver: %s → HTTP %d, treating as unmatched',
                self._base_url + self.ENDPOINT_PATH, resp.status_code,
            )
            return

        try:
            body = resp.json()
        except ValueError:
            logger.warning('leadbridge resolver: response not JSON, treating as unmatched')
            return

        for lead in body.get('leads', []):
            if not isinstance(lead, dict) or 'id' not in lead:
                continue
            method = _method_from_matched_on(lead.get('matched_on'))
            if method is None:
                # Unknown match method — refuse to persist. Deterministic
                # matching is the whole point; if LB sends something we
                # don't recognize, treat as unmatched rather than guess.
                logger.warning(
                    'leadbridge resolver: unknown matched_on=%r for lead %s, skipping',
                    lead.get('matched_on'), lead.get('id'),
                )
                continue
            yield ResolutionResult(
                target_system=self.target_system,
                target_type=TargetType.LEAD,
                target_id=str(lead['id']),
                match_method=method,
                confidence=1.0,
                metadata={
                    'matched_on': lead.get('matched_on'),
                    'source': 'leadbridge_http',
                },
            )


def _extract_external_id(conversation: Conversation) -> Optional[str]:
    """Some source systems (LeadBridge itself, but also Quo contact sync)
    stamp a `lead_id` or `external_id` into the conversation metadata.
    This is the strongest deterministic signal we have — check it first.
    """
    md = conversation.metadata or {}
    for key in ('lead_id', 'leadbridge_lead_id', 'external_lead_id'):
        val = md.get(key)
        if val:
            return str(val)
    # Also inspect the raw payload if present.
    raw = md.get('raw') or {}
    if isinstance(raw, dict):
        for key in ('lead_id', 'leadbridge_lead_id', 'externalId'):
            val = raw.get(key)
            if val:
                return str(val)
    return None


def _method_from_matched_on(matched_on: Optional[str]) -> Optional[str]:
    mapping = {
        'external_id': MatchMethod.EXTERNAL_ID,
        'phone_e164':  MatchMethod.PHONE_EXACT,
        'email':       MatchMethod.EMAIL_EXACT,
    }
    if not matched_on:
        return None
    return mapping.get(matched_on)
