"""Gateway error codes and the unified error response model."""

from __future__ import annotations

from enum import Enum

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ErrorCode(str, Enum):
    """Machine-readable, distinguishable rejection reasons."""

    VERSION_UNKNOWN = "version_unknown"
    VERSION_RETIRED = "version_retired"
    CALLER_MISSING = "caller_missing"
    RATE_LIMITED = "rate_limited"
    CONCURRENCY_LIMITED = "concurrency_limited"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class GatewayError(Exception):
    """A rejection produced by the gateway layer.

    Each error carries a stable machine-readable ``code`` plus a human-readable
    ``message`` explaining the decision basis.
    """

    def __init__(self, code: ErrorCode, message: str, *, status_code: int = 400,
                 details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def error_payload(code: ErrorCode, message: str, *, details: dict | None = None) -> dict:
    """Build the canonical, explainable error envelope.

    Shape::

        {"error": {"code": <machine readable>, "message": <human readable>,
                   "details": {...}}}
    """
    return {"error": {"code": code.value, "message": message, "details": details or {}}}


def install_exception_handlers(app: FastAPI) -> None:
    """Register the gateway error handler so every entry point behaves alike."""

    @app.exception_handler(GatewayError)
    async def _handle_gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.message, details=exc.details),
        )
