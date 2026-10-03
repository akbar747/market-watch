# src/market_watch/application/scheduler.py
"""周期调度：价格/挂单/元数据/公告周期，以及 `cycle_id` 分配。

架构依据：docs/01-architecture.md §5.4（调度周期）、§4.4（`next_run_at` 硬约束）、
§14 P1（上一轮未结束则跳过本轮并记 `cycle.skipped`）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
import uuid

from ..domain.models import utc_now
from .config import Settings
from .rates import next_run_at

__all__ = ["SOURCE_INTERVAL_FIELD", "CyclePlan", "Scheduler", "new_cycle_id"]

#: source_id → Settings 里的间隔字段名
SOURCE_INTERVAL_FIELD: Final[dict[str, str]] = {
    "gw2_prices": "prices_interval_s",
    "gw2_listings": "listings_interval_s",
    "gw2_items": "items_interval_s",
    "gw2_news_rss": "news_interval_s",
}


def new_cycle_id() -> str:
    """cycle_id：`YYYYmmddTHHMMSSZ-<8hex>`。可读 + 全局唯一。"""
    return f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


@dataclass(slots=True)
class CyclePlan:
    """一个源的一次可运行计划。"""

    source_id: str
    cycle_id: str
    scheduled_at: datetime
    interval_s: int


@dataclass(slots=True)
class _SourceClock:
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_max_age_s: int | None = None
    running: bool = False
    consecutive_failures: int = 0


@dataclass(slots=True)
class Scheduler:
    """决定「本轮该跑哪些源」。

    - `due()` 返回到期的源；**上一轮仍在 running 的源直接跳过**并记 `cycle.skipped`（§14 P1）。
    - `mark_done()` 里按 `next_run_at` 推进，尊重 `Cache-Control: max-age`。
    """

    settings: Settings
    sources: tuple[str, ...]
    clocks: dict[str, _SourceClock] = field(default_factory=dict)
    _on_skip: Callable[[str, str], None] | None = None

    def __post_init__(self) -> None:
        for sid in self.sources:
            self.clocks.setdefault(sid, _SourceClock())

    def interval_for(self, source_id: str) -> int:
        field_name = SOURCE_INTERVAL_FIELD.get(source_id)
        if field_name is None:
            return self.settings.prices_interval_s
        return int(getattr(self.settings, field_name))

    def due(self, now: datetime | None = None) -> list[CyclePlan]:
        now = now or utc_now()
        plans: list[CyclePlan] = []
        for sid in self.sources:
            clock = self.clocks[sid]
            if clock.running:
                if self._on_skip is not None:
                    self._on_skip(sid, "上一轮未结束")
                continue
            if clock.last_run_at is None:
                plans.append(CyclePlan(sid, new_cycle_id(), now, self.interval_for(sid)))
                continue
            gate = next_run_at(
                clock.last_run_at,
                self.interval_for(sid),
                clock.last_max_age_s,
                respect_max_age=True,
                now=now,
            )
            if now >= gate:
                plans.append(CyclePlan(sid, new_cycle_id(), now, self.interval_for(sid)))
        return plans

    def mark_running(self, source_id: str) -> None:
        self.clocks[source_id].running = True

    def mark_done(
        self,
        source_id: str,
        *,
        ok: bool,
        max_age_s: int | None = None,
        now: datetime | None = None,
    ) -> None:
        now = now or utc_now()
        clock = self.clocks[source_id]
        clock.running = False
        clock.last_run_at = now
        if ok:
            clock.last_success_at = now
            clock.consecutive_failures = 0
        else:
            clock.consecutive_failures += 1
        if max_age_s is not None:
            clock.last_max_age_s = max_age_s

    def next_deadline(self, now: datetime | None = None) -> datetime | None:
        """最近一个到期时刻，供 `serve` 决定 sleep 多久。"""
        now = now or utc_now()
        candidates: list[datetime] = []
        for sid in self.sources:
            clock = self.clocks[sid]
            if clock.running:
                continue
            if clock.last_run_at is None:
                candidates.append(now)
                continue
            candidates.append(
                next_run_at(
                    clock.last_run_at,
                    self.interval_for(sid),
                    clock.last_max_age_s,
                    respect_max_age=True,
                    now=now,
                )
            )
        return min(candidates) if candidates else None


async def run_forever(
    scheduler: Scheduler,
    run_cycle: Callable[[CyclePlan], object],
    *,
    stop: asyncio.Event,
    poll_interval_s: float = 1.0,
) -> None:
    """常驻调度循环。`stop` 被 set 后不再接受新周期（§4.6 退出序列第 2 步）。"""
    while not stop.is_set():
        plans = scheduler.due()
        for plan in plans:
            scheduler.mark_running(plan.source_id)
            try:
                result = run_cycle(plan)
                if asyncio.iscoroutine(result):
                    await result
                scheduler.mark_done(plan.source_id, ok=True)
            except asyncio.CancelledError:
                scheduler.mark_done(plan.source_id, ok=False)
                raise
            except Exception:  # noqa: BLE001 - 单源失败不得拖垮调度循环
                scheduler.mark_done(plan.source_id, ok=False)
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval_s)
        except TimeoutError:
            continue
