"""Health check endpoint.

Two flavors:
- GET /api/health/       → 200 as long as the process is running.
                           Railway probes this; it must not touch the DB
                           so a temporary DB blip doesn't kill the pod.
- GET /api/health/deep/  → same, plus DB + Redis reachability. Useful
                           for oncall + smoke tests; DO NOT wire Railway
                           health checks to this — a Redis outage would
                           put the app into an unrecoverable restart loop.
"""

from __future__ import annotations

import logging

from django.db import connections
from django.db.utils import OperationalError
from django.http import JsonResponse

logger = logging.getLogger(__name__)


def health(_request):
    return JsonResponse({'status': 'ok'})


def deep_health(_request):
    checks: dict[str, str] = {}
    ok = True

    try:
        connections['default'].cursor().execute('SELECT 1')
        checks['database'] = 'ok'
    except OperationalError as exc:
        checks['database'] = f'error: {exc}'
        ok = False

    try:
        from django.core.cache import caches  # noqa: WPS433
        # If a cache backend is configured, ping it; otherwise skip.
        default_cache = caches['default']
        default_cache.set('healthcheck', '1', timeout=1)
        default_cache.get('healthcheck')
        checks['cache'] = 'ok'
    except Exception as exc:
        # Cache is optional in Phase 1 — treat as informational.
        checks['cache'] = f'skipped: {exc}'

    payload = {'status': 'ok' if ok else 'degraded', 'checks': checks}
    return JsonResponse(payload, status=200 if ok else 503)
