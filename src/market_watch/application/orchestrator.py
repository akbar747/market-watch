# src/market_watch/application/orchestrator.py
"""一轮采集的完整编排：fanout → fetch → parse → validate → sink → detect。

架构依据：docs/01-architecture.md §3（职责表）、§13（`run_cycle(ctx) -> CycleReport`）。

依赖方向：本模块**只 import domain + 通过构造函数注入的 infrastructure 实例**，
不 `import infrastructure`（§3 铁律 2，防循环）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import time
from typing import Final

from ..domain.consistency import check_universe_size
from ..domain.errors import ErrorClass
from ..domain.models import (
    Announcement,
    CycleReport,
    FetchTask,
    ItemMeta,
    ListingLevel,
    PriceTick,
    Record,
    iso_z,
    utc_now,
)
from ..domain.ports import (
    Breaker,
    Fetcher,
    Politeness,
    RateLimiter,
    SnapshotSink,
    SourceAdapter,
)
from ..logging_setup import get_logger
from .alert_service import AlertService
from .dedupe import LastSeenTracker
from .fanout import drive
from .news_ingest import NewsIngest
from .price_ingest import PriceIngest

__all__ = ["MAX_CONSECUTIVE_SCHEMA_ERRORS", "CycleContext", "Orchestrator"]

_log = get_logger("market_watch.orchestrator")

#: §6 #11 / §14 P0：连续 ≥20 个 item 的 Schema 失败 → 判定端点改版，停机
MAX_CONSECUTIVE_SCHEMA_ERRORS: Final[int] = 20


class CycleContext:
    """一轮的上下文。由 `scheduler` 产出，`run_cycle` 消费。"""

    __slots__ = ("cursor", "cycle_id", "dry_run", "max_pages", "source_id", "started_at")

    def __init__(
        self,
        cycle_id: str,
        source_id: str,
        *,
        started_at: datetime | None = None,
        cursor: dict[str, str] | None = None,
        dry_run: bool = False,
        max_pages: int = 200,
    ) -> None:
        self.cycle_id = cycle_id
        self.source_id = source_id
        self.started_at = started_at or utc_now()
        self.cursor = cursor
        self.dry_run = dry_run
        self.max_pages = max_pages


class Orchestrator:
    """一轮编排的总导演。"""

    __slots__ = (
        "_adapters",
        "_alerts",
        "_breaker",
        "_consecutive_schema_errors",
        "_fetcher",
        "_limiter",
        "_news",
        "_prev_universe",
        "_price",
        "_sink",
        "_trackers",
    )

    def __init__(
        self,
        *,
        adapters: dict[str, SourceAdapter],
        fetcher: Fetcher,
        sink: SnapshotSink,
        limiter: RateLimiter,
        breaker: Breaker,
        alert_service: AlertService,
    ) -> None:
        self._adapters = dict(adapters)
        self._fetcher = fetcher
        self._sink = sink
        self._limiter = limiter
        self._breaker = breaker
        self._alerts = alert_service
        self._trackers: dict[str, LastSeenTracker] = {}
        self._news = NewsIngest(sink)
        self._price = PriceIngest(sink)
        self._prev_universe: dict[str, int] = {}
        self._consecutive_schema_errors: dict[str, int] = {}

    # ---- 主入口 ----
    async def run_cycle(self, ctx: CycleContext) -> CycleReport:
        """跑一轮。返回 `CycleReport`。"""
        adapter = self._adapters[ctx.source_id]
        t0 = time.monotonic()
        observed_at = ctx.started_at

        # 停机级预检：熔断 open 时直接跳过（不消耗额度）
        if self._breaker.state(adapter.host) == "open":
            _log.warning("cycle.skipped source=%s reason=breaker_open", ctx.source_id)
            return CycleReport(
                cycle_id=ctx.cycle_id,
                source_id=ctx.source_id,
                error_class=ErrorClass.UNKNOWN,
            )

        # 注入轮次上下文（adapter 是纯函数，时间/cycle_id 必须由调用方注入）
        tasks = [
            _stamp(task, observed_at, ctx.cycle_id) for task in adapter.initial_tasks(ctx.cursor)
        ]
        if not tasks:
            _log.info("cycle.no_tasks source=%s（清单为空或源未启用）", ctx.source_id)
            return CycleReport(cycle_id=ctx.cycle_id, source_id=ctx.source_id)

        pol: Politeness = adapter.politeness

        # ---- 取数一次（★ 只调 drive 一次：重复调用会翻倍消耗官方配额） ----
        # persist=False：落库由本层按 kind 分派到 price/news/items 的专用 ingest，
        # records 通过 on_page 回调累积（避免二次取数）。
        collected = _PageCollector()
        report = await drive(
            tasks,
            adapter,
            self._fetcher,
            self._sink,
            workers=pol.concurrency,
            cycle_id=ctx.cycle_id,
            source_id=ctx.source_id,
            max_pages=ctx.max_pages,
            persist=False,
            on_page=collected,
        )

        written = 0
        skipped = 0
        rejects = 0
        alerts = 0
        quality = report.quality
        records = list(collected.records)

        # 一致性违规 → data_reject + 隔离
        violations = [v for p in collected.pages for v in p.consistency_failures]
        if violations:
            rejects += self._price.quarantine(
                violations, cycle_id=ctx.cycle_id, source_id=ctx.source_id
            )

        # universe 漂移（§11.1-2：本轮总数相对上轮波动 ≤ ±5%）
        drift_violations = self._check_universe(ctx.source_id, report)
        rejects += len(drift_violations)
        if drift_violations:
            quality = "PARTIAL"

        # Schema 失败计数（§14 P0：≥20 → 停机）
        self._count_schema(ctx.source_id, violations)

        kind = adapter.kind
        if kind == "price":
            ticks = [r for r in records if isinstance(r, PriceTick)]
            # 隔离：命中 critical 违规的 item 不进异动检测
            quarantined = {
                v.item_id for v in violations if v.item_id is not None and v.severity == "critical"
            }
            clean = [t for t in ticks if t.item_id not in quarantined]
            tracker = self._tracker_for(ctx.source_id)
            self._price._tracker = tracker
            written, skipped = self._price.ingest(clean, cycle_id=ctx.cycle_id, quality=quality)
            prev_map = {t.item_id: _prev_values(tracker, t) for t in clean}
            events = self._alerts.evaluate(
                clean,
                prev_values=prev_map,
                quality=quality,
                now=observed_at,
            )
            alerts = len(events)
        elif kind == "orderbook":
            levels = [r for r in records if isinstance(r, ListingLevel)]
            written = self._write_listings(levels, ctx.cycle_id)
        elif kind == "item_meta":
            metas = [r for r in records if isinstance(r, ItemMeta)]
            written = self._write_items(metas)
        elif kind == "announcement":
            anns = [r for r in records if isinstance(r, Announcement)]
            written, _ = self._news.ingest(anns)

        duration_ms = (time.monotonic() - t0) * 1000.0
        _log.info(
            "cycle.done cycle=%s source=%s pages=%d records=%d written=%d skipped=%d "
            "rejects=%d alerts=%d quality=%s drift=%.2f%% duration_ms=%.1f",
            ctx.cycle_id,
            ctx.source_id,
            report.pages,
            report.records,
            written,
            skipped,
            rejects,
            alerts,
            quality,
            report.drift_ratio * 100,
            duration_ms,
        )
        return CycleReport(
            cycle_id=ctx.cycle_id,
            source_id=ctx.source_id,
            pages=report.pages,
            records=report.records,
            written=written,
            skipped_unchanged=skipped,
            rejects=rejects,
            alerts=alerts,
            quality=quality,
            duration_ms=duration_ms,
            drift_ratio=report.drift_ratio,
        )

    # ---- 内部 ----
    def _tracker_for(self, source_id: str) -> LastSeenTracker:
        tracker = self._trackers.get(source_id)
        if tracker is None:
            tracker = LastSeenTracker()
            self._trackers[source_id] = tracker
        return tracker

    def _check_universe(self, source_id: str, report) -> list:  # noqa: ANN001
        if not report.page_totals:
            return []
        # 用 X-Result-Total 的代理：page_totals 的最大值 * page_size 不精确，
        # 因此这里直接比对「本轮页数」的相对波动（漂移信号取自 report）
        curr = max(report.page_totals)
        prev = self._prev_universe.get(source_id)
        self._prev_universe[source_id] = curr
        return check_universe_size(prev, curr)

    def _count_schema(self, source_id: str, violations: Sequence) -> None:
        schema_hits = sum(1 for v in violations if v.rule.startswith("schema."))
        if schema_hits:
            self._consecutive_schema_errors[source_id] = (
                self._consecutive_schema_errors.get(source_id, 0) + schema_hits
            )
        else:
            self._consecutive_schema_errors[source_id] = 0
        total = self._consecutive_schema_errors.get(source_id, 0)
        if total >= MAX_CONSECUTIVE_SCHEMA_ERRORS:
            _log.error(
                "cycle.schema_error_threshold source=%s hits=%d >= %d -> 判定端点改版，停机排查"
                "（§14 P0 / §6 #11）",
                source_id,
                total,
                MAX_CONSECUTIVE_SCHEMA_ERRORS,
            )

    def _write_listings(self, levels: Sequence[ListingLevel], cycle_id: str) -> int:
        if not levels:
            return 0
        rows = [
            (
                lv.item_id,
                lv.side,
                lv.unit_price,
                lv.quantity,
                lv.listings,
                iso_z(lv.observed_at),
                cycle_id,
            )
            for lv in levels
        ]
        # 外键：先补 items 父行
        self._sink.submit(
            (
                "items",
                [(lv.item_id, "", None, None, iso_z(utc_now()), iso_z(utc_now())) for lv in levels],
            )
        )
        self._sink.submit(("listing_level", rows))
        return len(rows)

    def _write_items(self, metas: Sequence[ItemMeta]) -> int:
        if not metas:
            return 0
        rows = [
            (m.item_id, m.name, m.item_type, m.rarity, iso_z(m.updated_at), iso_z(utc_now()))
            for m in metas
        ]
        self._sink.submit(("items", rows))
        return len(rows)

    def persist_source_state(
        self,
        *,
        source_id: str,
        cycle_id: str,
        ok: bool,
        error_class: ErrorClass,
        cursor: dict[str, str] | None,
        robots_allowed: bool,
        robots_checked_at: str | None,
    ) -> None:
        """落 `source_state`（§8.2）。失败计数用于 scheduler 的连续失败判定。"""
        now_iso = iso_z(utc_now())
        self._sink.submit(
            (
                "source_state",
                [
                    (
                        source_id,
                        _cursor_to_str(cursor),
                        cycle_id,
                        now_iso,
                        now_iso if ok else None,
                        str(error_class),
                        0 if ok else 1,
                        robots_checked_at,
                        1 if robots_allowed else 0,
                        now_iso,
                    )
                ],
            )
        )


# ---------------------------------------------------------------------------
def _stamp(task: FetchTask, observed_at: datetime, cycle_id: str) -> FetchTask:
    """给 task 注入轮次时间与 cycle_id（adapter 的 parse 是纯函数，不能自己取）。"""
    from dataclasses import replace

    return replace(task, observed_at=observed_at, cycle_id=cycle_id)


def _prev_values(tracker: LastSeenTracker, tick: PriceTick) -> dict[str, float]:
    """从 tracker 取上一轮数值（供环比规则）。"""
    seen = tracker.peek(tick.item_id)
    if seen is None:
        return {}
    buy_px, buy_qty, sell_px, sell_qty = seen
    return {
        "buy_unit_price": float(buy_px),
        "buy_quantity": float(buy_qty),
        "sell_unit_price": float(sell_px),
        "sell_quantity": float(sell_qty),
    }


def _cursor_to_str(cursor: dict[str, str] | None) -> str | None:
    if not cursor:
        return None
    import json

    return json.dumps(cursor, ensure_ascii=False, sort_keys=True)


class _PageCollector:
    """`drive(on_page=...)` 的回调：累积 records 与 pages（避免二次取数）。"""

    __slots__ = ("pages", "records")

    def __init__(self) -> None:
        self.records: list[Record] = []
        self.pages: list = []

    def __call__(self, parsed) -> None:  # noqa: ANN001
        self.pages.append(parsed)
        self.records.extend(parsed.records)
