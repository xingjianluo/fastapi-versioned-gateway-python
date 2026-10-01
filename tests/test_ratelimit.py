"""Per-caller rate + concurrency limits, isolation and burst correctness."""

from __future__ import annotations

import threading
import time

import anyio
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.gateway.ratelimit import RateLimitConfig
from app.main import create_app
from tests.conftest import log_judge

H = {"X-Caller-Id": "alice"}


def test_rate_limit_after_five_requests_with_readable_reason(client):
    statuses = []
    for i in range(6):
        r = client.get("/gateway/ping", headers=H)
        statuses.append(r.status_code)
        if r.status_code != 200:
            log_judge(f"request #{i + 1} rejected", {"caller": "alice"}, r)
            err = r.json()["error"]
            assert err["code"] == "rate_limited"
            assert err["details"]["limit"] == 5
            assert err["details"]["observed"] == 5
            assert r.status_code == 429
    assert statuses == [200, 200, 200, 200, 200, 429]


def test_callers_are_isolated(client):
    for _ in range(5):
        assert client.get("/gateway/ping", headers=H).status_code == 200
    blocked = client.get("/gateway/ping", headers=H)
    log_judge("alice saturated", {"caller": "alice"}, blocked)
    assert blocked.json()["error"]["code"] == "rate_limited"

    bob = client.get("/gateway/ping", headers={"X-Caller-Id": "bob"})
    log_judge("bob unaffected by alice's limit", {"caller": "bob"}, bob)
    assert bob.status_code == 200


def test_limit_recovers_after_window_slides(client):
    for _ in range(5):
        client.get("/gateway/ping", headers=H)
    rejected = client.get("/gateway/ping", headers=H)
    assert rejected.status_code == 429
    log_judge("waiting for window to slide", {"sleep": 1.05}, rejected)
    time.sleep(1.05)
    admitted = client.get("/gateway/ping", headers=H)
    log_judge("request after recovery window", {"caller": "alice"}, admitted)
    assert admitted.status_code == 200


def test_concurrent_burst_never_over_admits(client, monkeypatch):
    """200 simultaneous requests from one caller: exactly 5 are admitted.

    The middleware checks-and-reserves atomically, so under real OS threads
    the count is never under/over.
    """
    monkeypatch.setattr(
        "app.gateway.middleware.time.monotonic",
        lambda: 1000.0,  # freeze the clock: all threads land in one window
    )
    outcomes = []
    lock = threading.Lock()

    def fire():
        r = client.get("/gateway/ping", headers={"X-Caller-Id": "burst"})
        with lock:
            outcomes.append(r.status_code)

    threads = [threading.Thread(target=fire) for _ in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = outcomes.count(200)
    limited = sum(1 for s in outcomes if s == 429)
    print(f"\n[CASE] burst 200 -> admitted={ok}, rejected={limited}, "
          f"total={len(outcomes)}")
    assert ok == 5, outcomes
    assert limited == 195
    # every rejection is the same distinguishable reason
    assert len(outcomes) == 200


def test_concurrency_cap_distinguishable_from_rate():
    """Three overlapping slow requests with max_concurrent=2: third -> 429
    concurrency_limited (a code distinct from rate_limited), while the rate
    budget (1000/s) is nowhere near exhausted."""
    import asyncio

    app2 = create_app(rate_limit=RateLimitConfig(rate_per_second=1000,
                                                 max_concurrent=2))
    app2.state.rate_limiter.reset()

    async def slow_entry(request: Request):
        await anyio.sleep(0.4)
        return JSONResponse({"ok": True})

    app2.add_api_route("/gateway/slow", slow_entry, methods=["GET"])

    async def scenario():
        transport = httpx.ASGITransport(app=app2)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            return await asyncio.gather(*[
                c.get("/gateway/slow", headers={"X-Caller-Id": "slow"})
                for _ in range(3)
            ])

    responses = asyncio.run(scenario())
    statuses = sorted(r.status_code for r in responses)
    print(f"\n[CASE] concurrency cap -> statuses={statuses}")
    for r in responses:
        if r.status_code != 200:
            log_judge("rejected by concurrency cap",
                      {"max_concurrent": 2, "rate": 1000}, r)
            assert r.json()["error"]["code"] == "concurrency_limited"
            assert r.json()["error"]["details"]["observed"] == 2
    assert statuses == [200, 200, 429]
