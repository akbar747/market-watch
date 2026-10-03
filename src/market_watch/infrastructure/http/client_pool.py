# src/market_watch/infrastructure/http/client_pool.py
"""每 host 一个 `httpx.AsyncClient`；故障后整池重建 —— 这是「断线重连」的落点。

架构依据：docs/01-architecture.md §4.3（连接池参数）、§4.5（重建信号与语义）。

重建判定（任一命中即 `invalidate(host)`）：
| 信号 | 说明 |
|---|---|
| `httpx.RemoteProtocolError`（Server disconnected） | 典型半开连接 |
| `ConnectError` / `ConnectTimeout` 连续 ≥ 2 次 | TLS/握手层失效 |
| `ReadTimeout` 在同 host 60s 窗口内 ≥ 5 次 | 对面过载/链路劣化 |
| `PoolTimeout` | 池子被占满——多半是连接泄漏 |
| 池使用时长 > `POOL_MAX_LIFETIME=1800s` 或请求数 > `POOL_MAX_REQUESTS=5000` | 主动轮换 |

重建语义：`asyncio.Lock` 串行 → `await client.aclose()` → 丢弃引用 → 下次 `get()`
惰性新建；**在飞的请求不受影响**（它们持有旧 client 引用，自然结束后被 GC）。
重建后 `rate *= 0.7` 并记 `event="pool.rebuilt"`。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import time
from typing import Final

import httpx

from ...logging_setup import get_logger

__all__ = [
    "CONNECT_ERROR_THRESHOLD",
    "POOL_MAX_LIFETIME_S",
    "POOL_MAX_REQUESTS",
    "READ_TIMEOUT_THRESHOLD",
    "READ_TIMEOUT_WINDOW_S",
    "ClientPool",
    "PoolStats",
]

_log = get_logger("market_watch.client_pool")

POOL_MAX_LIFETIME_S: Final[float] = 1800.0
POOL_MAX_REQUESTS: Final[int] = 5000
READ_TIMEOUT_WINDOW_S: Final[float] = 60.0
READ_TIMEOUT_THRESHOLD: Final[int] = 5
CONNECT_ERROR_THRESHOLD: Final[int] = 2


@dataclass(slots=True)
class PoolStats:
    created_at: float = field(default_factory=time.monotonic)
    requests: int = 0
    read_timeouts: deque[float] = field(default_factory=deque)
    connect_errors: int = 0
    rebuilds: int = 0

    def note_request(self) -> None:
        self.requests += 1

    def note_read_timeout(self, now: float) -> int:
        self.read_timeouts.append(now)
        self._trim(now)
        return len(self.read_timeouts)

    def note_connect_error(self) -> int:
        self.connect_errors += 1
        return self.connect_errors

    def _trim(self, now: float) -> None:
        while self.read_timeouts and now - self.read_timeouts[0] > READ_TIMEOUT_WINDOW_S:
            self.read_timeouts.popleft()

    def age_s(self, now: float | None = None) -> float:
        return (now or time.monotonic()) - self.created_at

    def reset_counters(self) -> None:
        self.created_at = time.monotonic()
        self.requests = 0
        self.read_timeouts.clear()
        self.connect_errors = 0


class ClientPool:
    """每 host 单 AsyncClient。**adapter 内禁止 new httpx.Client**（§7.3 规则 5）。"""

    __slots__ = (
        "_clients",
        "_headers",
        "_http2",
        "_limits",
        "_locks",
        "_max_lifetime_s",
        "_max_requests",
        "_on_rebuild",
        "_proxy_url",
        "_stats",
        "_timeout",
        "_transport_factory",
    )

    def __init__(
        self,
        *,
        timeout: httpx.Timeout,
        limits: httpx.Limits,
        headers: dict[str, str],
        proxy_url: str | None = None,
        http2: bool = False,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        on_rebuild: Callable[[str, str], None] | None = None,
        max_lifetime_s: float = POOL_MAX_LIFETIME_S,
        max_requests: int = POOL_MAX_REQUESTS,
    ) -> None:
        """构造连接池（每 host 一个 `httpx.AsyncClient`）。

        Args:
            timeout: httpx 超时配置（由 `Settings.httpx_timeout()` 产出）。
            limits: httpx 连接限制（由 `Settings.httpx_limits()` 产出）。
            headers: 默认请求头（含 UA）。
            proxy_url: C5——**单一静态代理**；含 `,` 的代理池由
                `policy.assert_single_proxy` 在 Settings 层就拒绝启动。
            http2: 固定 `False`（§4.3）：HTTP/2 多路复用与「连接数 = 并发数」
                的令牌桶假设错配。
            transport_factory: 可注入的 transport 工厂（单测传 `MockTransport`）。
            on_rebuild: 重建回调 `(host, reason)`，用于 `rate *= 0.7` 降速。
            max_lifetime_s / max_requests: 单连接的最大存活时间与累计请求数，
                超过即主动重建（防半开连接老化）。

        安全默认值（不允许改）：`trust_env=False`（忽略环境变量代理）、
        `follow_redirects=False`（防跳转越权）、`verify=True`（禁 `verify=False`）。
        """
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._stats: dict[str, PoolStats] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._timeout = timeout
        self._limits = limits
        self._headers = dict(headers)
        self._proxy_url = proxy_url
        self._http2 = http2
        self._transport_factory = transport_factory
        self._on_rebuild = on_rebuild
        self._max_lifetime_s = max_lifetime_s
        self._max_requests = max_requests

    def _lock(self, host: str) -> asyncio.Lock:
        lock = self._locks.get(host)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[host] = lock
        return lock

    def _stats_for(self, host: str) -> PoolStats:
        st = self._stats.get(host)
        if st is None:
            st = PoolStats()
            self._stats[host] = st
        return st

    def _build(self, host: str) -> httpx.AsyncClient:
        kwargs: dict[str, object] = {
            "base_url": f"https://{host}",
            "timeout": self._timeout,
            "limits": self._limits,
            "headers": dict(self._headers),
            # C5：trust_env=False → 忽略 HTTP(S)_PROXY 环境变量，架构上禁止代理池
            "trust_env": False,
            # 禁止跟随跳转：防止官方源把请求引到第三方域（越权访问）
            "follow_redirects": False,
            "http2": self._http2,
            # 禁止关闭证书校验（§4.3）
            "verify": True,
        }
        if self._proxy_url:
            kwargs["proxy"] = self._proxy_url
        if self._transport_factory is not None:
            kwargs["transport"] = self._transport_factory()
        return httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]

    async def get(self, host: str) -> httpx.AsyncClient:
        """取该 host 的 client（惰性创建，命中轮换条件时先重建）。

        Args:
            host: 目标主机（hostname，不带 scheme）。

        Returns:
            该 host 的 `httpx.AsyncClient`。同一 host 的并发调用由 per-host
            `asyncio.Lock` 串行化，不会重复建 client。
        """

        async with self._lock(host):
            client = self._clients.get(host)
            st = self._stats_for(host)
            now = time.monotonic()
            if client is not None and (
                st.age_s(now) > self._max_lifetime_s or st.requests > self._max_requests
            ):
                await self._rebuild_locked(host, reason="lifetime_or_requests")
                client = self._clients.get(host)
            if client is None:
                client = self._build(host)
                self._clients[host] = client
                st.reset_counters()
                _log.info("pool.created host=%s", host)
            st.note_request()
            return client

    async def invalidate(self, host: str, reason: str) -> None:
        """整池重建（「断线重连」的落点，§4.5）。

        Args:
            host: 目标主机。
            reason: 重建原因（`remote_protocol` / `pool_timeout` / 生命周期到点…），
                进日志与 `on_rebuild` 回调。

        语义：用 per-host `asyncio.Lock` 串行化；**正在飞的请求不受影响**
        （只关掉池里存的 idle client，不 abort 已有流）。
        """
        async with self._lock(host):
            await self._rebuild_locked(host, reason=reason)

    async def _rebuild_locked(self, host: str, *, reason: str) -> None:
        client = self._clients.pop(host, None)
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不得阻断重建
                _log.warning("pool.aclose_failed host=%s err=%s", host, exc)
        st = self._stats_for(host)
        st.rebuilds += 1
        st.reset_counters()
        _log.warning("pool.rebuilt host=%s reason=%s rebuilds=%d", host, reason, st.rebuilds)
        if self._on_rebuild is not None:
            self._on_rebuild(host, reason)

    # ---- 故障信号登记（由 fetcher 在分类后调用） ----
    def note_failure(self, host: str, kind: str) -> bool:
        """登记一次故障，返回「是否需要重建」。

        Args:
            host: 目标主机。
            kind: `remote_protocol` / `connect_error` / `read_timeout` / `pool_timeout`。

        Returns:
            `True` = 调用方应立即 `invalidate(host)`。
            `remote_protocol` 与 `pool_timeout` 每次都重建；`connect_error` /
            `read_timeout` 有阈值（`CONNECT_ERROR_THRESHOLD` / `READ_TIMEOUT_THRESHOLD`）。
        """
        st = self._stats_for(host)
        now = time.monotonic()
        if kind == "remote_protocol":
            return True
        if kind == "pool_timeout":
            return True
        if kind == "connect_error":
            return st.note_connect_error() >= CONNECT_ERROR_THRESHOLD
        if kind == "read_timeout":
            return st.note_read_timeout(now) >= READ_TIMEOUT_THRESHOLD
        return False

    def note_success(self, host: str) -> None:
        """记一次成功（仅重置 connect_errors 计数；读超时窗口按时间自然滑出）。

        Args:
            host: 目标主机。
        """
        st = self._stats_for(host)
        st.connect_errors = 0

    def stats(self, host: str) -> dict[str, object]:
        st = self._stats_for(host)
        return {
            "host": host,
            "requests": st.requests,
            "rebuilds": st.rebuilds,
            "age_s": round(st.age_s(), 1),
            "read_timeouts_60s": len(st.read_timeouts),
            "connect_errors": st.connect_errors,
        }

    def hosts(self) -> tuple[str, ...]:
        return tuple(self._clients)

    async def aclose_all(self) -> None:
        for host in list(self._clients):
            client = self._clients.pop(host, None)
            if client is None:
                continue
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001
                _log.warning("pool.aclose_failed host=%s err=%s", host, exc)
        self._clients.clear()
        _log.info("pool.all_closed")
