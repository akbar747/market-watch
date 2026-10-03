# src/market_watch/application/fanout.py
"""分页扇出与「下一批任务」推进，含漂移检测。

架构依据：docs/01-architecture.md §4.4（两阶段渐进扇出 + 单管道回填）、§6.1（分页漂移）。

设计要点：
- **页数未知时绝不"预测性全部并发"**。阶段 A 探路拿 `X-Page-Total`，阶段 B 用
  `asyncio.Queue(maxsize = workers*2)` + N 个 worker 推进。
- 在飞页数 ≤ `workers(4)`；内存有界；队列长度 = 剩余页数（看板进度条）。
- 幂等键是 `(cycle_id, item_id)`，**绝不用 page 当幂等键**（§6.1-5）。
- 漂移处理（§6.1）：各页 `X-Page-Total` 不一致 → 记 `pagination.drifted` 但**不重试**；
  漂移率 > 10% → 本轮标记 `PARTIAL`。
- **终止性硬护栏**：即便恶意端点永远返回同样的 `X-Page-Total`，也必须靠
  `max_pages` 与「已见 page 集合」在有限步内收敛（契约测试第 5 条）。

并发记账（避免死锁的关键）：
    `pending` = 已入队但未处理完的任务数（含正在处理的）。worker 只有在
    `pending == 0` 时才允许退出；子任务入队发生在自身 `pending -= 1` **之前**，
    因此不存在「pending 归零但还有子任务未入队」的窗口。
    队列满时 `await q.put()` 是安全的：此时 pending > 0，其余 worker 不会全部退出。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from typing import Final, Protocol

from ..domain.models import (
    Announcement,
    Cursor,
    FanoutReport,
    FetchTask,
    ItemMeta,
    ListingLevel,
    ParseResult,
    PriceTick,
    Quality,
    Record,
    ResponseView,
    iso_z,
    utc_now,
)
from ..domain.ports import Fetcher, SnapshotSink
from ..logging_setup import get_logger

__all__ = ["MAX_PAGES_HARD_LIMIT", "drive", "route_batch"]

_log = get_logger("market_watch.fanout")

#: 终止性硬护栏：单轮页数绝对上限（防御恶意/异常端点永不收敛）
MAX_PAGES_HARD_LIMIT: Final[int] = 2_000


class _AdapterLike(Protocol):
    """`drive` 需要的 adapter 子集。

    ★ 成员类型一律写**实现能接受的最小上界**，不是「看起来最宽的 `object`」：

    - 参数位（逆变）：`view` / `result` 必须写 `ResponseView` / `ParseResult`。
      标 `object` 等于要求实现把参数收窄到 `object`，而 adapter 收窄的是
      `ResponseView`，于是合法实现反而进不来。改声明而不是改实现，是因为
      改实现会逼 `drive` 内部到处断言。
    - 返回值位（协变）：`parse` 直接返回 `ParseResult`，不套一层
      「只声明字段的结构体 Protocol」。以前那个结构 Protocol 把成员声明成
      可写变量，而 `ParseResult` 是 `@dataclass(frozen=True)`（只读属性），
      于是「实现比声明窄」又被判成冲突——顺带把这层无谓的间接也去掉了。
    """

    def next_tasks(self, task: FetchTask, view: ResponseView) -> Sequence[FetchTask]: ...
    def parse(self, task: FetchTask, view: ResponseView) -> ParseResult: ...
    def cursor_from(self, view: ResponseView, result: ParseResult) -> Cursor | None: ...


async def drive(
    initial_tasks: Sequence[FetchTask],
    adapter: _AdapterLike,
    fetcher: Fetcher,
    sink: SnapshotSink,
    *,
    workers: int = 4,
    cycle_id: str,
    source_id: str,
    max_pages: int = MAX_PAGES_HARD_LIMIT,
    persist: bool = True,
    on_page: object | None = None,
) -> FanoutReport:
    """推进一个源的全部分页。

    `persist=False`（dry-run）时只解析不落库，用于 `once --dry-run` 冒烟。
    """
    if not initial_tasks:
        return FanoutReport()

    workers = max(1, int(workers))
    q: asyncio.Queue[FetchTask] = asyncio.Queue(maxsize=workers * 2)

    pending = 0
    seen_pages: set[str] = set()
    page_totals: set[int] = set()
    seen_records: set[tuple] = set()
    duplicate_items = 0
    records: list[Record] = []
    rejects = 0
    pages_done = 0
    cursor: Cursor | None = None
    lock = asyncio.Lock()

    work_event = asyncio.Event()

    async def enqueue(task: FetchTask) -> None:
        nonlocal pending
        pending += 1
        try:
            q.put_nowait(task)
        except asyncio.QueueFull:
            await q.put(task)  # 安全：此刻 pending > 0，其余 worker 不会全部退出
        work_event.set()

    async def worker() -> None:
        nonlocal pending, duplicate_items, rejects, pages_done, cursor
        while True:
            if pending == 0:
                return
            try:
                task = q.get_nowait()
            except asyncio.QueueEmpty:
                # 还有任务在其它 worker 手里：等它们产出子任务或使 pending 归零。
                # 用 Event 而非 sleep(0) 忙等——避免 3 个空闲 worker 空转烧 CPU。
                work_event.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(work_event.wait(), timeout=0.05)
                continue

            try:
                page_key = f"{task.path}|{dict(task.params).get('page', '')}"

                # --- 终止性护栏 1：重复 page 编号 ---
                if page_key in seen_pages:
                    _log.warning("pagination.repeated_page page_key=%s -> 跳过", page_key)
                    continue
                seen_pages.add(page_key)

                # --- 终止性护栏 2：绝对页数上限 ---
                if pages_done >= max_pages:
                    _log.warning("pagination.hard_limit reached=%d -> 停止推进", max_pages)
                    continue

                view, error_class, _attempts = await fetcher.fetch(task)
                if view is None:
                    _log.error(
                        "fanout.fetch_failed source=%s task=%s error=%s",
                        source_id,
                        task.task_id,
                        error_class,
                    )
                    continue

                page_total = view.page_total
                if page_total is not None:
                    page_totals.add(page_total)

                result = adapter.parse(task, view)

                async with lock:
                    pages_done += 1
                    for rec in result.records:
                        key = _record_key(rec)
                        if key in seen_records:
                            duplicate_items += 1
                            continue
                        seen_records.add(key)
                        records.append(rec)
                    rejects += len(result.consistency_failures)
                    if result.cursor:
                        cursor = result.cursor
                    if on_page is not None:
                        on_page(result)  # type: ignore[operator]

                # 子任务入队必须在自身 pending -= 1（下面的 finally）之前
                for nxt in adapter.next_tasks(task, view):
                    if len(seen_pages) + q.qsize() >= max_pages:
                        _log.warning("pagination.hard_limit_enqueue reached=%d", max_pages)
                        break
                    await enqueue(nxt)
            finally:
                pending -= 1

    # return_pending 需要在 worker 内可见；上面嵌套定义在使用前，Python 允许（闭包延迟绑定）
    for t in initial_tasks:
        await enqueue(t)

    async with asyncio.TaskGroup() as tg:
        for _ in range(workers):
            tg.create_task(worker())

    # ---- 漂移判定（§6.1-2/3）：记录但不重试 ----
    drift_ratio = 0.0
    if len(page_totals) > 1:
        lo, hi = min(page_totals), max(page_totals)
        drift_ratio = (hi - lo) / lo if lo > 0 else 0.0
        _log.warning(
            "pagination.drifted source=%s totals=%s drift=%.2f%% -> 不重试已成功的页",
            source_id,
            sorted(page_totals),
            drift_ratio * 100,
        )
    quality: Quality = "PARTIAL" if drift_ratio > 0.10 else "COMPLETE"

    if persist and records:
        table, rows = route_batch(records, quality, cycle_id)
        if rows:
            sink.submit((table, rows))

    return FanoutReport(
        pages=pages_done,
        records=len(records),
        rejects=rejects,
        drift_ratio=drift_ratio,
        duplicates=duplicate_items,
        page_totals=tuple(sorted(page_totals)),
        quality=quality,
    )


def _record_key(rec: Record) -> tuple:
    """幂等键：(类型, item_id 或 guid)。**绝不用 page**（§6.1-5）。"""
    if isinstance(rec, Announcement):
        return ("ann", rec.guid)
    return (type(rec).__name__, rec.item_id)


def route_batch(
    records: Sequence[Record], quality: Quality, cycle_id: str
) -> tuple[str, list[tuple]]:
    """把 Record 序列路由到目标表的批参数。表名与 `0001_init.sql` 严格对应。

    单源单轮只产一种 Record 类型（适配器契约保证）；若混排，取记录数最多的表，
    其余类型仍需调用方分次提交——这里保持「一次提交一张表」的 sink 契约。
    """
    price_rows: list[tuple] = []
    listing_rows: list[tuple] = []
    item_rows: list[tuple] = []
    ann_rows: list[tuple] = []

    for r in records:
        if isinstance(r, PriceTick):
            price_rows.append(
                (
                    r.item_id,
                    iso_z(r.observed_at),
                    cycle_id,
                    r.buy_unit_price,
                    r.buy_quantity,
                    r.sell_unit_price,
                    r.sell_quantity,
                    1 if r.whitelisted else 0,
                    quality,
                )
            )
        elif isinstance(r, ListingLevel):
            listing_rows.append(
                (
                    r.item_id,
                    r.side,
                    r.unit_price,
                    r.quantity,
                    r.listings,
                    iso_z(r.observed_at),
                    cycle_id,
                )
            )
        elif isinstance(r, ItemMeta):
            item_rows.append((r.item_id, r.name, r.item_type, r.rarity, iso_z(r.updated_at)))
        elif isinstance(r, Announcement):
            ann_rows.append(
                (
                    r.source_id,
                    r.guid,
                    r.title,
                    r.link,
                    iso_z(r.published_at) if r.published_at else None,
                    r.summary,
                    r.content_hash,
                    iso_z(r.fetched_at),
                    iso_z(utc_now()),
                )
            )

    candidates = [
        ("price_latest", price_rows),
        ("listing_level", listing_rows),
        ("items", item_rows),
        ("announcement", ann_rows),
    ]
    table, rows = max(candidates, key=lambda kv: len(kv[1]))
    return table, rows
