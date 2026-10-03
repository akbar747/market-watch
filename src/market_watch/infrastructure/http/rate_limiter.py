# src/market_watch/infrastructure/http/rate_limiter.py
"""每主机令牌桶 + AIMD 自适应降速（并发控制的**第 3 层**）。

架构依据：docs/01-architecture.md §4.2（三层并发控制 / AIMD）、§5.1（参数表）、§13。

三层顺序（由外向内）：
    1. `asyncio.Semaphore(GLOBAL_INFLIGHT=16)`  —— 全进程在飞闸
    2. 每主机 `Semaphore(concurrency=4)`         —— 单域并发上限
    3. 每主机 `TokenBucket(rps=8.0, capacity=12)` —— 本文件，令牌桶 + AIMD

★ 不可协商的两条契约：
    1. `acquire` 在 `asyncio.CancelledError` 时**必须退还已扣令牌**。
       否则优雅退出（Ctrl-C）后重启的进程会看到一个被吃空的桶，
       表现为「重启后第一分钟莫名很慢」（§4.2）。
    2. `reward()`（加性增）的天花板是 `min(配置初始值, EFFECTIVE_RPS_CAP)`，
       **永远不会**把速率抬到合规上限之上。想提速的唯一合法路径是减少请求数
       （用满 page_size=200），不是提高 rps/concurrency。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import time
from typing import Final

from ...domain import policy
from ...domain.models import RateState
from ...logging_setup import get_logger

__all__ = [
    "ADAPTIVE_FLOOR_RPS",
    "ADAPTIVE_HEALTHY_WINDOW_S",
    "ADAPTIVE_RECOVER_STEP",
    "DEFAULT_CONSERVATIVE_BURST",
    "DEFAULT_CONSERVATIVE_RPS",
    "HostRateLimiter",
    "TokenBucket",
]

_log = get_logger("market_watch.rate_limiter")

#: §5.1 自适应参数（常用稳定起点，需实测确认）
ADAPTIVE_FLOOR_RPS: Final[float] = 1.0  # 降速地板，永不低于 1 rps
ADAPTIVE_HEALTHY_WINDOW_S: Final[float] = 300.0  # 连续健康多久才允许加性增
ADAPTIVE_RECOVER_STEP: Final[float] = 1.10  # 每次 +10%

#: 未注册主机的保守默认值：宁可慢，不可越界（§4.2「未知主机按最保守处理」）
DEFAULT_CONSERVATIVE_RPS: Final[float] = 1.0
DEFAULT_CONSERVATIVE_BURST: Final[int] = 2


class TokenBucket:
    """单主机令牌桶。

    `clock` / `sleep` 可注入（§9.1「注入假时钟、假 sleep」），让退避与限额
    在单测里无需真等。
    """

    __slots__ = (
        "_clock",
        "_consumed",
        "_last",
        "_last_penalty_at",
        "_last_reward_at",
        "_sleep",
        "_throttled",
        "_tokens",
        "capacity",
        "ceiling_rps",
        "floor_rps",
        "initial_rps",
        "rps",
    )

    def __init__(
        self,
        rps: float = policy.EFFECTIVE_RPS_CAP,
        capacity: int = 12,
        *,
        ceiling_rps: float | None = None,
        floor_rps: float = ADAPTIVE_FLOOR_RPS,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.initial_rps = float(rps)
        self.rps = float(rps)
        self.capacity = int(capacity)
        # 天花板：C3 合规上限（8.0 rps）。reward 永远越不过它。
        # 注意「不超过配置初始值」由 HostRateLimiter 的 declared_initial 保证，
        # 不在这里再夹一道——否则 per-host 配 4.0 时连 +10% 恢复都被吃掉。
        self.ceiling_rps = min(
            float(ceiling_rps if ceiling_rps is not None else policy.EFFECTIVE_RPS_CAP),
            policy.EFFECTIVE_RPS_CAP,
        )
        self.floor_rps = float(floor_rps)
        self._clock: Callable[[], float] = clock or time.monotonic
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._last = float(self._clock())
        self._tokens = float(capacity)
        self._consumed = 0
        self._throttled = 0
        self._last_penalty_at: float | None = None
        self._last_reward_at: float | None = None

    # ---- 测试注入点（§9.1） ----
    def bind_clock(self, clock: Callable[[], float]) -> None:
        """替换时钟，并把补充基准对齐到新时钟（避免换钟后一次性补满）。"""
        self._clock = clock
        self._last = float(clock())

    def bind_sleep(self, sleep: Callable[[float], Awaitable[None]]) -> None:
        self._sleep = sleep

    # ---- 只读视图 ----
    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    @property
    def consumed_total(self) -> int:
        return self._consumed

    @property
    def throttled_total(self) -> int:
        return self._throttled

    def _refill(self) -> None:
        now = float(self._clock())
        delta = now - self._last
        if delta > 0:
            self._tokens = min(float(self.capacity), self._tokens + delta * self.rps)
            self._last = now
        elif delta < 0:
            # 时钟被替换/回拨：只挪基准，绝不倒扣令牌
            self._last = now

    # ---- 核心 ----
    async def acquire(self, cost: float = 1.0) -> float:
        """取 `cost` 个令牌。返回实际等待秒数（0.0 = 未等待）。

        ★ `CancelledError` 时退还令牌，保证优雅退出不留下被吃空的桶。
        """
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            self._consumed += 1
            return 0.0

        deficit = cost - self._tokens
        wait_s = deficit / self.rps if self.rps > 0 else 0.0

        # 预扣（可为负），睡醒后由 _refill 自然补平
        self._tokens -= cost
        self._consumed += 1
        self._throttled += 1
        try:
            await self._sleep(wait_s)
        except asyncio.CancelledError:
            self._tokens += cost  # ★ 退还
            self._consumed -= 1
            self._throttled -= 1
            raise
        self._refill()
        return wait_s

    # ---- AIMD ----
    def penalize(self, factor: float) -> float:
        """乘性减。429/420 → 0.5；读超时批量 / 池重建 → 0.7；剩余额度告急 → 0.6。"""
        factor = min(1.0, max(0.01, float(factor)))
        self._refill()
        before = self.rps
        self.rps = max(self.floor_rps, self.rps * factor)
        self._last_penalty_at = float(self._clock())
        if self.rps < before:
            _log.warning(
                "rate.penalized factor=%.2f from=%.3f to=%.3f floor=%.3f",
                factor,
                before,
                self.rps,
                self.floor_rps,
            )
        return self.rps

    def reward(self) -> float:
        """加性增：连续 `ADAPTIVE_HEALTHY_WINDOW_S` 无惩罚后 `rps *= 1.10`。

        天花板 = `min(initial_rps, EFFECTIVE_RPS_CAP)`，且**无**健康窗口则不恢复。
        """
        self._refill()
        now = float(self._clock())
        anchor = self._last_penalty_at
        if anchor is None:
            anchor = self._last_reward_at
        if anchor is not None and (now - anchor) < ADAPTIVE_HEALTHY_WINDOW_S:
            return self.rps  # 窗口未满，不恢复（§4.2）
        before = self.rps
        self.rps = min(self.ceiling_rps, self.rps * ADAPTIVE_RECOVER_STEP)
        self._last_reward_at = now
        if self.rps > before:
            _log.info(
                "rate.recovered from=%.3f to=%.3f ceiling=%.3f", before, self.rps, self.ceiling_rps
            )
        return self.rps

    def mark_error(self) -> None:
        """非降速类错误也刷新惩罚锚点，避免刚出错就立刻恢复。"""
        self._last_penalty_at = float(self._clock())

    # ---- 参数调整 ----
    def retune(self, rps: float, capacity: int) -> None:
        """重设参数。**只允许下调**（想提速请减少请求数，不是提高 rps）。"""
        self._refill()
        target_rps = min(float(rps), self.rps)
        self.rps = max(self.floor_rps, target_rps)
        self.capacity = min(int(capacity), self.capacity)
        self._tokens = min(self._tokens, float(self.capacity))

    def state(self, host: str) -> RateState:
        self._refill()
        return RateState(
            host=host,
            current_rps=self.rps,
            capacity=self.capacity,
            tokens=self._tokens,
            consumed_total=self._consumed,
            throttled_total=self._throttled,
            last_penalty_at=None,
        )


class HostRateLimiter:
    """每主机一个桶 + 全进程在飞闸。实现 `domain.ports.RateLimiter`。"""

    __slots__ = ("_buckets", "_clock", "_global_inflight", "_initial", "_sema", "_sleep")

    def __init__(
        self,
        global_inflight: int = 16,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._initial: dict[str, float] = {}
        self._global_inflight = max(1, int(global_inflight))
        self._sema = asyncio.Semaphore(self._global_inflight)
        self._clock = clock
        self._sleep = sleep

    # ---- 测试注入 ----
    def bind_clock(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        for bucket in self._buckets.values():
            bucket.bind_clock(clock)

    def bind_sleep(self, sleep: Callable[[float], Awaitable[None]]) -> None:
        self._sleep = sleep
        for bucket in self._buckets.values():
            bucket.bind_sleep(sleep)

    @property
    def inflight(self) -> asyncio.Semaphore:
        """第 1 层：全进程在飞闸（§4.2）。直接 await 即可。"""
        return self._sema

    # ---- 注册 ----
    def configure(self, host: str, rps: float, capacity: int) -> None:
        """注册/更新某主机桶（幂等）。**已注册主机只降不升**。

        Args:
            host: 目标主机。
            rps: 期望速率，会被 `policy.effective_rps` 再钳一道到 `EFFECTIVE_RPS_CAP`。
            capacity: 桶容量（突发上限，≥1）。

        Raises:
            不抛：配置值非法（rps ≤ 0）时按 0 处理，只是该桶永远不会放行。
        """
        rps = policy.effective_rps(rps)  # C3 再钳一道，防止调用方绕过
        capacity = max(1, int(capacity))
        declared_initial = self._initial.get(host)
        if declared_initial is not None:
            # 只允许下调：想提速请减少请求数，不是提高 rps
            rps = min(rps, declared_initial)
        else:
            self._initial[host] = float(rps)

        bucket = self._buckets.get(host)
        if bucket is None:
            bucket = TokenBucket(
                rps=rps,
                capacity=capacity,
                ceiling_rps=min(self._initial[host], policy.EFFECTIVE_RPS_CAP),
                clock=self._clock,
                sleep=self._sleep,
            )
            self._buckets[host] = bucket
        else:
            bucket.retune(rps, capacity)
        self._initial[host] = min(self._initial[host], float(rps))

    def bucket(self, host: str) -> TokenBucket:
        """取桶；未注册主机按最保守默认值建桶（1 rps / burst 2）。

        ⚠️ 「未知主机 = 最保守」是刻意设计（§4.2）：任何漏注册的 host 也不会
        打到官方端点上，代价只是慢一点。

        Args:
            host: 目标主机。

        Returns:
            该主机的 `TokenBucket`（可能刚刚惰性创建）。
        """
        bucket = self._buckets.get(host)
        if bucket is None:
            self.configure(host, DEFAULT_CONSERVATIVE_RPS, DEFAULT_CONSERVATIVE_BURST)
            bucket = self._buckets[host]
        return bucket

    # ---- 端口实现 ----
    async def acquire(self, host: str, cost: float = 1.0) -> float:
        return await self.bucket(host).acquire(cost)

    def penalize(self, host: str, factor: float) -> float:
        return self.bucket(host).penalize(factor)

    def reward(self, host: str) -> float:
        return self.bucket(host).reward()

    def snapshot(self, host: str) -> RateState:
        return self.bucket(host).state(host)

    def all_snapshots(self) -> list[RateState]:
        return [bucket.state(host) for host, bucket in self._buckets.items()]

    def hosts(self) -> list[str]:
        """已建桶的主机列表（升序，供看板遍历）。"""
        return sorted(self._buckets)
