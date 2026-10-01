import logging

import pytest

from app.gateway import create_app
from app.idempotency import IdempotencyStore
from app.ratelimit import RateLimitConfig, RateLimiter
from app.versioning import default_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@pytest.fixture
def registry():
    return default_registry()


@pytest.fixture
def limiter():
    return RateLimiter(RateLimitConfig(max_requests=5, window_seconds=60.0, max_concurrent=3))


@pytest.fixture
def store():
    return IdempotencyStore()


@pytest.fixture
def app(registry, limiter, store):
    return create_app(registry=registry, limiter=limiter, store=store)
