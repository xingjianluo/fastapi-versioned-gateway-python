"""Idempotency protection for write requests.

Same caller + same idempotency key + same payload => replay the first
response (no duplicate side effects). Same key reused with a different
payload => rejected as a conflict. Concurrent submissions with the same
key are serialized so only one execution happens: the later one waits for
the first to finish and then replays its response.
"""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass
from typing import Any, Callable


class IdempotencyVerdict(enum.Enum):
    EXECUTE = "execute"  # first time we see this key: run the handler
    REPLAY = "replay"  # key+payload seen before: return stored response
    CONFLICT = "conflict"  # key reused with a different payload


@dataclass(frozen=True)
class IdempotencyCheck:
    verdict: IdempotencyVerdict
    stored_response: Any = None
    detail: str = ""


@dataclass
class _Record:
    fingerprint: str
    done: threading.Event
    response: Any = None


class IdempotencyStore:
    """In-memory store keyed by (caller, idempotency key)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], _Record] = {}

    def begin(self, caller: str, key: str, fingerprint: str) -> IdempotencyCheck:
        """Atomically classify the submission.

        - EXECUTE: caller must later invoke ``complete`` with the response.
        - REPLAY: ``stored_response`` holds the first response.
        - CONFLICT: ``detail`` explains the payload mismatch.
        """
        store_key = (caller, key)
        with self._lock:
            record = self._records.get(store_key)
            if record is None:
                # First submission: claim the key, others will wait on us.
                self._records[store_key] = _Record(
                    fingerprint=fingerprint, done=threading.Event()
                )
                return IdempotencyCheck(verdict=IdempotencyVerdict.EXECUTE)
            if record.fingerprint != fingerprint:
                return IdempotencyCheck(
                    verdict=IdempotencyVerdict.CONFLICT,
                    detail=(
                        f"idempotency key '{key}' was already used with a "
                        "different request payload"
                    ),
                )
            done = record.done
        # Same key, same payload, first execution still in flight:
        # wait outside the lock so only one execution ever happens.
        done.wait()
        return IdempotencyCheck(
            verdict=IdempotencyVerdict.REPLAY,
            stored_response=record.response,
        )

    def complete(self, caller: str, key: str, response: Any) -> None:
        """Record the response for a key previously marked EXECUTE."""
        store_key = (caller, key)
        with self._lock:
            record = self._records.get(store_key)
            if record is None:
                raise KeyError(f"no in-flight idempotency record for {store_key!r}")
            record.response = response
            record.done.set()

    def run(
        self,
        caller: str,
        key: str,
        fingerprint: str,
        handler: Callable[[], Any],
    ) -> tuple[Any, IdempotencyVerdict]:
        """Convenience: begin -> maybe execute handler -> complete."""
        check = self.begin(caller, key, fingerprint)
        if check.verdict is IdempotencyVerdict.EXECUTE:
            response = handler()
            self.complete(caller, key, response)
            return response, IdempotencyVerdict.EXECUTE
        if check.verdict is IdempotencyVerdict.REPLAY:
            return check.stored_response, IdempotencyVerdict.REPLAY
        raise IdempotencyConflict(check.detail)


class IdempotencyConflict(Exception):
    """Raised by ``run`` when a key is reused with a different payload."""
