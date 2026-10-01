"""Idempotency: duplicate submissions replay the first response with no
duplicate side effects; key reuse with a different payload is rejected;
concurrent duplicates execute exactly once."""

import logging
import threading

from fastapi.testclient import TestClient

from app.gateway import create_app
from app.ratelimit import RateLimitConfig, RateLimiter

logger = logging.getLogger("test.idempotency")

CALLER = {"X-Caller-Id": "writer"}


def test_duplicate_submission_replays_first_response(app):
    client = TestClient(app)
    first = client.post("/items", json={"name": "widget"},
                        headers={**CALLER, "Idempotency-Key": "k-1"})
    dupe = client.post("/items", json={"name": "widget"},
                       headers={**CALLER, "Idempotency-Key": "k-1"})
    logger.info("first -> %s %s replayed=%s; duplicate -> %s %s replayed=%s",
                first.status_code, first.json(), first.headers.get("idempotency-replayed"),
                dupe.status_code, dupe.json(), dupe.headers.get("idempotency-replayed"))
    assert first.status_code == 201
    assert dupe.status_code == 201
    assert dupe.json() == first.json()
    assert first.headers["idempotency-replayed"] == "false"
    assert dupe.headers["idempotency-replayed"] == "true"
    # No duplicate side effect: exactly one item exists.
    items = client.get("/items").json()["items"]
    logger.info("side-effect check: items=%s", items)
    assert len(items) == 1


def test_key_reuse_with_different_payload_is_conflict(app):
    client = TestClient(app)
    first = client.post("/items", json={"name": "widget"},
                        headers={**CALLER, "Idempotency-Key": "k-2"})
    conflict = client.post("/items", json={"name": "gadget"},
                           headers={**CALLER, "Idempotency-Key": "k-2"})
    logger.info("same key, different payload -> %s code=%s",
                conflict.status_code, conflict.json()["error"]["code"])
    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert "k-2" in conflict.json()["error"]["message"]
    # The conflicting attempt created nothing.
    assert len(client.get("/items").json()["items"]) == 1


def test_conflict_reason_distinguishable_from_other_failures(app):
    client = TestClient(app)
    client.post("/items", json={"name": "a"}, headers={**CALLER, "Idempotency-Key": "k-3"})
    conflict = client.post("/items", json={"name": "b"}, headers={**CALLER, "Idempotency-Key": "k-3"})
    sunset = client.get("/items", headers={"X-API-Version": "v0"})
    logger.info("conflict=(%s,%s) sunset=(%s,%s)",
                conflict.status_code, conflict.json()["error"]["code"],
                sunset.status_code, sunset.json()["error"]["code"])
    assert conflict.status_code != sunset.status_code
    assert conflict.json()["error"]["code"] != sunset.json()["error"]["code"]


def test_concurrent_duplicates_execute_once(registry, store):
    """10 threads post the same key+payload simultaneously: exactly one
    execution, all callers get the identical item, one side effect."""
    # High concurrency cap so this test isolates idempotency, not limiting.
    limiter = RateLimiter(RateLimitConfig(max_requests=100, window_seconds=60.0, max_concurrent=100))
    app = create_app(registry=registry, limiter=limiter, store=store)
    n = 10
    barrier = threading.Barrier(n)
    results: list[tuple[int, dict, str]] = [None] * n

    def worker(i: int) -> None:
        client = TestClient(app)
        barrier.wait()
        resp = client.post("/items", json={"name": "burst"},
                           headers={**CALLER, "Idempotency-Key": "k-burst"})
        results[i] = (resp.status_code, resp.json(), resp.headers.get("idempotency-replayed"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = [r[0] for r in results]
    bodies = [r[1] for r in results]
    replayed = [r[2] for r in results]
    logger.info("input: %d concurrent identical posts -> statuses=%s replayed=%s bodies=%s",
                n, statuses, replayed, bodies)
    assert statuses == [201] * n
    assert all(b == bodies[0] for b in bodies)
    assert replayed.count("false") == 1
    assert replayed.count("true") == n - 1
    items = TestClient(app).get("/items").json()["items"]
    logger.info("side-effect check: items=%s", items)
    assert len(items) == 1


def test_same_key_different_callers_are_independent(app):
    client = TestClient(app)
    r1 = client.post("/items", json={"name": "x"},
                     headers={"X-Caller-Id": "c1", "Idempotency-Key": "shared"})
    r2 = client.post("/items", json={"name": "x"},
                     headers={"X-Caller-Id": "c2", "Idempotency-Key": "shared"})
    logger.info("caller c1 -> %s id=%s; caller c2 -> %s id=%s",
                r1.status_code, r1.json().get("id"), r2.status_code, r2.json().get("id"))
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]
