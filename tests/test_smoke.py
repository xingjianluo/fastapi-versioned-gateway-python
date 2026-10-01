"""Smoke test: the assembled application boots and the default entry works."""


def test_root_and_ping_ok():
    from app.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["default_api_version"] == "2025-01-01"

    resp = client.get("/gateway/ping", headers={"X-Caller-Id": "smoke"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["pong"] is True
    assert body["api_version"] == "2025-01-01"
    assert resp.headers["x-gateway-api-version"] == "2025-01-01"
    assert resp.headers["x-gateway-version-source"] == "default"
