"""Version negotiation: defaulting, deprecation flags, distinct rejections."""

from tests.conftest import log_judge

CALLER = {"X-Caller-Id": "version-tester"}


def test_undeclared_version_uses_stable_default(client):
    resp = client.get("/gateway/ping", headers=CALLER)
    log_judge("no version header -> stable default", {"headers": CALLER}, resp)
    assert resp.status_code == 200
    assert resp.json()["api_version"] == "2025-01-01"
    assert resp.headers["x-gateway-version-source"] == "default"


def test_stable_version_accepted_without_default_marker(client):
    headers = {**CALLER, "X-API-Version": "2025-01-01"}
    resp = client.get("/gateway/ping", headers=headers)
    log_judge("stable version explicitly declared", {"headers": headers}, resp)
    assert resp.status_code == 200
    assert resp.headers["x-gateway-api-version"] == "2025-01-01"
    assert "x-gateway-version-source" not in resp.headers
    assert "x-gateway-version-deprecation" not in resp.headers


def test_deprecated_version_still_served_but_flagged(client):
    headers = {**CALLER, "X-API-Version": "2024-01-01"}
    resp = client.get("/gateway/ping", headers=headers)
    log_judge("deprecated version accepted + flagged", {"headers": headers}, resp)
    assert resp.status_code == 200
    assert resp.json()["api_version"] == "2024-01-01"
    assert "x-gateway-version-deprecation" in resp.headers
    # deprecation flag must also appear on the rejection envelope's headers
    bad = client.post(
        "/gateway/orders",
        headers={**headers, "Idempotency-Key": "dep-1"},
        json={"item": "book"},
    )
    log_judge("deprecated version on a write still flags the header",
              {"version": "2024-01-01"}, bad)
    assert bad.status_code == 201
    assert "x-gateway-version-deprecation" in bad.headers


def test_retired_version_rejected_with_distinct_code(client):
    headers = {**CALLER, "X-API-Version": "2023-01-01"}
    resp = client.get("/gateway/ping", headers=headers)
    log_judge("retired version must be refused (not fallback)",
              {"headers": headers}, resp)
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "version_retired"
    assert resp.headers.get("x-gateway-api-version") is None
    detail = resp.json()["error"]["details"]
    assert detail["requested_version"] == "2023-01-01"
    assert detail["default_version"] == "2025-01-01"


def test_unknown_version_rejected_and_never_falls_back(client):
    headers = {**CALLER, "X-API-Version": "2099-01-01"}
    resp = client.get("/gateway/ping", headers=headers)
    log_judge("unknown version must not silently fall back",
              {"headers": headers}, resp)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "version_unknown"
    assert err["code"] != "version_retired"  # reasons must be distinguishable
    assert "2025-01-01" in err["details"]["known_versions"]
    assert resp.headers.get("x-gateway-api-version") is None


def test_garbage_version_string_is_unknown_not_default(client):
    headers = {**CALLER, "X-API-Version": "banana"}
    resp = client.get("/gateway/ping", headers=headers)
    log_judge("garbage version -> unknown, no default fallback",
              {"headers": headers}, resp)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "version_unknown"
