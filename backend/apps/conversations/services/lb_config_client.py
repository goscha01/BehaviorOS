"""HTTP client for `GET /api/v1/learning/config` on the LeadBridge
learning boundary (Pipeline 1B-4).

Small dedicated client — the lead-scoped `lb_learning_client` doesn't
need to grow. Reuses the same `LEADBRIDGE_LEARNING_URL` +
`LEADBRIDGE_LEARNING_TOKEN` env vars we already use for ingestion.

Returns the raw response body verbatim (as `dict`). Snapshot storage +
content-hash dedup is the caller's job (see
`fetch_lb_config` management command).
"""

from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)


class LbConfigClient:
    ENDPOINT_PATH = '/api/v1/learning/config'

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self._base_url = (
            base_url or getattr(settings, 'LEADBRIDGE_LEARNING_URL', '')
        ).rstrip('/')
        self._token = token or getattr(settings, 'LEADBRIDGE_LEARNING_TOKEN', '')
        self._timeout = timeout

    @property
    def endpoint(self) -> str:
        return f'{self._base_url}{self.ENDPOINT_PATH}'

    def fetch(
        self, *, tenant_id: str, service_group: Optional[str] = None,
    ) -> dict:
        """Fetch the tenant's effective configuration.

        Raises RuntimeError on non-200 responses so the caller command
        can surface the error message to the operator rather than
        writing a partial snapshot.
        """
        if not (self._base_url and self._token):
            raise RuntimeError(
                'lb config client: LEADBRIDGE_LEARNING_URL and '
                'LEADBRIDGE_LEARNING_TOKEN must be set'
            )
        import requests
        headers = {
            'Authorization': f'Bearer {self._token}',
            'X-BehaviorOS-Client': 'conversations-lb-config-client',
            'Accept': 'application/json',
        }
        params = {'tenantId': tenant_id}
        if service_group:
            params['serviceGroup'] = service_group
        r = requests.get(
            self.endpoint, headers=headers, params=params,
            timeout=self._timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f'lb config fetch failed: HTTP {r.status_code} '
                f'body={r.text[:500]}'
            )
        body = r.json()
        # Sanity: contract_version presence is our forward-compat guard.
        cv = body.get('contract_version')
        if cv != 'v1':
            raise RuntimeError(
                f'lb config contract_version mismatch: expected v1, got {cv!r}. '
                f'Refusing to persist — bump the BehaviorOS-side parser first.'
            )
        return body
