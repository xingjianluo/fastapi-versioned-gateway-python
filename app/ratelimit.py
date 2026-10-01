"""Per-caller rate limiting: fixed-window rate + concurrency cap.

Limits are enforced per caller identity. Callers never affect each other.
Counting is exact under concurrent bursts (a single lock guards the whole
check-and-increment, so no update is lost or double-counted), and callers
are admitted again once the window rolls over or in-flight requests finish.
"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass


class LimitKind(enum.Enum):
    RATE = "rate"
    CONCURRENCY = "concurrency"


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    kind: LimitKind | None = None
    retry_after: float | None = None
    detail: str = ""


@dataclass(frozen=True)
class RateLimitConfig:
    max_requests: int = 10
    window_seconds: float = 60.0
    max_concurrent: int = 5


@dataclass
class _CallerState:
    window_start: float
    window_count: int
    in_flight: int


class RateLimiter:
    """Thread/async-safe per-caller limiter.

    Usage: ``decision = limiter.acquire(caller)``; if ``decision.allowed``
    the caller MUST call ``limiter.release(caller)`` when the request
    finishes so the concurrency slot is returned.
    """

    def __init__(self, config: RateLimitConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._states: dict[str, _CallerState] = {}

    def acquire(self, caller: str) -> LimitDecision:
        """Try to admit one request for ``caller`` (atomic check+count)."""
        now = time.monotonic()
        with self._lock:
            state = self._states.get(caller)
            if state is None:
                state = _CallerState(window_start=now, window_count=0, in_flight=0)
                self._states[caller] = state

            # Roll the fixed window if it has expired.
            elapsed = now - state.window_start
            if elapsed >= self._config.window_seconds:
                state.window_start = now
                state.window_count = 0
                elapsed = 0.0

            if state.in_flight >= self._config.max_concurrent:
                return LimitDecision(
                    allowed=False,
                    kind=LimitKind.CONCURRENCY,
                    retry_after=None,
                    detail=(
                        f"caller '{caller}' has {state.in_flight} requests "
                        f"in flight (max {self._config.max_concurrent})"
                    ),
                )

            if state.window_count >= self._config.max_requests:
                retry_after = self._config.window_seconds - elapsed
                return LimitDecision(
                    allowed=False,
                    kind=LimitKind.RATE,
                    retry_after=retry_after,
                    detail=(
                        f"caller '{caller}' sent {state.window_count} requests "
                        f"in the current {self._config.window_seconds:g}s window "
                        f"(max {self._config.max_requests})"
                    ),
                )

            state.window_count += 1
            state.in_flight += 1
            return LimitDecision(allowed=True)

    def release(self, caller: str) -> None:
        """Return the concurrency slot held by a finished request."""
        with self._lock:
            state = self._states.get(caller)
            if state is not None and state.in_flight > 0:
                state.in_flight -= 1
