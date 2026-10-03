# src/market_watch/infrastructure/http/fetcher.py
"""**全仓唯一允许出网的地方**。

架构依据：docs/01-architecture.md §3（职责表）、§13（顺序: robots → breaker → limiter → request → classify）。

强制检查点落在这里：
- **C4 只读**：`method ∈ {"GET","HEAD"}` 且无 body，否则抛 `ReadOnlyViolation`。
- **C2 robots**：`RobotsGuard.assert_allowed`，无开关键。
- **C3 限流**：所有请求必须经令牌桶（`limiter.acquire`），重试也付费（`RETRY_COST=1.0`）。
- **熔断**：open 状态直接短路，不消耗任何请求额度。
"""

from __future__ import annotations

import time
from typing import Final

import httpx

from ...domain.errors import ErrorClass
from ...domain.models import FetchResult, FetchTask, ResponseView
from ...domain.policy import assert_readonly
from ...logging_setup import get_logger
from .breaker import CircuitBreaker
from .classification import classify_exc, classify_response
from .client_pool import ClientPool
from .metrics import Metrics
from .rate_limiter import HostRateLimiter
from .retry import execute
from .robots import RobotsGuard

__all__ = ["Fetcher"]

_log = get_logger("market_watch.fetcher")

#: 故障 → client_pool.note_failure 的 kind（§4.5 信号表）
_FAILURE_KIND: Final[dict[ErrorClass, str]] = {
    ErrorClass.REMOTE_PROTOCOL: "remote_protocol",
    ErrorClass.POOL_TIMEOUT: "pool_timeout",
    ErrorClass.CONNECT_TIMEOUT: "connect_error",
    ErrorClass.DNS_FAILURE: "connect_error",
    ErrorClass.READ_TIMEOUT: "read_timeout",
}


