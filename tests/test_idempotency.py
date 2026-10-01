"""Idempotency: replays, key conflicts, concurrent dedupe, no double effects."""

from __future__ import annotations

import asyncio
import threading

import httpx

from app.gateway.routes import SIDE_EFFECTS
from tests.conftest import log_judge

HEADERS = {"X-Caller-Id": "idem-user", "Idempotency-Key": "order-001"}


def test_repeated_submission_replays_first_result(client):
    r1 = client.post("/gateway/orders", headers=HEADERS, json={"item": "book"})
    r2 = client.post("/gateway/orders", headers=HEADERS, json={"item": "book"})
    r3 = client.post("/gateway/orders", headers=HEADERS, json={"item": "book"})
    log_judge("first submission", {"body": {"item": "book"}}, r1)
    log_judge("second submission (same key+body)", {"body": {"item": "book"}}, r2)

    assert r1.status_code == r2.status_code == r3.status_code == 201
    assert r1.json() == r2.json() == r3.json()
    assert r1.headers["x-idempotency-replayed"] == "false"
    assert r2.headers["x-idempotency-replayed"] == "true"
    assert r3.headers["x-idempotency-replayed"] == "true"
    assert len(SIDE_EFFECTS) == 1, SIDE_EFFECTS
    print(f"[CASE] side effects after 3 identical submissions: "
          f"{len(SIDE_EFFECTS)} (must be 1)")


def test_same_key_different_payload_is_rejected_with_reason(client):
    client.post("/gateway/orders", headers=HEADERS, json={"item": "book"})
    conflict = client.post("/gateway/orders", headers=HEADERS,
                           json={"item": "entirely-different"})
    log_judge("key reused for different payload",
              {"original": {"item": "book"},
               "conflict": {"item": "entirely-different"}}, conflict)
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "idempotency_conflict"
    assert err["details"]["idempotency_key"] == "order-001"
    assert err["details"]["original_fingerprint"] != \
        err["details"]["conflicting_fingerprint"]
    # conflict must not add a side effect
    assert len(SIDE_EFFECTS) == 1


def test_keys_are_scoped_per_caller(client):
    client.post("/gateway/orders", headers=HEADERS, json={"item": "book"})
    other = client.post(
        "/gateway/orders",
        headers={"X-Caller-Id": "someone-else", "Idempotency-Key": "order-001"},
        json={"item": "book"},
    )
    log_judge("same key used by a different caller",
              {"caller": "someone-else"}, other)
    assert other.status_code == 201
    assert other.headers["x-idempotency-replayed"] == "false"
    assert len(SIDE_EFFECTS) == 2


def test_concurrent_identical_submissions_single_side_effect():
    """N threads racing with one key: exactly one side effect, all get the
    same first result (concurrent submissions wait for the first outcome)."""
    from app.gateway.ratelimit import RateLimitConfig
    from app.main import create_app

    # High limits so the limiter never interferes with this scenario.
    app = create_app(rate_limit=RateLimitConfig(rate_per_second=10_000,
                                                max_concurrent=10_000))
    app.state.idempotency_store.reset()
    app.state.rate_limiter.reset()
    from app.gateway.routes import reset_side_effects
    reset_side_effects()

    results: list[dict] = []
    statuses: list[int] = []
    lock = threading.Lock()

    def fire():
        # Every thread gets its own TestClient but shares the single app/store.
        from fastapi.testclient import TestClient
        c = TestClient(app)
        r = c.post("/gateway/orders",
                   headers={"X-Caller-Id": "racer",
                            "Idempotency-Key": "race-key-1"},
                   json={"item": "phone"})
        with lock:
            statuses.append(r.status_code)
            results.append(r.json())

    threads = [threading.Thread(target=fire) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"\n[CASE] 10 concurrent same-key posts -> statuses={statuses}, "
          f"side_effects={len(SIDE_EFFECTS)}")
    assert statuses == [201] * 10
    assert all(body == results[0] for body in results), results
    assert len(SIDE_EFFECTS) == 1, SIDE_EFFECTS
    assert SIDE_EFFECTS[0]["payload"] == {"item": "phone"}


def test_idempotency_consistent_across_entry_points(client):
    h_order = {"X-Caller-Id": "multi", "Idempotency-Key": "shared-k"}
    h_pay = {"X-Caller-Id": "multi", "Idempotency-Key": "shared-k"}
    o1 = client.post("/gateway/orders", headers=h_order, json={"item": "x"})
    # Same key reused against a DIFFERENT entry but identical body: the store
    # keys on (caller, idempotency-key) regardless of the entry, therefore the
    # first outcome is replayed (same order_id) rather than a new side effect.
    o2 = client.post("/gateway/payments", headers=h_pay, json={"item": "x"})
    log_judge("same key across /orders and /payments, identical body",
              {"entries": ["/gateway/orders", "/gateway/payments"]}, o2)
    assert o2.status_code == 201
    assert o2.json()["order_id"] == o1.json()["order_id"]
    assert o2.headers["x-idempotency-replayed"] == "true"
    assert len(SIDE_EFFECTS) == 1


def test_concurrent_conflict_and_replay_via_asgi():
    """Concurrent same-key requests with DIFFERENT payloads: exactly one wins,
    every conflicting one gets 409, the matching one replays."""
    from app.gateway.ratelimit import RateLimitConfig
    from app.main import create_app

    app = create_app(rate_limit=RateLimitConfig(rate_per_second=10_000,
                                                max_concurrent=10_000))
    app.state.idempotency_store.reset()
    app.state.rate_limiter.reset()
    from app.gateway.routes import reset_side_effects
    reset_side_effects()

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            async def post(payload):
                return await c.post(
                    "/gateway/orders",
                    headers={"X-Caller-Id": "race2",
                             "Idempotency-Key": "race-key-2"},
                    json=payload,
                )
            return await asyncio.gather(
                post({"item": "A"}),
                post({"item": "A"}),  # identical -> replay
                post({"item": "B"}),  # different -> conflict
            )

    responses = asyncio.run(scenario())
    codes = sorted(
        (r.status_code,
         r.json().get("error", {}).get("code", "ok"),
         r.json().get("order_id"))
        for r in responses
    )
    print(f"\n[CASE] concurrent mixed submissions -> {codes}, "
          f"side_effects={len(SIDE_EFFECTS)}")
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 201, 409], statuses
    conflict = next(r for r in responses if r.status_code == 409)
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    oks = [r for r in responses if r.status_code == 201]
    assert oks[0].json()["order_id"] == oks[1].json()["order_id"]
    assert len(SIDE_EFFECTS) == 1
