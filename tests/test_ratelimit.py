"""Rate limiting: exact counting under concurrent bursts, per-caller
isolation, distinguishable 429 reasons, and recovery after the window."""

import logging
import threading
import time

from fastapi.testclient import TestClient

from app.gateway import create_app
from app.ratelimit import LimitKind, RateLimitConfig, RateLimiter

logger = logging.getLogger("test.ratelimit")


def _fire_concurrently(app, caller: str, n: int) -> list[int]:
    """Fire n requests from n threads; return their status codes."""
    barrier = threading.Barrier(n)
    statuses: list[int] = [0] * n

    def worker(i: int) -> None:
        client = TestClient(app)
        barrier.wait()
        resp = client.get("/items", headers={"X-Caller-Id": caller})
        statuses[i] = resp.status_code

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return statuses


def test_concurrent_burst_counts_exactly(registry, store):
    """30 simultaneous requests against a limit of 5: exactly 5 pass,
    exactly 25 are rejected — no lost or double counting."""
    limiter = RateLimiter(RateLimitConfig(max_requests=5, window_seconds=60.0, max_concurrent=100))
    app = create_app(registry=registry, limiter=limiter, store=store)
    statuses = _fire_concurrently(app, "burst-caller", 30)
    ok, rejected = statuses.count(200), statuses.count(429)
    logger.info("input: 30 concurrent requests, max_requests=5 -> ok=%s rejected=%s",
                ok, rejected)
    assert ok == 5
    assert rejected == 25


def test_rate_limit_reason_is_readable_and_distinct(app):
    client = TestClient(app)
    headers = {"X-Caller-Id": "chatty"}
    statuses = [client.get("/items", headers=headers).status_code for _ in range(5)]
    resp = client.get("/items", headers=headers)
    logger.info("input: 6th request in window -> status=%s code=%s retry_after=%s",
                resp.status_code, resp.json()["error"]["code"],
                resp.headers.get("retry-after"))
    assert statuses == [200] * 5
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"
    assert "chatty" in resp.json()["error"]["message"]
    assert resp.headers.get("retry-after") is not None


def test_callers_are_isolated(app):
    client = TestClient(app)
    for _ in range(5):
        assert client.get("/items", headers={"X-Caller-Id": "a"}).status_code == 200
    blocked = client.get("/items", headers={"X-Caller-Id": "a"})
    other = client.get("/items", headers={"X-Caller-Id": "b"})
    anon = client.get("/items")
    logger.info("caller a (exhausted) -> %s; caller b -> %s; anonymous -> %s",
                blocked.status_code, other.status_code, anon.status_code)
    assert blocked.status_code == 429
    assert other.status_code == 200
    assert anon.status_code == 200


def test_recovery_after_window_rollover(registry, store):
    limiter = RateLimiter(RateLimitConfig(max_requests=2, window_seconds=0.2, max_concurrent=10))
    app = create_app(registry=registry, limiter=limiter, store=store)
    client = TestClient(app)
    headers = {"X-Caller-Id": "roller"}
    assert client.get("/items", headers=headers).status_code == 200
    assert client.get("/items", headers=headers).status_code == 200
    blocked = client.get("/items", headers=headers)
    logger.info("inside window: 3rd request -> %s", blocked.status_code)
    assert blocked.status_code == 429
    time.sleep(0.25)
    recovered = client.get("/items", headers=headers)
    logger.info("after window rollover: -> %s", recovered.status_code)
    assert recovered.status_code == 200


def test_concurrency_cap_and_release():
    """In-flight cap rejects with a distinct reason; releasing a slot
    re-admits the caller."""
    limiter = RateLimiter(RateLimitConfig(max_requests=100, window_seconds=60.0, max_concurrent=2))
    d1 = limiter.acquire("c")
    d2 = limiter.acquire("c")
    d3 = limiter.acquire("c")
    logger.info("decisions: %s %s %s (kind=%s)",
                d1.allowed, d2.allowed, d3.allowed, d3.kind)
    assert d1.allowed and d2.allowed
    assert not d3.allowed and d3.kind is LimitKind.CONCURRENCY
    limiter.release("c")
    d4 = limiter.acquire("c")
    logger.info("after release: allowed=%s", d4.allowed)
    assert d4.allowed


def test_concurrency_reason_via_http(registry, store):
    """Concurrency rejections surface as 429 concurrency_limited, which is
    distinguishable from rate_limited."""
    limiter = RateLimiter(RateLimitConfig(max_requests=100, window_seconds=60.0, max_concurrent=1))
    app = create_app(registry=registry, limiter=limiter, store=store)
    # Occupy the single in-flight slot directly, then hit the HTTP layer.
    assert limiter.acquire("busy").allowed
    client = TestClient(app)
    resp = client.get("/items", headers={"X-Caller-Id": "busy"})
    logger.info("input: request while slot occupied -> status=%s code=%s",
                resp.status_code, resp.json()["error"]["code"])
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "concurrency_limited"
    limiter.release("busy")
