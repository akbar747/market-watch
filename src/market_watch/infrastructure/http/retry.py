# src/market_watch/infrastructure/http/retry.py
"""「是否重试 + 等多久」的唯一决策点 + 执行器。

架构依据：docs/01-architecture.md §5.2（重试与退避）、§6（18 类矩阵唯一实现点）、§13。

退避公式：
    raw = base * factor ** (attempt - 1)
    cap = min(raw, BACKOFF_MAX_S)
    sleep = U(0, cap)                       # full jitter（§5.2）
    若 respect_retry_after 且有 Retry-After → sleep = min(retry_after, RETRY_AFTER_MAX_S)
    若 420/vendor 限流 → sleep = max(retry_after_or_0, 30s)  强制 30s 起步
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import random
import time
from typing import Final

import httpx

from ...domain.errors import ErrorClass, OpenCircuit, policy_for
from ...domain.models import FetchResult, FetchTask, ResponseView
from ...domain.ports import Breaker, RateLimiter
from ...logging_setup import get_logger
from .classification import classify_exc, classify_response
from .client_pool import ClientPool

__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_FACTOR",
    "BACKOFF_MAX_S",
    "RETRY_AFTER_MAX_S",
    "RETRY_COST",
    "TOTAL_ATTEMPT_BUDGET_S",
    "VENDOR_FLOOR_S",
    "RetryBudget",
    "decide",
    "execute",
]

_log = get_logger("market_watch.retry")

#: §5.2 默认值
BACKOFF_BASE_S: Final[float] = 1.0
BACKOFF_FACTOR: Final[float] = 2.0
BACKOFF_MAX_S: Final[float] = 30.0
RETRY_AFTER_MAX_S: Final[float] = 120.0
TOTAL_ATTEMPT_BUDGET_S: Final[float] = 90.0
RETRY_COST: Final[float] = 1.0  # 重试同样要过令牌桶（重试不是免费的份额）

#: 420 / vendor 限流的强制起步（§6 #6）
VENDOR_FLOOR_S: Final[float] = 30.0


@dataclass(slots=True)
class RetryBudget:
    """单个请求的累计预算（含等待）。超预算即软失败并停止重试（§5.2）。"""

    total_attempt_budget_s: float = TOTAL_ATTEMPT_BUDGET_S
    started_at: float = 0.0
    slept_s: float = 0.0

    def start(self) -> None:
        self.started_at = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0

    def remaining(self) -> float:
        return self.total_attempt_budget_s - self.elapsed()


def decide(
    err: ErrorClass,
    view: ResponseView | None,
    attempt: int,
    *,
    base_s: float = BACKOFF_BASE_S,
    factor: float = BACKOFF_FACTOR,
    max_s: float = BACKOFF_MAX_S,
    retry_after_max_s: float = RETRY_AFTER_MAX_S,
    rng: random.Random | None = None,
) -> tuple[bool, float]:
    """决策纯函数：返回 `(是否重试, 等待秒数)`。`attempt` 从 1 开始（1 = 首次）。

    §6 逐条对齐：
    - `retryable=False` 的分类一律 (False, 0.0)。
    - `attempt >= max_attempts` → 不再重试。
    - 429/503 优先读 `Retry-After`，钳到 ≤ `RETRY_AFTER_MAX_S`；
      超过该上限则回落到 `BACKOFF_MAX_S` 而不是傻等（§5.1 `RETRY_AFTER_MAX_S` 说明）。
    - 420/vendor：强制 `max(Retry-After, 30s)` 起步。
    - JSON_DECODE **不指数**：固定 `2.0 * attempt`。
    - 其余：指数 + full jitter（不加抖动会让 140 个分页任务在重试时同步再撞一次）。
    """
    pol = policy_for(err)
    if not pol["retryable"]:
        return False, 0.0
    if attempt >= pol["max_attempts"]:
        return False, 0.0

    if rng is None:
        # 必须用实例而非 `rng or random`：后者把**模块**当发生器用，模块不是
        # `random.Random`，`rng.uniform` 在类型上不成立；full jitter 只用局部
        # `uniform(0, cap)`，不碰全局随机状态，换实例不产生行为差异。
        rng = random.Random()  # noqa: S311 - 退避抖动，非安全用途（§5.2 full jitter）
    retry_after = view.retry_after_seconds if view is not None else None

    # --- 420 / vendor 限流：强制 30s 起步 ---
    if err == ErrorClass.RATE_LIMITED_VENDOR:
        floor = VENDOR_FLOOR_S
        if pol["respect_retry_after"] and retry_after is not None:
            floor = max(floor, min(retry_after, retry_after_max_s))
        return True, floor

    # --- 优先 Retry-After（429 / 5xx 的 503）---
    #     超过可信上限(Retry-After > RETRY_AFTER_MAX_S) → 落空，走下面指数退避，不傻等
    if pol["respect_retry_after"] and retry_after is not None and retry_after <= retry_after_max_s:
        return True, max(0.0, retry_after)

    # --- JSON 解析失败：固定步长，不指数（§6 #8） ---
    if err == ErrorClass.JSON_DECODE:
        return True, 2.0 * attempt

    raw = base_s * (factor ** (attempt - 1))
    cap = min(raw, max_s)
    return True, rng.uniform(0.0, cap)


async def execute(
    task: FetchTask,
    *,
    pool: ClientPool,
    limiter: RateLimiter,
    breaker: Breaker,
    # ★ 首参是 `httpx.AsyncClient`（`ClientPool.get` 的实际返回类型），不是 `object`。
    # 标 `object` 会把「客户端」这道逆变位置敞开，调用方传进来的
    # `Callable[[AsyncClient, FetchTask], ...]` 就全部对不上号（mypy 逆变报错）。
    request: Callable[[httpx.AsyncClient, FetchTask], Awaitable[ResponseView]],
    on_event: Callable[[str, dict], None] | None = None,
    base_s: float = BACKOFF_BASE_S,
    factor: float = BACKOFF_FACTOR,
    max_s: float = BACKOFF_MAX_S,
    total_budget_s: float = TOTAL_ATTEMPT_BUDGET_S,
    retry_cost: float = RETRY_COST,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    rng: random.Random | None = None,
) -> FetchResult:
    """重试执行器。**全仓唯一的「发请求 + 重试」循环**（供 `fetcher.fetch` 调用）。

    顺序：熔断 before → 限流 acquire → 请求 → 分类 → 决策 → 退避 → 下一轮。
    冷却（`cool_host`）与池重建信号由调用方（`fetcher`）按分类结果触发，
    本函数只负责「重试与否 + 等多久」。

    返回 `FetchResult`；`view` 为 `None` 表示一次响应都没拿到。

    Args:
        task: 出网任务（`host` 决定熔断与限流的按键）。
        pool: 每 host 的 `httpx.AsyncClient` 池（断线重连的落点）。
        limiter: 限流器（实现 `domain.ports.RateLimiter`）。
        breaker: 熔断器（实现 `domain.ports.Breaker`）。
        request: 单次请求回调 `(client, task) -> ResponseView`；
            由调用方（`fetcher`）注入，本函数不关心底层 transport。
        on_event: 可选的事件回调 `(event_name, payload)`，用于 metrics 挂钩。
        base_s / factor / max_s: 退避基数 / 指数 / 上限。
        total_budget_s: 单个请求的累计时间预算（含等待），超预算即软失败停止重试。
        retry_cost: 重试的令牌成本（默认 `RETRY_COST=1.0`，重试不是免费份额）。
        sleep: 可注入的等待实现（测试用假时钟，避免真等 30s）。
        rng: 可注入的随机源（退避抖动；默认 `random`，非加密用途）。

    Raises:
        asyncio.CancelledError: 原样传播（`CancelledError` 不计失败、不重试）。
    """
    _sleep = sleep or asyncio.sleep
    budget = RetryBudget(total_attempt_budget_s=total_budget_s)
    budget.start()

    attempts = 0
    slept_ms = 0.0
    last_view: ResponseView | None = None
    last_err = ErrorClass.UNKNOWN
    retry_after_used: float | None = None

    while True:
        attempts += 1
        # --- 熔断前置 ---
        try:
            breaker.before(task.host)
        except OpenCircuit:
            _log.warning("retry.circuit_open host=%s task=%s", task.host, task.task_id)
            return FetchResult(
                task_id=task.task_id,
                view=None,
                error_class=ErrorClass.UNKNOWN,
                attempts=attempts,
                total_ms=budget.elapsed() * 1000.0,
                slept_ms=slept_ms,
            )

        # --- 令牌（重试也要付费：RETRY_COST=1.0） ---
        cost = retry_cost if attempts > 1 else float(task.cost)
        await limiter.acquire(task.host, cost)

        # --- 请求 ---
        t0 = time.monotonic()
        try:
            client = await pool.get(task.host)
            view = await request(client, task)
            last_view = view
            last_err = classify_response(view)
        except asyncio.CancelledError:
            raise  # §6 #17：必须原样传播，禁止吞掉
        except Exception as exc:  # noqa: BLE001 - 分类需要兜底全部异常
            last_err = classify_exc(exc)
            last_view = None
        duration_ms = (time.monotonic() - t0) * 1000.0

        if on_event is not None:
            on_event(
                "attempt",
                {
                    "task_id": task.task_id,
                    "attempt": attempts,
                    "error_class": str(last_err),
                    "duration_ms": round(duration_ms, 2),
                },
            )

        # --- 成功/终态 ---
        if last_err in (ErrorClass.OK, ErrorClass.NOT_MODIFIED):
            breaker.record(task.host, last_err)
            limiter.reward(task.host)
            return FetchResult(
                task_id=task.task_id,
                view=last_view,
                error_class=last_err,
                attempts=attempts,
                total_ms=budget.elapsed() * 1000.0,
                slept_ms=slept_ms,
                retry_after_used_s=retry_after_used,
            )

        # 记录失败（供熔断统计）
        breaker.record(task.host, last_err)

        # --- 决策 ---
        retry, wait_s = decide(
            last_err,
            last_view,
            attempts,
            base_s=base_s,
            factor=factor,
            max_s=max_s,
            rng=rng,
        )
        pol = policy_for(last_err)
        if wait_s and (last_view is None or last_view.retry_after_seconds is not None):
            retry_after_used = wait_s

        if not retry:
            return FetchResult(
                task_id=task.task_id,
                view=last_view,
                error_class=last_err,
                attempts=attempts,
                total_ms=budget.elapsed() * 1000.0,
                slept_ms=slept_ms,
                retry_after_used_s=retry_after_used,
            )

        # --- 预算：超预算即软失败，停止重试（§5.2） ---
        if budget.remaining() <= wait_s:
            _log.warning(
                "retry.budget_exhausted task=%s attempts=%d err=%s remaining=%.1fs wait=%.1fs",
                task.task_id,
                attempts,
                last_err,
                budget.remaining(),
                wait_s,
            )
            return FetchResult(
                task_id=task.task_id,
                view=last_view,
                error_class=last_err,
                attempts=attempts,
                total_ms=budget.elapsed() * 1000.0,
                slept_ms=slept_ms,
                retry_after_used_s=retry_after_used,
            )

        _log.warning(
            "retry.scheduled task=%s attempt=%d err=%s wait=%.2fs cool_factor=%.2f",
            task.task_id,
            attempts,
            last_err,
            wait_s,
            pol["cool_factor"],
        )
        await _sleep(wait_s)
        slept_ms += wait_s * 1000.0
