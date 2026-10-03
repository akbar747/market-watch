# src/market_watch/infrastructure/storage/maintenance.py
"""保留清理、rollup、`PRAGMA optimize` / `incremental_vacuum`。

架构依据：docs/01-architecture.md §8.4。

★ **顺序铁律：先 rollup → 打 `rolled_up` 标记 → 再删 change → 最后 vacuum。**
颠倒会永久丢数据。

★ Phase 1 实跑发现的缺陷修复（必须保留）：
若只有「rollup 后再删」而没有 `rolled_up` 标记，进程在 rollup 与 delete 之间崩溃时，
重跑会把同一批 `price_change` 行**再滚一次**，`price_hourly.samples` 翻倍。
因此步骤 1b 的打标记与 1a 的 rollup **在同一事务**内完成，且 rollup 只吃
`rolled_up = 0` 的行（幂等）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import time
from typing import Final

from ...domain.retention import RETENTION, bucket_key, cutoffs, hour_buckets_between, time_column
from ...logging_setup import get_logger
from .sqlite import apply_migrations, connect, pragma_optimize

__all__ = ["MARK_ROLLED_UP_SQL", "ROLLUP_SQL", "Maintenance", "run_gc"]

_log = get_logger("market_watch.maintenance")


# ---------------------------------------------------------------------------
# 步骤 1a：rollup（§8.3 / §8.4 同款语句），只滚 rolled_up=0 的行 → 幂等
# ---------------------------------------------------------------------------
ROLLUP_SQL: Final[str] = """
INSERT INTO price_hourly (item_id, bucket_ts, open_sell, high_sell, low_sell, close_sell,
                          avg_sell, vwap_sell, spread, depth_buy, depth_sell, samples)
SELECT pc.item_id, :bucket,
       COALESCE(
           (SELECT ph.open_sell FROM price_hourly ph
             WHERE ph.item_id = pc.item_id AND ph.bucket_ts = :bucket),
           (SELECT pc0.sell_unit_price FROM price_change pc0
             WHERE pc0.item_id = pc.item_id
               AND pc0.observed_at >= :bucket_start AND pc0.observed_at < :bucket_end
             ORDER BY pc0.observed_at ASC LIMIT 1)
       ),
       MAX(pc.sell_unit_price),
       MIN(pc.sell_unit_price),
       (SELECT pc2.sell_unit_price FROM price_change pc2
         WHERE pc2.item_id = pc.item_id
           AND pc2.observed_at >= :bucket_start AND pc2.observed_at < :bucket_end
         ORDER BY pc2.observed_at DESC LIMIT 1),
       AVG(pc.sell_unit_price),
       SUM(pc.sell_unit_price * pc.sell_quantity) / NULLIF(SUM(pc.sell_quantity), 0),
       AVG(pc.sell_unit_price - pc.buy_unit_price),
       SUM(pc.buy_quantity), SUM(pc.sell_quantity), COUNT(*)
FROM price_change pc
WHERE pc.observed_at >= :bucket_start AND pc.observed_at < :bucket_end
  AND pc.rolled_up = 0                            -- 幂等关键：只滚未被滚过的行
GROUP BY pc.item_id
ON CONFLICT(item_id, bucket_ts) DO UPDATE SET
  high_sell  = MAX(high_sell,  excluded.high_sell),
  low_sell   = MIN(low_sell,   excluded.low_sell),
  close_sell = excluded.close_sell,
  avg_sell   = excluded.avg_sell,
  vwap_sell  = excluded.vwap_sell,
  spread     = excluded.spread,
  depth_buy  = excluded.depth_buy,
  depth_sell = excluded.depth_sell,
  samples    = price_hourly.samples + excluded.samples;
"""

# ---------------------------------------------------------------------------
# 步骤 1b：打标记（与 1a 同一事务）
# ---------------------------------------------------------------------------
MARK_ROLLED_UP_SQL: Final[str] = """
UPDATE price_change SET rolled_up = 1
 WHERE observed_at >= :bucket_start AND observed_at < :bucket_end AND rolled_up = 0;
