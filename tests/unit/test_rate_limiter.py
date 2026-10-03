# tests/unit/test_rate_limiter.py
"""令牌桶 + AIMD（§4.2）。重点：`CancelledError` 必须退还令牌。"""

from __future__ import annotations

import asyncio

import pytest

from market_watch.infrastructure.http.rate_limiter import (
    ADAPTIVE_FLOOR_RPS,
    HostRateLimiter,
    TokenBucket,
)


def _bucket(rps: float = 8.0, capacity: int = 12) -> TokenBucket:
    return TokenBucket(rps=rps, capacity=capacity)


class TestTokenBucket:
    def test_starts_full(self) -> None:
        b = _bucket()
        assert b.tokens == pytest.approx(12.0)

    def test_acquire_consumes_token(self) -> None:
        b = _bucket()
        assert b.acquire_sync() == 0.0
        assert b.consumed_total == 1

    def test_throttles_when_empty(self, fake_clock, fake_sleep) -> None:
        b = _bucket(rps=2.0, capacity=1)
        b.bind_clock(fake_clock)
        b.bind_sleep(fake_sleep)
        b._tokens = 0.0
        wait = asyncio.run(b.acquire(1.0))
        assert wait == pytest.approx(0.5)  # deficit 1 / rps 2
        assert b.throttled_total == 1

    def test_refill_over_time(self, fake_clock) -> None:
        b = _bucket(rps=10.0, capacity=10)
        b.bind_clock(fake_clock)
        b._tokens = 0.0
        fake_clock.advance(0.5)
        assert b.tokens == pytest.approx(5.0)

    def test_refill_caps_at_capacity(self, fake_clock) -> None:
        b = _bucket(rps=10.0, capacity=4)
        b.bind_clock(fake_clock)
        b._tokens = 0.0
        fake_clock.advance(100.0)
        assert b.tokens == pytest.approx(4.0)


class TestCancellationRefund:
    def test_cancelled_acquire_refunds_token(self, fake_clock) -> None:
        """★ §4.2：取消时必须退还，否则重启后进程看到被吃空的桶。"""
        b = _bucket(rps=1.0, capacity=10)
        b.bind_clock(fake_clock)
        before = b.tokens

        async def _sleep_forever(_seconds: float) -> None:
            raise asyncio.CancelledError()

        b.bind_sleep(_sleep_forever)
        b._tokens = 0.0  # 强制走等待路径

        async def _run() -> None:
            await b.acquire(1.0)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_run())
        # 令牌被退还（0 - 1 + 1 = 0，且 throttled 计数回滚）
        assert b.tokens == pytest.approx(0.0)
        assert b.throttled_total == 0
        assert before >= 0

    def test_cancelled_during_wait_does_not_lose_token(self, fake_clock) -> None:
        b = _bucket(rps=2.0, capacity=2)
        b.bind_clock(fake_clock)
        b._tokens = 2.0

        async def _cancel(_s: float) -> None:
            raise asyncio.CancelledError()

        b.bind_sleep(_cancel)
        b._tokens = 0.5  # 不足 1.0 → 走等待路径

        async def _run() -> None:
            await b.acquire(1.0)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_run())
        assert b.tokens == pytest.approx(0.5)  # 退还回原值


class TestAIMD:
    def test_penalize_is_multiplicative_decrease(self) -> None:
        b = _bucket(rps=8.0)
        assert b.penalize(0.5) == pytest.approx(4.0)
        assert b.penalize(0.5) == pytest.approx(2.0)

    def test_penalize_has_floor(self) -> None:
        b = _bucket(rps=1.2)
        b.penalize(0.5)
        assert b.rps >= ADAPTIVE_FLOOR_RPS

    def test_reward_is_additive_increase(self, fake_clock) -> None:
        b = _bucket(rps=4.0)
        b.bind_clock(fake_clock)
        fake_clock.advance(1000.0)  # 超过 HEALTHY_WINDOW
        assert b.reward() == pytest.approx(4.4)
        fake_clock.advance(1000.0)
        assert b.reward() == pytest.approx(4.84)

    def test_reward_never_exceeds_initial(self, fake_clock) -> None:
        b = _bucket(rps=8.0)
        b.bind_clock(fake_clock)
        for _ in range(20):
            fake_clock.advance(1000.0)
            b.reward()
        assert b.rps == pytest.approx(8.0)

    def test_reward_requires_healthy_window(self, fake_clock) -> None:
        b = _bucket(rps=4.0)
        b.bind_clock(fake_clock)
        b.penalize(0.5)
        fake_clock.advance(10.0)  # 远小于 300s
        assert b.reward() == pytest.approx(2.0)  # 不恢复


class TestHostRateLimiter:
    def test_configure_is_idempotent(self) -> None:
        lim = HostRateLimiter()
        lim.configure("a.example", 8.0, 12)
        lim.configure("a.example", 8.0, 12)
        assert lim.snapshot("a.example").current_rps == pytest.approx(8.0)

    def test_unknown_host_gets_conservative_default(self) -> None:
        lim = HostRateLimiter()
        assert lim.snapshot("unknown.example").current_rps <= 1.0

    def test_configure_never_raises_above_initial(self) -> None:
        lim = HostRateLimiter()
        lim.configure("a.example", 2.0, 4)
        lim.configure("a.example", 8.0, 12)  # 试图抬高
        assert lim.snapshot("a.example").current_rps <= 2.0 + 1e-9

    def test_per_host_isolation(self) -> None:
        lim = HostRateLimiter()
        lim.configure("a.example", 8.0, 12)
        lim.configure("b.example", 8.0, 12)
        lim.penalize("a.example", 0.5)
        assert lim.snapshot("a.example").current_rps == pytest.approx(4.0)
        assert lim.snapshot("b.example").current_rps == pytest.approx(8.0)


# 同步辅助（避免每个用例都写 asyncio.run）
def _acquire_sync(self: TokenBucket) -> float:  # pragma: no cover - 测试辅助
    return asyncio.run(self.acquire())


TokenBucket.acquire_sync = _acquire_sync  # type: ignore[attr-defined]