class Fetcher:
    """实现 `domain.ports.Fetcher`。全仓唯一出网口。"""

    __slots__ = ("_breaker", "_limiter", "_metrics", "_pool", "_probe_remaining", "_robots", "_ua")

    def __init__(
        self,
        *,
        pool: ClientPool,
        limiter: HostRateLimiter,
        breaker: CircuitBreaker,
        robots: RobotsGuard,
        metrics: Metrics,
        user_agent: str,
        probe_rate_limit_header: bool = False,
    ) -> None:
        self._pool = pool
        self._limiter = limiter
        self._breaker = breaker
        self._robots = robots
        self._metrics = metrics
        self._ua = user_agent
        # §6 #7：本实测源不返回 Remaining 头 → 默认关闭该探测器
        self._probe_remaining = probe_rate_limit_header

    async def fetch(self, task: FetchTask) -> tuple[ResponseView | None, ErrorClass, int]:
        """执行一次出网（全仓唯一入口）。

        顺序（§13）：**C4 只读 → C2 robots → 熔断 → 限流 → 请求 → 分类 → 后置**。

        Args:
            task: 出网任务。

        Returns:
            `(view, error_class, attempts)`。`view` 为 `None` 表示没拿到响应
            （robots 拒绝 / 熔断短路 / 全部分类为不可重试）；`attempts=0` 表示
            一次都没发出去（被前置闸门拦下）。

        Raises:
            ReadOnlyViolation: C4——`method` 不在 `{GET, HEAD}` 或带了 body。
            RobotsDenied: 由内部转成 `ROBOTS_DENIED` 返回，不外抛。
        """

        # --- C4 只读判定（在发出任何字节之前） ---
        assert_readonly(task.method, None)

        # --- C2 robots（无开关键） ---
        try:
            await self._robots.assert_allowed(task.host, task.path, self._ua)
        except Exception as exc:  # noqa: BLE001 - RobotsDenied 与其它抓取异常统一处理
            from ...domain.errors import RobotsDenied

            if isinstance(exc, RobotsDenied):
                self._metrics.count("robots.denied", host=task.host)
                _log.error("fetch.robots_denied host=%s path=%s", task.host, task.path)
                return None, ErrorClass.ROBOTS_DENIED, 0
            raise

        # --- 熔断前置 ---
        try:
            self._breaker.before(task.host)
        except Exception as exc:  # noqa: BLE001
            from ...domain.errors import OpenCircuit

            if isinstance(exc, OpenCircuit):
                self._metrics.count("breaker.short_circuited", host=task.host)
                return None, ErrorClass.UNKNOWN, 0
            raise

        # --- 执行（限流 + 重试在 retry.execute 内） ---
        result: FetchResult = await execute(
            task,
            pool=self._pool,
            limiter=self._limiter,
            breaker=self._breaker,
            request=self._request,
            on_event=self._on_attempt,
        )

        # --- 后置：冷却 / 池重建 / 限流头探测 / 指标 ---
        await self._post_process(task, result)
        return result.view, result.error_class, result.attempts

    async def _request(self, client: httpx.AsyncClient, task: FetchTask) -> ResponseView:
        """真正发一次请求。retry.execute 会调用它。"""
        t0 = time.monotonic()
        resp = await client.request(
            task.method,
            task.path,
            params=dict(task.params),
            headers={"User-Agent": self._ua, **dict(task.headers)},
        )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return ResponseView(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            content=resp.content,
            url=str(resp.url),
            elapsed_ms=elapsed_ms,
        )

    def _on_attempt(self, name: str, payload: dict) -> None:
        self._metrics.count(f"http.{name}", **payload)

    async def _post_process(self, task: FetchTask, result: FetchResult) -> None:
        err = result.error_class
        view = result.view

        self._metrics.observe(
            "http.request",
            host=task.host,
            error_class=str(err),
            attempts=result.attempts,
            duration_ms=result.total_ms,
            bytes_len=len(view.content) if view else 0,
            status_code=view.status_code if view else None,
        )
        self._metrics.record_fetch_log(
            host=task.host,
            path=task.path,
            method=task.method,
            status_code=view.status_code if view else None,
            attempts=result.attempts,
            duration_ms=result.total_ms,
            resp_bytes=len(view.content) if view else 0,
            error_class=str(err),
            ua=self._ua,
        )

        if err in (ErrorClass.OK, ErrorClass.NOT_MODIFIED):
            self._pool.note_success(task.host)
            self._metrics.count("http.success", host=task.host)
            if view is not None and self._probe_remaining:
                self._probe_rate_limit_headers(task.host, view)
            return

        # --- 冷却（§6 的 cool_host / cool_factor） ---
        from ...domain.errors import policy_for

        pol = policy_for(err)
        if pol["cool_host"]:
            self._limiter.penalize(task.host, pol["cool_factor"])
            self._metrics.count("rate.penalized", host=task.host, reason=str(err))

        # --- 连接池重建信号（§4.5） ---
        kind = _FAILURE_KIND.get(err)
        if kind is not None and self._pool.note_failure(task.host, kind):
            await self._pool.invalidate(task.host, reason=kind)
            self._limiter.penalize(task.host, 0.7)  # 比 429 轻柔的自适应减速
            self._metrics.count("pool.rebuilt", host=task.host, reason=kind)

    def _probe_rate_limit_headers(self, host: str, view: ResponseView) -> None:
        """§6 #7：存在 `X-RateLimit-Remaining` 且 ≤10% limit → `rate *= 0.6` 主动降速。"""
        limit = view.rate_limit
        remaining = view.rate_remaining
        if limit is None or remaining is None or limit <= 0:
            return
        if remaining <= limit * 0.10:
            self._limiter.penalize(host, 0.6)
            self._metrics.count("rate.header_low", host=host, remaining=remaining, limit=limit)
            _log.warning(
                "rate.header_low host=%s remaining=%d/%d -> rate*=0.6", host, remaining, limit
            )

    async def aclose(self) -> None:
        await self._pool.aclose_all()


def classify_view(view: ResponseView) -> ErrorClass:
    """供 adapter/metrics 复用：`classification` 是唯一出口（§6）。"""
    return classify_response(view)


def classify_error(exc: BaseException) -> ErrorClass:
    """异常 → ErrorClass 的便捷转发（唯一出口仍是 `classification.classify_exc`）。"""
    return classify_exc(exc)
