"""Per-caller rate limiting (sliding window) and concurrency limiting."""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass

from app.gateway.errors import ErrorCode, GatewayError

WINDOW_SECONDS = 1.0


@dataclass(frozen=True)
class RateLimitConfig:
    rate_per_second: int
    max_concurrent: int


class _Bucket:
    __slots__ = ("hits", "in_flight")

    def __init__(self) -> None:
        # Timestamps of rate-limited requests admitted in the current window.
        self.hits: deque[float] = deque()
        self.in_flight: int = 0


class RateLimiter:
    """Per-caller token accounting safe under concurrent bursts.

    Different callers have independent buckets. Limits are checked and
    reserved atomically so bursts can neither under- nor over-count: the
    window eviction, threshold comparison and slot reservation all happen
    inside one critical section.
    """

    def __init__(self, config: RateLimitConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = defaultdict(_Bucket)

    def acquire(self, caller: str, now: float) -> None:
        """Atomically reserve one request slot for ``caller``.

        Raises GatewayError(RATE_LIMITED / CONCURRENCY_LIMITED) when exceeded.
        On rejection no slot is reserved.
        """
        cfg = self._config
        with self._lock:
            bucket = self._buckets[caller]
            # Evict timestamps that have slid out of the trailing 1s window.
            horizon = now - WINDOW_SECONDS
            while bucket.hits and bucket.hits[0] <= horizon:
                bucket.hits.popleft()

            if len(bucket.hits) >= cfg.rate_per_second:
                retry_after = max(0.0, WINDOW_SECONDS - (now - bucket.hits[0]))
                raise GatewayError(
                    ErrorCode.RATE_LIMITED,
                    f"Caller '{caller}' exceeded the rate limit of "
                    f"{cfg.rate_per_second} request(s) per "
                    f"{WINDOW_SECONDS:g}s (observed {len(bucket.hits)}); "
                    f"retry after {retry_after:.3f}s.",
                    status_code=429,
                    details={
                        "caller": caller,
                        "limit": cfg.rate_per_second,
                        "window_seconds": WINDOW_SECONDS,
                        "observed": len(bucket.hits),
                        "retry_after_seconds": round(retry_after, 3),
                    },
                )

            if bucket.in_flight >= cfg.max_concurrent:
                raise GatewayError(
                    ErrorCode.CONCURRENCY_LIMITED,
                    f"Caller '{caller}' exceeded the concurrency limit of "
                    f"{cfg.max_concurrent} simultaneous request(s) "
                    f"(observed {bucket.in_flight}); retry once an in-flight "
                    f"request completes.",
                    status_code=429,
                    details={
                        "caller": caller,
                        "max_concurrent": cfg.max_concurrent,
                        "observed": bucket.in_flight,
                    },
                )

            # Reservation only happens after BOTH checks pass, atomically.
            bucket.hits.append(now)
            bucket.in_flight += 1

    def release(self, caller: str) -> None:
        """Return the concurrency slot acquired by :meth:`acquire`."""
        with self._lock:
            bucket = self._buckets.get(caller)
            if bucket is not None and bucket.in_flight > 0:
                bucket.in_flight -= 1
            # Prune idle buckets so callers that went quiet cost nothing and
            # get a fully fresh accounting on their next burst.
            if bucket is not None and bucket.in_flight == 0 and not bucket.hits:
                del self._buckets[caller]

    def reset(self) -> None:
        """Forget all callers (test helper)."""
        with self._lock:
            self._buckets.clear()
