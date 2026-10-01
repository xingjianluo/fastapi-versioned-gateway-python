"""Gateway wiring: applies versioning, rate limiting and idempotency
uniformly to every entry point of the FastAPI app.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .idempotency import IdempotencyStore, IdempotencyVerdict
from .ratelimit import LimitKind, RateLimitConfig, RateLimiter
from .versioning import (
    DEPRECATION_HEADER,
    SUNSET_HEADER,
    VERSION_HEADER,
    RejectReason,
    VersionRegistry,
    VersionState,
    default_registry,
)

logger = logging.getLogger("gateway")

CALLER_HEADER = "X-Caller-Id"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotency-Replayed"

ANONYMOUS_CALLER = "anonymous"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
EXEMPT_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


class GatewayMiddleware(BaseHTTPMiddleware):
    """Order per request: version negotiation -> rate limit -> idempotency."""

    def __init__(self, app, registry: VersionRegistry, limiter: RateLimiter, store: IdempotencyStore):
        super().__init__(app)
        self._registry = registry
        self._limiter = limiter
        self._store = store

    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        # 1. Version negotiation.
        requested = request.headers.get(VERSION_HEADER)
        result = self._registry.negotiate(requested)
        if not result.accepted:
            if result.reason is RejectReason.VERSION_SUNSET:
                logger.info("version rejected: requested=%r reason=version_sunset", requested)
                return _error(
                    410,
                    "version_sunset",
                    f"API version '{requested}' has been sunset and is no longer served",
                )
            logger.info("version rejected: requested=%r reason=unknown_version", requested)
            return _error(
                400,
                "unknown_version",
                f"API version '{requested}' does not exist; "
                f"omit the {VERSION_HEADER} header to use the default version",
            )
        version = result.version
        assert version is not None

        # 2. Per-caller rate limiting.
        caller = request.headers.get(CALLER_HEADER, ANONYMOUS_CALLER)
        decision = self._limiter.acquire(caller)
        if not decision.allowed:
            logger.info(
                "rate limited: caller=%r kind=%s detail=%s",
                caller, decision.kind.value if decision.kind else "?", decision.detail,
            )
            headers = {}
            if decision.kind is LimitKind.RATE and decision.retry_after is not None:
                headers["Retry-After"] = str(max(1, round(decision.retry_after)))
            return _error(
                429,
                f"{decision.kind.value if decision.kind else 'unknown'}_limited",
                decision.detail,
                headers=headers,
            )

        try:
            # 3. Idempotency for write requests that carry a key.
            idem_key = request.headers.get(IDEMPOTENCY_HEADER)
            if request.method in WRITE_METHODS and idem_key:
                return await self._with_idempotency(request, call_next, caller, idem_key, version)
            response = await call_next(request)
            return self._annotate(response, version)
        finally:
            self._limiter.release(caller)

    async def _with_idempotency(self, request: Request, call_next, caller: str, key: str, version):
        body = await request.body()
        fingerprint = hashlib.sha256(
            request.method.encode() + b" " + request.url.path.encode() + b"\n" + body
        ).hexdigest()
        check = self._store.begin(caller, key, fingerprint)

        if check.verdict is IdempotencyVerdict.CONFLICT:
            logger.info("idempotency conflict: caller=%r key=%r", caller, key)
            return _error(409, "idempotency_conflict", check.detail)

        if check.verdict is IdempotencyVerdict.REPLAY:
            logger.info("idempotency replay: caller=%r key=%r", caller, key)
            stored = check.stored_response or {}
            response = JSONResponse(
                status_code=stored.get("status", 200),
                content=stored.get("body"),
            )
            response.headers[REPLAYED_HEADER] = "true"
            return self._annotate(response, version)

        # EXECUTE: run the handler once, capture and store the response.
        try:
            response = await call_next(request)
        except Exception:
            # Unblock any concurrent duplicate waiting on this key.
            self._store.complete(caller, key, {"status": 500, "body": None})
            raise
        body_bytes = b""
        async for chunk in response.body_iterator:
            body_bytes += chunk
        try:
            parsed: Any = json.loads(body_bytes)
        except (ValueError, UnicodeDecodeError):
            parsed = None
        self._store.complete(caller, key, {"status": response.status_code, "body": parsed})
        replay = Response(
            content=body_bytes,
            status_code=response.status_code,
            media_type=response.media_type,
        )
        replay.headers[REPLAYED_HEADER] = "false"
        return self._annotate(replay, version)

    @staticmethod
    def _annotate(response: Response, version) -> Response:
        """Mark the negotiated version and deprecation state on responses."""
        response.headers[VERSION_HEADER] = version.name
        if version.state is VersionState.DEPRECATED:
            response.headers[DEPRECATION_HEADER] = "true"
            if version.sunset_date:
                response.headers[SUNSET_HEADER] = version.sunset_date
        return response


def create_app(
    registry: VersionRegistry | None = None,
    limiter: RateLimiter | None = None,
    store: IdempotencyStore | None = None,
) -> FastAPI:
    """Build the FastAPI app with gateway middleware and routes."""
    registry = registry or default_registry()
    limiter = limiter or RateLimiter(RateLimitConfig())
    store = store or IdempotencyStore()

    app = FastAPI(title="versioned-gateway")
    app.state.registry = registry
    app.state.limiter = limiter
    app.state.idempotency = store
    app.state.item_seq = itertools.count(1)
    app.state.items = {}

    app.add_middleware(
        GatewayMiddleware, registry=registry, limiter=limiter, store=store
    )

    @app.get("/")
    def read_root():
        return {"Hello": "World"}

    @app.get("/items")
    def list_items(request: Request):
        return {
            "version": request.headers.get(VERSION_HEADER) or "default",
            "items": list(request.app.state.items.values()),
        }

    @app.post("/items", status_code=201)
    def create_item(request: Request, payload: dict[str, Any]):
        item_id = next(request.app.state.item_seq)
        item = {"id": item_id, **payload}
        request.app.state.items[item_id] = item
        return item

    return app
