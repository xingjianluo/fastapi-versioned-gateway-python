"""Application assembly: wire policy, limiter, idempotency store and middleware."""

from __future__ import annotations

from fastapi import FastAPI

from app.gateway.errors import install_exception_handlers
from app.gateway.idempotency import IdempotencyStore
from app.gateway.middleware import GatewayMiddleware
from app.gateway.ratelimit import RateLimitConfig, RateLimiter
from app.gateway.routes import router as gateway_router
from app.gateway.versions import default_policy


def create_app(
    *,
    rate_limit: RateLimitConfig | None = None,
    idempotency: IdempotencyStore | None = None,
    limiter: RateLimiter | None = None,
) -> FastAPI:
    """Build the FastAPI application with the gateway layer installed."""
    app = FastAPI(
        title="Versioned FastAPI Gateway",
        version="0.1.0",
        description=(
            "External gateway providing version negotiation, per-caller rate "
            "limiting and idempotency protection consistently at every entry."
        ),
    )

    policy = default_policy()
    store = idempotency if idempotency is not None else IdempotencyStore()
    if limiter is None:
        limiter = RateLimiter(
            rate_limit or RateLimitConfig(rate_per_second=5, max_concurrent=2)
        )

    # Pure-ASGI middleware (add wraps inside-out; order is irrelevant since the
    # gateway middleware handles the whole decision chain on its own).
    app.add_middleware(
        GatewayMiddleware,
        prefix="/gateway",
        policy=policy,
        limiter=limiter,
        idempotency=store,
    )
    app.include_router(gateway_router, prefix="/gateway")
    install_exception_handlers(app)

    # Exposed for tests / operational reset.
    app.state.idempotency_store = store
    app.state.rate_limiter = limiter
    app.state.version_policy = policy

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {"service": "versioned-gateway",
                "entries": ["/gateway/ping", "/gateway/orders",
                            "/gateway/payments"],
                "default_api_version": policy.default_version}

    return app


app = create_app()
