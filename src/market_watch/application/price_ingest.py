# src/market_watch/application/price_ingest.py
"""价格用例：`prices` 全量快照 + `watchlist` 原始挂单。

架构依据：docs/01-architecture.md §3、§6.1、§8.3。

职责：
1. 「先补齐 items 行，再写 price_latest」——`price_latest.item_id` 有
   `REFERENCES items(item_id)` 外键，`foreign_keys=ON` 下缺父行会直接 23503 报错。
2. change gate：仅当四项数值任一变化时才追加 `price_change`（§8.3）。
3. 一致性违规 → `data_reject` + 隔离该快照（异动检测不消费）。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..domain.models import (
    ConsistencyViolation,
    CycleReport,
    PriceTick,
    iso_z,
    utc_now,
)
from ..domain.pricing import pct_change
from ..logging_setup import get_logger

__all__ = ["QUARANTINE_QUALITY", "PriceIngest"]

_log = get_logger("market_watch.price_ingest")

#: 被一致性规则隔离的快照标记（§11.1-3）：异动检测不得消费
QUARANTINE_QUALITY: str = "QUARANTINED"


class PriceIngest:
    """把一批 `PriceTick` 变成写入批次。纯编排，不碰 sqlite3（走 SnapshotSink）。"""

    __slots__ = ("_tracker", "_writer")

    def __init__(self, writer, tracker=None) -> None:  # noqa: ANN001
        self._writer = writer
        self._tracker = tracker

    def ingest(
        self,
        ticks: Sequence[PriceTick],
        *,
        cycle_id: str,
        quality: str = "COMPLETE",
    ) -> tuple[int, int]:
        """落 price_latest + 变化的行落 price_change。返回 `(写入行数, 跳过未变行数)`。"""
        if not ticks:
            return 0, 0

        now_iso = iso_z(utc_now())
        # 1) 先补齐 items 父行（外键要求）
        item_rows = [(t.item_id, "", None, None, now_iso, now_iso) for t in ticks]
        self._writer.submit(("items", item_rows))

        # 2) price_latest 全量 upsert
        latest_rows = [
            (
                t.item_id,
                iso_z(t.observed_at),
                cycle_id,
                t.buy_unit_price,
                t.buy_quantity,
                t.sell_unit_price,
                t.sell_quantity,
                1 if t.whitelisted else 0,
                quality if quality in ("COMPLETE", "PARTIAL") else "COMPLETE",
            )
            for t in ticks
        ]
        self._writer.submit(("price_latest", latest_rows))

        # 3) change gate：仅变化的行进 price_change
        change_rows = []
        skipped = 0
        for t in ticks:
            if self._tracker is None:
                changed, prev_sell = True, None
            else:
                changed, prev_sell = self._tracker.changed(t)
            if not changed:
                skipped += 1
                continue
            delta = pct_change(
                float(prev_sell) if prev_sell is not None else None,
                float(t.sell_unit_price) if t.sell_unit_price is not None else None,
            )
            change_rows.append(
                (
                    t.item_id,
                    iso_z(t.observed_at),
                    cycle_id,
                    t.buy_unit_price,
                    t.buy_quantity,
                    t.sell_unit_price,
                    t.sell_quantity,
                    prev_sell,
                    delta,
                )
            )
        if change_rows:
            self._writer.submit(("price_change", change_rows))

        _log.info(
            "price_ingest cycle=%s ticks=%d latest=%d changed=%d skipped=%d",
            cycle_id,
            len(ticks),
            len(latest_rows),
            len(change_rows),
            skipped,
        )
        return len(latest_rows), skipped

    def quarantine(
        self,
        violations: Sequence[ConsistencyViolation],
        *,
        cycle_id: str,
        source_id: str,
        payload: str = "",
    ) -> int:
        """一致性违规落 `data_reject`。**不静默丢弃**（§11.1-3）。"""
        if not violations:
            return 0
        now_iso = iso_z(utc_now())
        rows = [
            (
                cycle_id,
                source_id,
                v.item_id,
                v.rule,
                v.field,
                v.observed,
                v.expected,
                v.severity,
                (payload or "")[:4096],
                now_iso,
            )
            for v in violations
        ]
        self._writer.submit(("data_reject", rows))
        _log.warning("price_ingest.quarantined cycle=%s rejects=%d", cycle_id, len(rows))
        return len(rows)


def build_report(
    cycle_id: str,
    source_id: str,
    *,
    pages: int,
    records: int,
    written: int,
    skipped: int,
    rejects: int,
    alerts: int,
    quality: str,
    drift_ratio: float,
    duration_ms: float,
    error_class,  # noqa: ANN001
) -> CycleReport:
    """组装 CycleReport（保持 orchestrator 精简）。"""
    return CycleReport(
        cycle_id=cycle_id,
        source_id=source_id,
        pages=pages,
        records=records,
        written=written,
        skipped_unchanged=skipped,
        rejects=rejects,
        alerts=alerts,
        quality="PARTIAL" if quality == "PARTIAL" else "COMPLETE",
        error_class=error_class,
        duration_ms=duration_ms,
        drift_ratio=drift_ratio,
    )
