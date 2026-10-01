"""Cross-cutting contract: caller identity, envelope consistency, replay cost."""

from __future__ import annotations

from tests.conftest import log_judge


def test_missing_caller_is_rejected_distinctly(client):
    resp = client.get("/gateway/ping")
    log_judge("anonymous request", {"headers": {}}, resp)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "caller_missing"
    assert "x-caller-id" in resp.json()["error"]["message"]


def test_non_gateway_paths_are_not_guarded(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers.get("x-gateway-api-version") is None


def test_all_rejection_reasons_are_distinguishable(client):
    cases = []
    r = client.get("/gateway/ping")
    cases.append(("caller_missing", r))
    r = client.get("/gateway/ping",
                   headers={"X-Caller-Id": "c", "X-API-Version": "2023-01-01"})
    cases.append(("version_retired", r))
    r = client.get("/gateway/ping",
                   headers={"X-Caller-Id": "c", "X-API-Version": "nope"})
    cases.append(("version_unknown", r))
    for i in range(5):
        client.get("/gateway/ping", headers={"X-Caller-Id": "rl"})
    r = client.get("/gateway/ping", headers={"X-Caller-Id": "rl"})
    cases.append(("rate_limited", r))
    client.post("/gateway/orders",
                headers={"X-Caller-Id": "ic", "Idempotency-Key": "k"},
                json={"a": 1})
    r = client.post("/gateway/orders",
                    headers={"X-Caller-Id": "ic", "Idempotency-Key": "k"},
                    json={"a": 2})
    cases.append(("idempotency_conflict", r))

    codes = [resp.json()["error"]["code"] for _, resp in cases]
    print(f"\n[CASE] rejection codes across scenarios: {codes}")
    assert codes == ["caller_missing", "version_retired", "version_unknown",
                     "rate_limited", "idempotency_conflict"]
    for resp in cases:
        body = resp[1].json()["error"]
        assert set(body) == {"code", "message", "details"}
        assert body["message"]


def test_idempotent_replay_does_not_consume_rate_budget():
    """A replay is a gateway-level cache hit: it short-circuits before rate
    acquisition, so a saturated rate window must not block replays."""
    from fastapi.testclient import TestClient

    from app.gateway.ratelimit import RateLimitConfig
    from app.main import create_app

    app2 = create_app(rate_limit=RateLimitConfig(rate_per_second=5,
                                                 max_concurrent=5))
    with TestClient(app2) as c2:
        h = {"X-Caller-Id": "z", "Idempotency-Key": "z-1"}
        first = c2.post("/gateway/orders", headers=h, json={"item": "lamp"})
        assert first.status_code == 201

        # Saturate the caller's sliding window with ordinary traffic.
        for _ in range(5):
            c2.get("/gateway/ping", headers={"X-Caller-Id": "z"})
        blocked = c2.get("/gateway/ping", headers={"X-Caller-Id": "z"})
        assert blocked.status_code == 429

        # Replay still succeeds despite the saturated window.
        replay = c2.post("/gateway/orders", headers=h, json={"item": "lamp"})
        log_judge("replay while rate-saturated", {"window": "saturated"},
                  replay)
        assert replay.status_code == 201
        assert replay.headers["x-idempotency-replayed"] == "true"
        assert replay.json()["order_id"] == first.json()["order_id"]
