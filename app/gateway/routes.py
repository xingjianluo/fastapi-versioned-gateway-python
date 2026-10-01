"""Example gateway entry points exercising versioning, limiting and idempotency."""

from __future__ import annotations

import json
import threading
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

# In-memory side-effect log: tests assert that repeats/conflicts never duplicate it.
SIDE_EFFECTS: list[dict] = []
_effect_lock = threading.Lock()


def _record_side_effect(kind: str, payload: dict) -> str:
    """Pretend to perform a real write; append exactly one auditable record."""
    effect_id = f"{kind[:3]}-{uuid.uuid4().hex[:8]}"
    with _effect_lock:
        SIDE_EFFECTS.append({"id": effect_id, "kind": kind, "payload": payload})
    return effect_id


def _parse_body(request: Request) -> tuple[dict | None, JSONResponse | None]:
    raw: bytes = request.scope.get("gateway_body", b"")
    try:
        return json.loads(raw.decode() or "{}"), None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_json",
                               "message": "Request body must be a JSON object.",
                               "details": {}}},
        )


@router.get("/ping")
async def ping(request: Request) -> JSONResponse:
    """Read-only entry: version + rate limit, no idempotency needed."""
    decision = request.scope["gateway_version"]
    return JSONResponse(status_code=200, content={
        "pong": True,
        "caller": request.scope["gateway_caller"],
        "api_version": decision.effective,
        "version_status": decision.status.value,
    })


@router.post("/orders")
async def create_order(request: Request) -> JSONResponse:
    """Write entry: a real side effect is produced exactly once per idempotency key."""
    payload, error = _parse_body(request)
    if error is not None:
        return error
    order_id = _record_side_effect("order", payload)
    return JSONResponse(status_code=201, content={
        "order_id": order_id,
        "resource": "order",
        "created_from": payload,
        "api_version": request.scope["gateway_version"].effective,
    })


@router.post("/payments")
async def make_payment(request: Request) -> JSONResponse:
    """Second write entry proving behavior is consistent across entry points."""
    payload, error = _parse_body(request)
    if error is not None:
        return error
    payment_id = _record_side_effect("payment", payload)
    return JSONResponse(status_code=201, content={
        "payment_id": payment_id,
        "resource": "payment",
        "created_from": payload,
        "api_version": request.scope["gateway_version"].effective,
    })


def reset_side_effects() -> None:
    with _effect_lock:
        SIDE_EFFECTS.clear()
