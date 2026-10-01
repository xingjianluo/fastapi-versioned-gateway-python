"""Pure-ASGI gateway middleware: version, rate limit and idempotency in one place.

The middleware owns the complete decision chain for every request under the
mount prefix, so all entry points share identical, explainable behavior::

    caller identity -> version negotiation -> idempotency lookup
                       -> atomic rate/concurrency acquire
                       -> downstream execution -> idempotency capture/replay
"""

from __future__ import annotations

import hashlib
import json
import time

from app.gateway.errors import ErrorCode, GatewayError, error_payload
from app.gateway.idempotency import Claim, IdempotencyRecord, IdempotencyStore
from app.gateway.ratelimit import RateLimiter
from app.gateway.versions import (
    VersionDecision,
    VersionPolicy,
    VersionStatus,
    negotiate,
)

CALLER_HEADER = "x-caller-id"
IDEMPOTENCY_HEADER = "idempotency-key"

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class GatewayMiddleware:
    """Applies consistent gateway rules to every entry under the mount prefix."""

    def __init__(
        self,
        app,
        *,
        prefix: str = "/gateway",
        policy: VersionPolicy | None = None,
        limiter: RateLimiter | None = None,
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        self.app = app
        self.prefix = prefix
        self.policy = policy
        self.limiter = limiter
        self.idempotency = idempotency

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if not path.startswith(self.prefix):
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        caller = headers.get(CALLER_HEADER)
        if not caller:
            await self._reject(
                send,
                GatewayError(
                    ErrorCode.CALLER_MISSING,
                    f"Missing required '{CALLER_HEADER}' header; the gateway "
                    f"accounts rate limits per caller and cannot serve "
                    f"anonymous requests.",
                    status_code=401,
                ),
            )
            return

        # 1) Version negotiation ------------------------------------------------
        try:
            decision: VersionDecision = negotiate(
                headers.get(self.policy.header_name), self.policy
            )
        except GatewayError as exc:
            await self._reject(send, exc)
            return

        # 2) Buffer the (write) body once for fingerprinting + downstream replay
        body = b""
        http_method = scope["method"].upper()
        if http_method in WRITE_METHODS and self.idempotency is not None:
            chunks: list[bytes] = []
            while True:
                message = await receive()
                if message["type"] == "http.request":
                    chunks.append(message.get("body", b""))
                    if not message.get("more_body"):
                        break
                elif message["type"] == "http.disconnect":
                    return
            body = b"".join(chunks)

            async def receive_replay():
                return {"type": "http.request", "body": body, "more_body": False}
        else:
            receive_replay = receive

        # 3) Idempotency lookup for write requests ------------------------------
        claim: Claim | None = None
        replay_record: IdempotencyRecord | None = None
        idem_key = headers.get(IDEMPOTENCY_HEADER)
        if http_method in WRITE_METHODS and self.idempotency is not None and idem_key:
            fingerprint = hashlib.sha256(body).hexdigest()
            try:
                replay_record, claim = await self.idempotency.begin_or_get(
                    caller, idem_key, fingerprint
                )
            except GatewayError as exc:
                await self._reject(send, exc, decision=decision)
                return

        # Repeated (or concurrent-identical) submission: replay the first
        # outcome without consuming rate budget and without any side effect.
        if replay_record is not None:
            await self._send_replay(send, replay_record, decision, idem_key)
            return

        # 4) Atomic rate / concurrency acquisition ------------------------------
        try:
            self.limiter.acquire(caller, time.monotonic())
        except GatewayError as exc:
            if claim is not None:
                # Key stays reserved: the request never executed, and a retry
                # must still become the first real attempt.
                await self.idempotency.discard(claim)
            await self._reject(send, exc, decision=decision)
            return

        scope["gateway_caller"] = caller
        scope["gateway_version"] = decision
        scope["gateway_body"] = body

        # 5) Downstream execution with response capture -------------------------
        captured: dict = {"status": 500, "chunks": []}
        started = False

        async def send_wrapper(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                captured["status"] = message["status"]
                message = self._with_gateway_headers(dict(message), decision,
                                                     idem_key, replayed=False)
            elif message["type"] == "http.response.body":
                captured["chunks"].append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive_replay, send_wrapper)
        except Exception:
            self.limiter.release(caller)
            if claim is not None:
                await self.idempotency.discard(claim)
            raise

        self.limiter.release(caller)

        # 6) Persist idempotent outcome for future replays ----------------------
        if claim is not None:
            response_body = b"".join(captured["chunks"])
            if started:
                await self.idempotency.complete(claim, captured["status"],
                                                response_body)
            else:
                await self.idempotency.discard(claim)

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _gateway_headers(decision: VersionDecision | None, idem_key: str | None,
                         *, replayed: bool) -> list[tuple[bytes, bytes]]:
        headers: list[tuple[bytes, bytes]] = []
        if decision is not None:
            headers.append((b"x-gateway-api-version", decision.effective.encode()))
            if decision.used_default:
                headers.append((b"x-gateway-version-source", b"default"))
            if decision.status is VersionStatus.DEPRECATED:
                headers.append((
                    b"x-gateway-version-deprecation",
                    b"version is deprecated; migrate to the stable default",
                ))
        if idem_key:
            headers.append((b"x-idempotency-key", idem_key.encode()))
            headers.append((b"x-idempotency-replayed",
                            b"true" if replayed else b"false"))
        return headers

    @staticmethod
    def _with_gateway_headers(message: dict, decision: VersionDecision | None,
                              idem_key: str | None, *, replayed: bool) -> dict:
        raw = message.get("headers", [])
        raw = [(k, v) for k, v in raw
               if k.lower() not in {b"x-gateway-api-version",
                                    b"x-gateway-version-source",
                                    b"x-gateway-version-deprecation",
                                    b"x-idempotency-key",
                                    b"x-idempotency-replayed"}]
        raw.extend(GatewayMiddleware._gateway_headers(
            decision, idem_key, replayed=replayed))
        message["headers"] = raw
        return message

    async def _reject(self, send, exc: GatewayError, *,
                      decision: VersionDecision | None = None) -> None:
        body = error_payload(exc.code, exc.message, details=exc.details)
        raw = json.dumps(body).encode()
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
        ]
        headers.extend(self._gateway_headers(decision, None, replayed=False))
        await send({"type": "http.response.start",
                    "status": exc.status_code, "headers": headers})
        await send({"type": "http.response.body", "body": raw})

    async def _send_replay(self, send, record: IdempotencyRecord,
                           decision: VersionDecision, idem_key: str) -> None:
        body = record.body
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        headers.extend(self._gateway_headers(decision, idem_key, replayed=True))
        await send({"type": "http.response.start",
                    "status": record.status_code, "headers": headers})
        await send({"type": "http.response.body", "body": body})
