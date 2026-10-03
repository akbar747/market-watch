# src/market_watch/interfaces/wiring.py
"""依赖 wiring：把 Settings / 池 / 限流器 / 熔断 / 守卫 / writer 组装成一个 Runtime。

架构依据：docs/01-architecture.md §3（「interfaces 可以 import application 与
infrastructure，wiring 的地方」）。这里是全仓**唯一**把 infra 实现注入 application 的地方。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from ..application.alert_service import AlertService, load_rules
from ..application.config import Settings
from ..application.orchestrator import CycleContext, Orchestrator
from ..application.rates import compile_politeness
from ..domain.models import CycleReport
from ..infrastructure.adapters.registry import build_default_registry
from ..infrastructure.alerting.sqlite_sink import SqliteAlertSink
from ..infrastructure.alerting.stdout_sink import StdoutAlertSink
from ..infrastructure.http.breaker import BreakerConfig, CircuitBreaker
from ..infrastructure.http.client_pool import ClientPool
from ..infrastructure.http.fetcher import Fetcher
from ..infrastructure.http.metrics import Metrics
from ..infrastructure.http.rate_limiter import HostRateLimiter
from ..infrastructure.http.robots import RobotsGuard
from ..infrastructure.storage.writer import SqliteWriter
from ..logging_setup import get_logger

__all__ = ["Runtime", "build_runtime"]

_log = get_logger("market_watch.wiring")

DEFAULT_WATCHLIST: Final[tuple[int, ...]] = (24, 68, 19721, 46731)
DEFAULT_ITEM_IDS: Final[tuple[int, ...]] = (24, 68)


@dataclass(slots=True)
class Runtime:
    """一组已装配好的运行期对象。"""

    settings: Settings
    orchestrator: Orchestrator
    fetcher: Fetcher
    limiter: HostRateLimiter
    breaker: CircuitBreaker
    robots: RobotsGuard
    metrics: Metrics
    writer: SqliteWriter
    registry: Any

    async def run_source(
        self,
        source_id: str,
        cycle_id: str,
        *,
        dry_run: bool = False,
        max_pages: int = 200,
        cursor: dict[str, str] | None = None,
    ) -> CycleReport:
        ctx = CycleContext(cycle_id, source_id, dry_run=dry_run, max_pages=max_pages, cursor=cursor)
        report = await self.orchestrator.run_cycle(ctx)
        await self.writer.flush(timeout_s=10.0)
        return report

    async def aclose(self) -> None:
        """退出序列：writer drain → fetcher/pool 关闭。"""
        await self.writer.aclose()
        await self.fetcher.aclose()
        _log.info("runtime.closed")


async def build_runtime(settings: Settings, *, dry_run: bool = False) -> Runtime:
    """组装 Runtime。全部限额参数来自 `settings`（已过 C3 钳制）。"""
    registry = build_default_registry(
        page_size=settings.page_size,
        watchlist=DEFAULT_WATCHLIST,
        item_ids=DEFAULT_ITEM_IDS,
        version=settings.version,
        contact_url=settings.contact_url,
    )

    # ---- 礼貌预算：C3 钳制在此生效 ----
    book = compile_politeness(settings, registry.politeness_map(), registry.host_map())

    limiter = HostRateLimiter(global_inflight=settings.global_inflight)
    for source_id in registry.source_ids():
        pol = book.for_source(source_id)
        limiter.configure(registry.host_of(source_id), pol.rps, pol.burst)

    breaker = CircuitBreaker(
        BreakerConfig(
            window_s=settings.breaker_window_s,
            min_samples=settings.breaker_min_samples,
            failure_ratio=settings.breaker_failure_ratio,
            consecutive_5xx=settings.breaker_consecutive_5xx,
            open_duration_s=settings.breaker_open_duration_s,
            half_open_probes=settings.breaker_half_open_probes,
            half_open_success_to_close=settings.breaker_half_open_success_to_close,
        )
    )

    robots = RobotsGuard(
        settings.user_agent,
        timeout_s=settings.connect_timeout_s,
        on_deny=lambda host, path: _log.error("robots.denied host=%s path=%s", host, path),
    )

    writer = SqliteWriter(
        settings.db_path,
        queue_maxsize=settings.writer_queue_size,
        max_batch=settings.writer_max_batch,
    )
    writer.start()

    metrics = Metrics(fetch_log_sink=lambda row: writer.submit(("fetch_log", [row])))

    pool = ClientPool(
        timeout=settings.httpx_timeout(),
        limits=settings.httpx_limits(),
        headers={"User-Agent": settings.user_agent, "Accept": "application/json, text/xml;q=0.9"},
        proxy_url=settings.proxy_url,
        http2=settings.http2,
        # 池重建 → rate *= 0.7（比 429 轻柔的自适应减速，§4.5 信号表）。
        # `reason` 是回调契约的一部分（`ClientPool` 会传），此处只做降速不区分原因。
        on_rebuild=lambda host, reason="pool_rebuilt": limiter.penalize(host, 0.7),  # noqa: ARG005
    )

    fetcher = Fetcher(
        pool=pool,
        limiter=limiter,
        breaker=breaker,
        robots=robots,
        metrics=metrics,
        user_agent=settings.user_agent,
        probe_rate_limit_header=settings.ratelimit_header_probe,
    )

    rules = load_rules(settings.config_dir / "alerts.yaml")
    alert_service = AlertService(
        sinks=(StdoutAlertSink(), SqliteAlertSink(writer)),
        rules=rules,
    )

    orchestrator = Orchestrator(
        adapters={sid: registry.get(sid) for sid in registry.source_ids()},
        fetcher=fetcher,
        sink=writer,
        limiter=limiter,
        breaker=breaker,
        alert_service=alert_service,
    )

    _log.info(
        "runtime.built sources=%s rps_cap=%.2f watchlist=%d db=%s",
        ",".join(registry.source_ids()),
        settings.per_host_rps,
        len(DEFAULT_WATCHLIST),
        settings.db_path,
    )
    return Runtime(
        settings=settings,
        orchestrator=orchestrator,
        fetcher=fetcher,
        limiter=limiter,
        breaker=breaker,
        robots=robots,
        metrics=metrics,
        writer=writer,
        registry=registry,
    )
