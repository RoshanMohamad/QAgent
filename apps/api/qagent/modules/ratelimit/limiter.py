"""A fixed-window rate limiter backed by Redis (CLAUDE.md section 23).

Two things this protects, and why each matters:

- **Unauthenticated endpoints** (`/auth/register`, `/auth/login`), keyed by
  client IP. Without a limit here, a JWT signed with a compromised or weak
  `QAGENT_SECRET_KEY` is the least of an operator's problems - the login
  endpoint itself is a free password-guessing oracle, and registration is a
  free way to fill the `organizations` table.
- **Expensive, queued actions** (`POST .../runs`, `POST .../performance/scan`),
  keyed by organization. Nothing else in this codebase stops one tenant from
  saturating the shared Celery queue every other tenant's runs wait behind -
  RLS isolates *data*, not *throughput*.

Fixed-window (not sliding, not a token bucket) is a deliberate simplification:
it under-protects at window boundaries (a caller can send `2 * limit` requests
across a boundary) in exchange for being two Redis commands and no background
process. That trade is fine for "stop a runaway script," which is the actual
threat model here - not for billing-grade metering, which would need something
sturdier.

`RedisLike` is a Protocol, not a class, so this is tested (see
`tests/test_ratelimit.py`) against a small in-memory fake rather than a real
Redis server, the same way `browser/runner.py`'s tests fake Playwright's
`Page` instead of launching a browser.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


class RedisLike(Protocol):
    def incr(self, key: str) -> int: ...
    def expire(self, key: str, seconds: int) -> object: ...
    def ttl(self, key: str) -> int: ...


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class RateLimiter:
    def __init__(self, redis: RedisLike) -> None:
        self._redis = redis

    def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        """Record one request against ``key`` and say whether it's allowed.

        The window is anchored to first use of this exact key, not wall-clock
        boundaries (no "every request in the same calendar minute" alignment):
        ``INCR`` creates the key at count 1, and only that first caller sets
        the expiry, which is what makes "anchored to first use" true without a
        second round trip to check existence first.
        """
        count = self._redis.incr(key)
        if count == 1:
            self._redis.expire(key, window_seconds)

        if count > limit:
            retry_after = self._redis.ttl(key)
            return RateLimitResult(
                allowed=False,
                remaining=0,
                retry_after_seconds=max(retry_after, 1),
            )

        return RateLimitResult(allowed=True, remaining=limit - count, retry_after_seconds=0)


class InMemoryRateLimiter:
    """A `RateLimiter`-shaped limiter with no Redis at all.

    For a single-process deployment (or a test) where pulling in a real Redis
    connection isn't worth it. Not safe across multiple API processes - each
    would keep its own counters - which is exactly the gap the Redis-backed
    `RateLimiter` above closes for a real deployment.
    """

    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, float]] = {}  # key -> (count, resets_at)

    def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        now = time.monotonic()
        count, resets_at = self._counts.get(key, (0, 0.0))
        if now >= resets_at:
            count, resets_at = 0, now + window_seconds

        count += 1
        self._counts[key] = (count, resets_at)

        if count > limit:
            return RateLimitResult(
                allowed=False,
                remaining=0,
                retry_after_seconds=max(int(resets_at - now), 1),
            )
        return RateLimitResult(allowed=True, remaining=limit - count, retry_after_seconds=0)
