"""RateLimiter: fixed-window counting tested against a small fake Redis, so
these tests need neither a real Redis server nor the network.
"""

from __future__ import annotations

from qagent.modules.ratelimit.limiter import InMemoryRateLimiter, RateLimiter


class FakeRedis:
    """Enough of the redis-py client API for RateLimiter to exercise: incr,
    expire and ttl, backed by a plain dict."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._ttls: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def expire(self, key: str, seconds: int) -> None:
        self._ttls[key] = seconds

    def ttl(self, key: str) -> int:
        return self._ttls.get(key, -1)


def test_allows_requests_under_the_limit() -> None:
    limiter = RateLimiter(FakeRedis())
    for _ in range(5):
        result = limiter.hit("k", limit=5, window_seconds=60)
        assert result.allowed is True


def test_denies_the_request_that_exceeds_the_limit() -> None:
    limiter = RateLimiter(FakeRedis())
    for _ in range(5):
        limiter.hit("k", limit=5, window_seconds=60)

    result = limiter.hit("k", limit=5, window_seconds=60)
    assert result.allowed is False
    assert result.remaining == 0


def test_reports_decreasing_remaining_count() -> None:
    limiter = RateLimiter(FakeRedis())
    first = limiter.hit("k", limit=5, window_seconds=60)
    second = limiter.hit("k", limit=5, window_seconds=60)
    assert first.remaining == 4
    assert second.remaining == 3


def test_sets_the_window_expiry_only_on_the_first_hit() -> None:
    redis = FakeRedis()
    limiter = RateLimiter(redis)
    limiter.hit("k", limit=5, window_seconds=60)
    assert redis.ttl("k") == 60


def test_different_keys_are_independent() -> None:
    limiter = RateLimiter(FakeRedis())
    for _ in range(5):
        limiter.hit("org-a", limit=5, window_seconds=60)

    # A different tenant/IP is unaffected by org-a exhausting its own budget.
    result = limiter.hit("org-b", limit=5, window_seconds=60)
    assert result.allowed is True


def test_retry_after_reflects_the_remaining_window() -> None:
    redis = FakeRedis()
    limiter = RateLimiter(redis)
    for _ in range(5):
        limiter.hit("k", limit=5, window_seconds=60)
    redis._ttls["k"] = 42  # simulate 42s left in the window

    result = limiter.hit("k", limit=5, window_seconds=60)
    assert result.retry_after_seconds == 42


class TestInMemoryRateLimiter:
    def test_allows_then_denies_like_the_redis_backed_version(self) -> None:
        limiter = InMemoryRateLimiter()
        for _ in range(3):
            assert limiter.hit("k", limit=3, window_seconds=60).allowed is True

        assert limiter.hit("k", limit=3, window_seconds=60).allowed is False

    def test_window_resets_after_it_elapses(self, monkeypatch) -> None:
        limiter = InMemoryRateLimiter()
        clock = [1000.0]
        monkeypatch.setattr(
            "qagent.modules.ratelimit.limiter.time.monotonic", lambda: clock[0]
        )

        for _ in range(3):
            limiter.hit("k", limit=3, window_seconds=10)
        assert limiter.hit("k", limit=3, window_seconds=10).allowed is False

        clock[0] += 11  # past the window
        assert limiter.hit("k", limit=3, window_seconds=10).allowed is True
