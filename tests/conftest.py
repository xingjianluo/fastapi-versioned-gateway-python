"""Shared fixtures and test logging helpers.

Every key request/decision is printed so failure causes are explainable from
the unit-test logs (run ``pytest -s`` to see them).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.gateway.ratelimit import RateLimitConfig
from app.main import create_app


def log_judge(title: str, request_desc: dict, response) -> None:
    """Print the key inputs and the gateway's decision basis for one case."""
    body = None
    try:
        body = response.json()
    except Exception:
        body = response.text
    print(f"\n[CASE] {title}")
    print(f"  input     : {request_desc}")
    print(f"  status    : {response.status_code}")
    print(f"  gw-version: {response.headers.get('x-gateway-api-version')} "
          f"(source={response.headers.get('x-gateway-version-source')}, "
          f"deprecated={response.headers.get('x-gateway-version-deprecation')})")
    print(f"  body      : {body}")


@pytest.fixture
def client():
    app = create_app(rate_limit=RateLimitConfig(rate_per_second=5,
                                               max_concurrent=2))
    app.state.idempotency_store.reset()
    app.state.rate_limiter.reset()
    from app.gateway.routes import reset_side_effects
    reset_side_effects()
    with TestClient(app) as c:
        yield c