"""


class Maintenance:
    """GC 执行器。用**写连接**（与 writer 不同连接，但同一单写者模型下串行调用）。"""

    __slots__ = ("_db_path",)

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)

    def gc(self, now: datetime | None = None, *, topn_hourly: int = 2000) -> dict[str, object]:
        """执行一次完整 GC。返回统计字典（供 `cli gc` 打印）。

        顺序：rollup(+标记) → 删除过期 → PRAGMA optimize → incremental_vacuum。
        """
        from ...domain.models import utc_now

        now = now or utc_now()
        stats: dict[str, object] = {}
        conn = connect(self._db_path)
        try:
            # ★ 迁移只在 writer 那边跑过（writer.py:169）。维护通路先于 writer
            # 打开就会看到空库，2026-10-03 实跑 `market-watch gc` 全新安装时
            # 直接 `sqlite3.OperationalError: no such table: price_hourly` 崩掉。
            # 这里补一次幂等迁移，保证 maintenance 自己不会撞上「表不存在」。
            apply_migrations(conn)
            stats["rolled_buckets"] = self._rollup(conn, now)
            stats["deleted"] = self._delete_expired(conn, now, topn_hourly=topn_hourly)
            pragma_optimize(conn)
            try:
                conn.execute("PRAGMA incremental_vacuum")
            except sqlite3.DatabaseError as exc:
                _log.warning("incremental_vacuum.failed err=%s", exc)
        finally:
            conn.close()
        stats["elapsed_ms"] = 0.0
        return stats

    def _rollup(self, conn: sqlite3.Connection, now: datetime) -> int:
        """把「即将过期」窗口内的 price_change 滚入 price_hourly。

        覆盖窗口 = [price_change 保留期的起点, now)，即所有还在库里的行都滚一遍；
        因为打了 `rolled_up` 标记，重复执行不会翻倍（幂等）。
        """
        window = RETENTION.get("price_change")
        if window is None:
            return 0
        start = now - window
        buckets = hour_buckets_between(start, now)
        done = 0
        t0 = time.monotonic()
        with conn:  # rollup 与打标记在同一事务
            for bucket in buckets:
                bucket_start = bucket
                bucket_end = bucket_key(
                    datetime.fromisoformat(bucket.replace("Z", "+00:00")) + timedelta(hours=1)
                )
                cur = conn.execute(
                    ROLLUP_SQL,
                    {"bucket": bucket, "bucket_start": bucket_start, "bucket_end": bucket_end},
                )
                cur.close()
                cur = conn.execute(
                    MARK_ROLLED_UP_SQL,
                    {"bucket_start": bucket_start, "bucket_end": bucket_end},
                )
                marked = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                cur.close()
                if marked:
                    done += 1
        _log.info(
            "gc.rollup buckets=%d with_rows=%d elapsed_ms=%.1f",
            len(buckets),
            done,
            (time.monotonic() - t0) * 1000.0,
        )
        return done

    def _delete_expired(
        self, conn: sqlite3.Connection, now: datetime, *, topn_hourly: int
    ) -> dict[str, int]:
        """步骤 2：删过期数据（**必须在 rollup 之后**）。"""
        cuts = cutoffs(now)
        deleted: dict[str, int] = {}
        with conn:
            for table, cutoff in cuts.items():
                if table == "alert_event":
                    # ★ 必须跳过通用删除：alert_event 只有 acknowledged=1 才允许删（§8.4）。
                    # 原先这里会先无差别 DELETE ... WHERE fired_at < cutoff，
                    # 把未确认的告警也一起清掉，下面那条 `AND acknowledged = 1` 就成了死代码。
                    continue
                col = time_column(table)
                cur = conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
                deleted[table] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                cur.close()
            # alert_event：acknowledged=1 才允许删（§8.4）
            cur = conn.execute(
                "DELETE FROM alert_event WHERE fired_at < ? AND acknowledged = 1",
                (cuts["alert_event"],),
            )
            deleted["alert_event"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            cur.close()
            # price_hourly：仅清理不在观测清单里的（§8.4 保留 TopN）
            cur = conn.execute(
                "DELETE FROM price_hourly WHERE bucket_ts < ? "
                "AND item_id NOT IN (SELECT item_id FROM watchlist)",
                (cuts["price_hourly"],),
            )
            deleted["price_hourly"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            cur.close()
        _log.info("gc.deleted %s", deleted)
        return deleted


def run_gc(db_path: Path | str, *, now: datetime | None = None) -> dict[str, object]:
    """CLI `gc` 的入口。"""
    return Maintenance(db_path).gc(now)
