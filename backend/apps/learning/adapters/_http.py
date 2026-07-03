"""Shared HTTP + fixture loading helpers.

Kept intentionally small — adapters that need bespoke pagination or auth
schemes can bypass these helpers.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

FIXTURES_DIR = Path(__file__).resolve().parent / 'fixtures'

DEFAULT_TIMEOUT_SECONDS = 15


def load_fixture(filename: str) -> list[dict[str, Any]]:
    """Load a list-of-dicts fixture from adapters/fixtures/.

    Fixtures let Phase 1 ship + test end-to-end before source systems
    expose HTTP endpoints. Once a source has a real endpoint, the adapter
    configures its URL/token env vars and the fixture path becomes dead
    code (still useful for local dev + tests).
    """
    path = FIXTURES_DIR / filename
    with path.open(encoding='utf-8') as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f'Fixture {filename} must be a JSON list, got {type(data).__name__}')
    return data


def http_fetch_list(
    url: str,
    token: str,
    since: datetime | None,
) -> list[dict[str, Any]]:
    """Simple GET → JSON list, single page, service-token auth.

    Real pagination + retry + 429 handling get added per-adapter once we
    know each source's actual response shape. This helper is deliberately
    minimal — do not build abstractions here.
    """
    params: dict[str, str] = {}
    if since is not None:
        params['since'] = since.isoformat()
    response = requests.get(
        url,
        params=params,
        headers={
            'Authorization': f'Bearer {token}',
            'X-BehaviorOS-Client': 'learning-engine',
        },
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError(f'Expected JSON list from {url}, got {type(data).__name__}')
    return data
