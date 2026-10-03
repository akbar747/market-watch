# src/market_watch/infrastructure/http/robots.py
"""robots.txt 抓取 / 24h 缓存 / 强制判定。

架构依据：docs/01-architecture.md §11.2-C2、§14 P0。

铁律：
- **无开关键**。`policy.ROBOTS_MODE` **不存在**；豁免必须是代码里的 `AuthorizationRecord`
  + PR 评审 + `docs/04-compliance.md` 登记。
- 404 / 无 robots.txt → RFC 9309 视为**无指令（允许）**。
- 一旦从「允许」变「禁止」，该源立即停用并 critical 告警（§14 P0）。
- 判定的 UA 必须是我们的真实 UA，**禁止**伪装 Googlebot 等搜索引擎。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import time
from typing import Final
from urllib.robotparser import RobotFileParser

import httpx

from ...domain.errors import RobotsDenied
from ...domain.models import iso_z, utc_now
from ...logging_setup import get_logger

__all__ = [
    "CACHE_TTL_S",
    "ROBOTS_CACHE_TTL_S",
    "RobotsGuard",
    "RobotsRecord",
]

_log = get_logger("market_watch.robots")

#: §11.2-C2：缓存 24h，落 `source_state.robots_checked_at`
ROBOTS_CACHE_TTL_S: Final[int] = 86_400
CACHE_TTL_S: Final[int] = ROBOTS_CACHE_TTL_S  # 别名，便于测试引用


@dataclass(slots=True)
class RobotsRecord:
    """一次 robots 抓取的留痕（供 `docs/04-compliance.md` 与审计表归档）。"""

    host: str
    checked_at: float = field(default_factory=time.monotonic)
    status_code: int | None = None
    allowed: bool = True
    raw: str = ""
    fetched_at_iso: str = ""
    note: str = ""

    def is_fresh(self, ttl_s: int = ROBOTS_CACHE_TTL_S) -> bool:
        return (time.monotonic() - self.checked_at) < ttl_s


class RobotsGuard:
    """每 host 一份缓存。构造期可注入预置快照（测试用仓库留存的 robots 快照）。"""

    __slots__ = (
        "_locks",
        "_on_deny",
        "_parsers",
        "_records",
        "_timeout_s",
        "_transport_factory",
        "_ttl_s",
        "_ua",
    )

    def __init__(
        self,
        user_agent: str,
        *,
        timeout_s: float = 10.0,
        ttl_s: int = ROBOTS_CACHE_TTL_S,
        transport_factory: Callable[[], httpx.AsyncTransport] | None = None,
        on_deny: Callable[[str, str], None] | None = None,
        preloaded: dict[str, RobotsRecord] | None = None,
    ) -> None:
        """构造 robots 守卫。

        Args:
            user_agent: 本进程的 UA（C1 校验过的完整 UA 串）。
            timeout_s: 抓 robots.txt 的超时。
            ttl_s: 缓存新鲜期（默认 `ROBOTS_CACHE_TTL_S` = 24h）。
            transport_factory: 可注入的 transport 工厂（单测传 `MockTransport`）。
            on_deny: 拒绝回调 `(host, path)`，用于发 critical 告警（C2）。
            preloaded: 仓库留存的 robots 原文快照（契约测试与离线判定都靠它）。

        C2 无开关键：首台机器跑完会把 `RobotsRecord` 落到 `source_state`，
        **`policy.ROBOTS_MODE` 不存在**，任何豁免必须是代码里的 `AuthorizationRecord`。
        """
        self._ua = user_agent
        self._records: dict[str, RobotsRecord] = dict(preloaded or {})
        self._parsers: dict[str, RobotFileParser] = {}
        # 预置快照（仓库留存的 robots 原文）也要建 parser，否则契约测试断言不到
        for host, rec in self._records.items():
            if rec.status_code is not None and 200 <= rec.status_code < 300 and rec.raw:
                self._parsers[host] = _parse_robots(rec.raw)
        self._locks: dict[str, asyncio.Lock] = {}
        self._timeout_s = timeout_s
        self._transport_factory = transport_factory
        self._ttl_s = ttl_s
        self._on_deny = on_deny

    def _lock(self, host: str) -> asyncio.Lock:
        lock = self._locks.get(host)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[host] = lock
        return lock

    def snapshot(self, host: str) -> RobotsRecord | None:
        """取单 host 的 robots 记录（含原文、headers、抓取时间）。

        Returns:
            `RobotsRecord`；未抓过返回 `None`。
        """
        return self._records.get(host)

    def snapshots(self) -> dict[str, RobotsRecord]:
        """全部 host 的 robots 记录快照（供 `policy-check` 与合规归档打印）。"""
        return dict(self._records)

    async def ensure(self, host: str) -> RobotsRecord:
        """确保该 host 有新鲜（<24h）的 robots 记录；没有（或过期）就抓一次。

        Args:
            host: 目标主机。

        Returns:
            新鲜的 `RobotsRecord`（404 / 无 robots → RFC 9309「无指令 = 允许」）。

        Raises:
            抓取失败不抛：按 RFC 9309 走「无法获取」分支，由调用方
            `assert_allowed` 决定保守放行还是拒绝。
        """
        rec = self._records.get(host)
        if rec is not None and rec.is_fresh(self._ttl_s):
            return rec
        async with self._lock(host):
            rec = self._records.get(host)
            if rec is not None and rec.is_fresh(self._ttl_s):
                return rec
            return await self._fetch(host)

    async def _fetch(self, host: str) -> RobotsRecord:
        url = f"https://{host}/robots.txt"
        now_iso = iso_z(utc_now())
        try:
            if self._transport_factory is not None:
                transport = self._transport_factory()
                async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
                    resp = await client.get(
                        url, timeout=self._timeout_s, headers={"User-Agent": self._ua}
                    )
                status, text = resp.status_code, resp.text
            else:
                async with httpx.AsyncClient(trust_env=False) as client:
                    resp = await client.get(
                        url, timeout=self._timeout_s, headers={"User-Agent": self._ua}
                    )
                status, text = resp.status_code, resp.text
        except Exception as exc:  # noqa: BLE001 - 抓取失败不得阻断：按 RFC 9309 走「无法获取」分支
            _log.warning("robots.fetch_failed host=%s err=%s -> 按无指令(allow)处理", host, exc)
            rec = RobotsRecord(
                host=host,
                status_code=None,
                allowed=True,
                raw="",
                fetched_at_iso=now_iso,
                note=f"抓取失败: {type(exc).__name__}",
            )
            self._records[host] = rec
            return rec

        if status == 404 or status == 410:
            rec = RobotsRecord(
                host=host,
                status_code=status,
                allowed=True,
                raw=text[:2000],
                fetched_at_iso=now_iso,
                note=f"HTTP {status} 无 robots.txt -> RFC 9309 视为无指令(allow)",
            )
        elif 200 <= status < 300:
            rec = RobotsRecord(
                host=host,
                status_code=status,
                allowed=True,
                raw=text,
                fetched_at_iso=now_iso,
                note="已解析 robots.txt",
            )
            self._parsers[host] = _parse_robots(text)
        else:
            rec = RobotsRecord(
                host=host,
                status_code=status,
                allowed=True,
                raw=text[:2000],
                fetched_at_iso=now_iso,
                note=f"HTTP {status} 非 2xx/404 -> 按无指令(allow)处理",
            )
        self._records[host] = rec
        _log.info("robots.loaded host=%s status=%s note=%s", host, status, rec.note)
        return rec

    async def allowed(self, host: str, path: str, ua: str | None = None) -> bool:
        """`RobotsGuard.allowed(host, path, ua) -> bool`（§13 签名）。

        ua 缺省用构造期的真实 UA。**禁止**传入伪装 UA。
        """
        await self.ensure(host)
        parser = self._parsers.get(host)
        if parser is None:
            # 无 robots.txt（404/抓取失败）→ RFC 9309 无指令 → 允许
            return True
        agent = ua or self._ua
        try:
            ok = parser.can_fetch(agent, path if path.startswith("/") else f"/{path}")
        except Exception as exc:  # noqa: BLE001 - parser 异常一律保守放行？不：保守拒绝
            _log.error("robots.parse_error host=%s err=%s -> 保守拒绝", host, exc)
            return False
        if not ok:
            _log.error("robots.denied host=%s path=%s ua=%s", host, path, agent)
            if self._on_deny is not None:
                self._on_deny(host, path)
        return ok

    async def assert_allowed(self, host: str, path: str, ua: str | None = None) -> None:
        """不允许即抛 `RobotsDenied`（→ ErrorClass.ROBOTS_DENIED，该源本轮终止）。"""
        if not await self.allowed(host, path, ua):
            raise RobotsDenied(host, path)


def _parse_robots(text: str) -> RobotFileParser:
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    return parser
