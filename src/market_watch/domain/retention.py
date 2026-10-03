# src/market_watch/domain/retention.py
"""保留窗口计算（表 → cutoff 时间戳）。纯函数，便于单测。

架构依据：docs/01-architecture.md §8.4（保留策略与清理）。

顺序铁律（§8.4）：**先 rollup → 再打 rolled_up 标记 → 再删 change → 最后 vacuum**。
颠倒会永久丢数据。本模块只负责算 cutoff，执行顺序由 `storage/maintenance.py` 保证。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Final

from .models import iso_z, utc_now

__all__ = [
    "RETENTION",
    "bucket_key",
    "cutoff_for",
    "cutoffs",
    "hour_buckets_between",
    "time_column",
]

#: 表 → 保留窗口。`None` = 不删除。
RETENTION: Final[Mapping[str, timedelta | None]] = {
    "price_change": timedelta(hours=72),
    "listing_level": timedelta(hours=48),
    "price_hourly": timedelta(days=180),  # 且仅 TopN 物品在列
    "fetch_log": timedelta(days=14),  # 合规审计留痕
    "data_reject": timedelta(days=14),
    "alert_event": timedelta(days=90),  # acknowledged=1 才允许删
    "price_latest": None,  # 最新态不删
    "announcement": None,  # 公告永久保留（量小）
}

#: 表 → 用于比较的时间列名（默认 observed_at）
_TIME_COLUMN: Final[Mapping[str, str]] = {
    "price_change": "observed_at",
    "listing_level": "observed_at",
    "price_hourly": "bucket_ts",
    "fetch_log": "ts",
    "data_reject": "created_at",
    "alert_event": "fired_at",
}


def cutoff_for(table: str, now: datetime | None = None) -> str | None:
    """算单个表的 cutoff（ISO8601 `'Z'` 字符串）。

    Args:
        table: 表名，取值见 `RETENTION`（如 `price_change` / `fetch_log`）。
        now: 基准时刻。**可注入**是为了单测与 GC 时刻可控；缺省取 `utc_now()`。

    Returns:
        该表的 cutoff 字符串；`None` 表示该表不清理（`RETENTION` 里窗口为 `None` 的表，
        如 `price_latest` / `announcement`）。未知表名同样返回 `None`（保守：不清理，
        避免以后加表时把 GC 打挂）。
    """
    window = RETENTION.get(table)
    if window is None:
        return None
    now = now or utc_now()
    return iso_z(now - window)


def cutoffs(now: datetime | None = None) -> dict[str, str]:
    """算**全部**可清理表的 cutoff。

    Args:
        now: 基准时刻，语义同 `cutoff_for`，缺省取 `utc_now()`。

    Returns:
        `{表名: cutoff_ISO8601}`，只包含窗口非 `None` 的表（最新态与公告不删）。
    """
    now = now or utc_now()
    return {table: iso_z(now - w) for table, w in RETENTION.items() if w is not None}


def time_column(table: str) -> str:
    """取出该表用于比较时间先后列的列名（供 `maintenance.py` 拼 DELETE 的 WHERE）。

    Args:
        table: 表名。

    Returns:
        列名字符串；未知表回落到 `'observed_at'`（保守：宁可删不掉也不删错列）。
    """
    return _TIME_COLUMN.get(table, "observed_at")


def bucket_key(ts: datetime) -> str:
    """算小时桶键。

    Args:
        ts: 任意 timezone-aware 时间戳。

    Returns:
        `'YYYY-MM-DDTHH:00:00Z'`——与 `price_hourly.bucket_ts` 的格式严格一致
        （`iso_z` 会先归一到 UTC，所以入参带不带 tz 都安全）。
    """
    return iso_z(ts.replace(minute=0, second=0, microsecond=0))


def hour_buckets_between(start: datetime, end: datetime) -> list[str]:
    """枚举 rollup 需要覆盖的小时桶。

    Args:
        start: 左闭（`[start`），会被对齐到整点。
        end: 右开（`end)`），不与 `end` 所在小时相交。

    Returns:
        桶键列表（升序、无重复）。`start >= end` 时返回空列表。
    """
    buckets: list[str] = []
    cursor = start.replace(minute=0, second=0, microsecond=0)
    while cursor < end:
        buckets.append(bucket_key(cursor))
        cursor += timedelta(hours=1)
    return buckets
