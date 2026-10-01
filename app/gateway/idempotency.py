"""Idempotency protection for write requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import anyio

from app.gateway.errors import ErrorCode, GatewayError


class IdempotencyState(str, Enum):
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"


@dataclass
class IdempotencyRecord:
    key: str
    caller: str
    fingerprint: str
    state: IdempotencyState
    status_code: int = 0
    body: bytes = b""
    event: anyio.Event = field(default_factory=anyio.Event, repr=False)


@dataclass(frozen=True)
class Claim:
    """Ownership token returned to the single request allowed to execute."""

    caller: str
    key: str
    fingerprint: str


class IdempotencyStore:
    """Stores first outcomes keyed by the caller's idempotency key.

    Semantics:

    - same ``(caller, key)`` + same request fingerprint: the first request
      executes; later (including *concurrent*) submissions wait if necessary
      and then receive an identical replay — the side effect happens once;
    - same ``(caller, key)`` + *different* fingerprint: rejected with
      ``idempotency_conflict`` and an explanation;
    - keys of different callers are independent;
    - if the first attempt crashes before producing a response its reservation
      is discarded so the caller can retry the same key.
    """

    def __init__(self) -> None:
        self._lock = anyio.Lock()
        self._records: dict[tuple[str, str], IdempotencyRecord] = {}

    async def begin_or_get(
        self, caller: str, key: str, fingerprint: str
    ) -> tuple[IdempotencyRecord | None, Claim | None]:
        """Atomically claim a key or fetch the existing record.

        Returns ``(record, claim)``: exactly one element is non-None.
        ``claim`` is returned when the caller owns the fresh in-flight
        reservation; ``record`` is returned for a replay of a completed first
        attempt. Concurrent identical submissions wait for the first attempt.
        """
        storage_key = (caller, key)
        async with self._lock:
            existing = self._records.get(storage_key)
            if existing is None:
                record = IdempotencyRecord(
                    key=key, caller=caller, fingerprint=fingerprint,
                    state=IdempotencyState.IN_FLIGHT,
                )
                self._records[storage_key] = record
                return None, Claim(caller=caller, key=key, fingerprint=fingerprint)

            if existing.fingerprint != fingerprint:
                raise GatewayError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    f"Idempotency-Key '{key}' for caller '{caller}' was already "
                    f"used with a different request payload; a key may only be "
                    f"reused by replaying the exact original request.",
                    status_code=409,
                    details={
                        "caller": caller,
                        "idempotency_key": key,
                        "original_fingerprint": existing.fingerprint,
                        "conflicting_fingerprint": fingerprint,
                    },
                )

            if existing.state is IdempotencyState.COMPLETED:
                return existing, None

            # Identical request still being executed by the first submitter.
            wait_event = existing.event

        # Release the lock while waiting, then re-evaluate once signalled
        # (the first attempt may have completed or have been discarded).
        await wait_event.wait()
        async with self._lock:
            existing = self._records.get(storage_key)
            if existing is not None and existing.state is IdempotencyState.COMPLETED:
                return existing, None
        # First attempt was discarded: retry now that the key is free.
        return await self.begin_or_get(caller, key, fingerprint)

    async def complete(self, claim: Claim, status_code: int, body: bytes) -> None:
        """Persist the final response for an owned in-flight claim."""
        storage_key = (claim.caller, claim.key)
        async with self._lock:
            record = self._records.get(storage_key)
            if record is None or record.state is IdempotencyState.COMPLETED:
                return
            record.state = IdempotencyState.COMPLETED
            record.status_code = status_code
            record.body = body
            record.event.set()

    async def discard(self, claim: Claim) -> None:
        """Release a claim whose owner failed before producing a response."""
        storage_key = (claim.caller, claim.key)
        async with self._lock:
            record = self._records.pop(storage_key, None)
        if record is not None:
            record.event.set()

    def reset(self) -> None:
        self._records.clear()
