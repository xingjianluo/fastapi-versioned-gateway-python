"""Version negotiation: default fallback, deprecation marking, and
distinguishable rejections for sunset vs unknown versions."""

import logging

from fastapi.testclient import TestClient

from app.versioning import RejectReason

logger = logging.getLogger("test.versioning")


def test_missing_header_uses_default_version(app):
    client = TestClient(app)
    resp = client.get("/items")
    logger.info("input: no X-API-Version header -> status=%s served=%s",
                resp.status_code, resp.headers.get("x-api-version"))
    assert resp.status_code == 200
    assert resp.headers["x-api-version"] == "v2"
    assert "deprecation" not in resp.headers


def test_deprecated_version_served_but_marked(app):
    client = TestClient(app)
    resp = client.get("/items", headers={"X-API-Version": "v1"})
    logger.info("input: X-API-Version=v1 (deprecated) -> status=%s deprecation=%s sunset=%s",
                resp.status_code, resp.headers.get("deprecation"), resp.headers.get("sunset"))
    assert resp.status_code == 200
    assert resp.headers["x-api-version"] == "v1"
    assert resp.headers["deprecation"] == "true"
    assert resp.headers["sunset"] == "2026-12-31"


def test_sunset_version_rejected_with_distinct_reason(app):
    client = TestClient(app)
    resp = client.get("/items", headers={"X-API-Version": "v0"})
    logger.info("input: X-API-Version=v0 (sunset) -> status=%s code=%s",
                resp.status_code, resp.json()["error"]["code"])
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "version_sunset"


def test_unknown_version_rejected_with_distinct_reason(app):
    client = TestClient(app)
    resp = client.get("/items", headers={"X-API-Version": "v99"})
    logger.info("input: X-API-Version=v99 (unknown) -> status=%s code=%s",
                resp.status_code, resp.json()["error"]["code"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_version"


def test_reject_reasons_are_distinguishable(app):
    client = TestClient(app)
    sunset = client.get("/items", headers={"X-API-Version": "v0"})
    unknown = client.get("/items", headers={"X-API-Version": "v99"})
    logger.info("sunset -> (%s, %s); unknown -> (%s, %s)",
                sunset.status_code, sunset.json()["error"]["code"],
                unknown.status_code, unknown.json()["error"]["code"])
    assert sunset.status_code != unknown.status_code
    assert sunset.json()["error"]["code"] != unknown.json()["error"]["code"]


def test_no_silent_fallback_on_rejection(app):
    """A rejected version must NOT be silently served as the default."""
    client = TestClient(app)
    for bad in ("v0", "v99"):
        resp = client.get("/items", headers={"X-API-Version": bad})
        logger.info("input: X-API-Version=%s -> status=%s (must not be 200/v2)",
                    bad, resp.status_code)
        assert resp.status_code != 200
        assert resp.headers.get("x-api-version") != "v2"


def test_registry_negotiation_unit(registry):
    logger.info("registry table: %s", {k: v.state.value for k, v in registry._versions.items()})
    assert registry.negotiate(None).version.name == "v2"
    assert registry.negotiate("v1").accepted
    assert registry.negotiate("v0").reason is RejectReason.VERSION_SUNSET
    assert registry.negotiate("nope").reason is RejectReason.UNKNOWN_VERSION
