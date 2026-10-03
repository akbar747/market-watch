# src/market_watch/infrastructure/storage/writer.py
"""单写线程 + 线程安全队列批量 upsert（消解 SQLite 单写者瓶颈）。

架构依据：docs/01-architecture.md §8.1、§8.3。

要点：
- **一个专用写线程**（`sqlite3` 是阻塞 C 扩展），asyncio 侧只 `submit(batch)`（同步、不阻塞）。
- 用 `queue.Queue`（**线程安全**）而非 `asyncio.Queue`——写线程要直接消费它。
  `asyncio.Queue` 不是线程安全的，跨线程 `get_nowait` 会破坏内部状态。
- `maxsize=20000` 触发背压（§14 P1：队列满时宁可丢一轮变更也不拖垮进程）。
- `executemany` 批大小 500；写线程每次尽量取到 `MAX_BATCH=2000` 条一次性提交。
- `busy_timeout=5000` + 单写线程 ⇒ 不需要额外锁。
- 全部 upsert 用 `ON CONFLICT ... DO UPDATE`，**幂等**。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import queue
import sqlite3
import threading
import time
from typing import Final

from ...domain.models import WriteBatch
from ...logging_setup import get_logger
from .sqlite import apply_migrations, connect, pragma_optimize, wal_checkpoint_truncate

__all__ = ["EXECUTEMANY_CHUNK", "MAX_BATCH_DEFAULT", "QUEUE_MAXSIZE", "UPSERTS", "SqliteWriter"]

_log = get_logger("market_watch.writer")

QUEUE_MAXSIZE: Final[int] = 20_000
MAX_BATCH_DEFAULT: Final[int] = 2_000
EXECUTEMANY_CHUNK: Final[int] = 500

_STOP = object()  # 线程停止哨兵


# ---------------------------------------------------------------------------
# §8.3 核心 upsert 语句（幂等）
# ---------------------------------------------------------------------------
UPSERTS: Final[dict[str, str]] = {
    # price_latest：幂等覆盖 + 防乱序回写
    "price_latest": """
INSERT INTO price_latest
  (item_id, observed_at, cycle_id, buy_unit_price, buy_quantity, sell_unit_price, sell_quantity, whitelisted, quality)
VALUES (?,?,?,?,?,?,?,?,?)
ON CONFLICT(item_id) DO UPDATE SET
  observed_at     = excluded.observed_at,
  cycle_id        = excluded.cycle_id,
  buy_unit_price  = excluded.buy_unit_price,
  buy_quantity    = excluded.buy_quantity,
  sell_unit_price = excluded.sell_unit_price,
  sell_quantity   = excluded.sell_quantity,
  whitelisted     = excluded.whitelisted,
  quality         = excluded.quality
WHERE excluded.observed_at >= price_latest.observed_at;
""",
    # price_change：ux_price_change(item_id, observed_at) 保证幂等
    "price_change": """
INSERT INTO price_change
  (item_id, observed_at, cycle_id, buy_unit_price, buy_quantity, sell_unit_price, sell_quantity, prev_sell_price, delta_pct)
VALUES (?,?,?,?,?,?,?,?,?)
ON CONFLICT(item_id, observed_at) DO UPDATE SET
  cycle_id = excluded.cycle_id,
  prev_sell_price = excluded.prev_sell_price,
  delta_pct = excluded.delta_pct;
""",
    # items：首次插入，后续只更新名称与类型（空 name 不覆盖已有值——PII 命中会置空）
    "items": """
INSERT INTO items (item_id, name, item_type, rarity, first_seen_at, updated_at)
VALUES (?,?,?,?,?,?)
ON CONFLICT(item_id) DO UPDATE SET
  name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE items.name END,
  item_type = COALESCE(excluded.item_type, items.item_type),
  rarity = COALESCE(excluded.rarity, items.rarity),
  updated_at = excluded.updated_at;
""",
    # listing_level：同一 (item_id, side, unit_price, observed_at) 覆盖，天然幂等
    "listing_level": """
INSERT INTO listing_level (item_id, side, unit_price, quantity, listings, observed_at, cycle_id)
VALUES (?,?,?,?,?,?,?)
ON CONFLICT(item_id, side, unit_price, observed_at) DO UPDATE SET
  quantity = excluded.quantity, listings = excluded.listings;
""",
    # announcement：仅当内容确实变化时才刷新 updated_at
    "announcement": """
INSERT INTO announcement (source_id, guid, title, link, published_at, summary, content_hash, first_seen_at, updated_at)
VALUES (?,?,?,?,?,?,?,?,?)
ON CONFLICT(source_id, guid) DO UPDATE SET
  title = excluded.title, summary = excluded.summary,
  content_hash = excluded.content_hash, updated_at = excluded.updated_at
WHERE announcement.content_hash IS NOT excluded.content_hash;
""",
    # fetch_log：合规审计留痕，只追加
    "fetch_log": """
INSERT INTO fetch_log (cycle_id, host, path, method, status_code, attempts, duration_ms, resp_bytes, error_class, robots_ok, ua, ts)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?);
""",
    # data_reject：数据质量隔离区，只追加
    "data_reject": """
INSERT INTO data_reject (cycle_id, source_id, item_id, rule, field, observed, expected, severity, payload, created_at)
VALUES (?,?,?,?,?,?,?,?,?,?);
""",
    "alert_event": """
INSERT INTO alert_event
  (alert_id, dedupe_key, rule, source_id, item_id, severity, metric, prev_value, curr_value, delta_pct, message, fired_at, cooldown_until, acknowledged)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(alert_id) DO NOTHING;
""",
    "source_state": """
INSERT INTO source_state (source_id, last_cursor, last_cycle_id, last_run_at, last_success_at, last_error_class, consecutive_failures, robots_checked_at, robots_allowed, updated_at)
VALUES (?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(source_id) DO UPDATE SET
  last_cursor = excluded.last_cursor,
  last_cycle_id = excluded.last_cycle_id,
  last_run_at = excluded.last_run_at,
  last_success_at = excluded.last_success_at,
  last_error_class = excluded.last_error_class,
  consecutive_failures = excluded.consecutive_failures,
  robots_checked_at = excluded.robots_checked_at,
  robots_allowed = excluded.robots_allowed,
  updated_at = excluded.updated_at;
""",
}


class SqliteWriter:
    """实现 `domain.ports.SnapshotSink`。**唯一持有写连接的组件**。"""

    __slots__ = (
        "_closed",
        "_conn",
        "_dropped_rows",
        "_max_batch",
        "_path",
        "_q",
        "_queue_maxsize",
        "_started",
        "_thread",
        "_written_rows",
    )

    def __init__(
        self,
        db_path: Path | str,
        *,
        queue_maxsize: int = QUEUE_MAXSIZE,
        max_batch: int = MAX_BATCH_DEFAULT,
    ) -> None:
        self._path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None
        self._queue_maxsize = max(1, int(queue_maxsize))
        self._q: queue.Queue = queue.Queue(maxsize=self._queue_maxsize)
        self._max_batch = max(1, int(max_batch))
        self._started = False
        self._closed = False
        self._written_rows = 0
        self._dropped_rows = 0

    # ---- 生命周期 ----
    def start(self) -> None:
        """建写连接 + 起写线程。**不需要**在事件循环内调用（写线程与 loop 解耦）。"""
        if self._started:
            return
        self._conn = connect(self._path)
        apply_migrations(self._conn)
        self._thread = threading.Thread(target=self._run, name="mw-sqlite-writer", daemon=True)
        self._thread.start()
        self._started = True
        _log.info("writer.started path=%s queue_maxsize=%d", self._path, self._queue_maxsize)

    async def start_async(self) -> None:
        self.start()

    def submit(self, batch: WriteBatch) -> None:
        """asyncio 侧调用（同步、非阻塞）。**队列满即丢弃并计数**（背压，§14 P1）。"""
        if not self._started:
            raise RuntimeError("SqliteWriter 未 start()")
        table, rows = batch
        if not rows:
            return
        try:
            self._q.put_nowait((table, list(rows)))
        except queue.Full:
            self._dropped_rows += len(rows)
            _log.error(
                "writer.queue_full table=%s rows=%d dropped_total=%d -> 背压生效，"
                "宁可丢一轮变更也不拖垮进程（§14 P1）",
                table,
                len(rows),
                self._dropped_rows,
            )

    async def flush(self, timeout_s: float = 10.0) -> None:
        """等队列排空。用 `to_thread` 调 `join()`，避免阻塞事件循环。"""
        import asyncio

        if not self._started:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(self._q.join), timeout=max(0.0, timeout_s))
        except TimeoutError:
            _log.error("writer.flush_timeout remaining=%d", self._q.qsize())

    async def drain(self, timeout_s: float = 30.0) -> bool:
        """`lifecycle.run_until_stopped` 的 drain 钩子：返回 True 表示写完。"""
        await self.flush(timeout_s)
        return self.depth == 0

    async def aclose(self) -> None:
        """停止写线程并关连接（退出序列第 5 步）。"""
        import asyncio

        if self._closed:
            return
        await self.flush(timeout_s=10.0)
        self._q.put(_STOP)
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 10.0)
        if self._conn is not None:
            pragma_optimize(self._conn)
            wal_checkpoint_truncate(self._conn)
            self._conn.close()
            self._conn = None
        self._closed = True
        _log.info(
            "writer.closed written_rows=%d dropped_rows=%d", self._written_rows, self._dropped_rows
        )

    @property
    def depth(self) -> int:
        return self._q.qsize()

    @property
    def stats(self) -> dict[str, int]:
        return {
            "depth": self.depth,
            "written_rows": self._written_rows,
            "dropped_rows": self._dropped_rows,
        }

    # ---- 写线程 ----
    def _run(self) -> None:
        conn = self._conn
        assert conn is not None
        while True:
            first = self._q.get()
            if first is _STOP:
                self._q.task_done()
                break
            pending: list[WriteBatch] = [first]
            total_rows = len(first[1])
            while total_rows < self._max_batch:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if item is _STOP:
                    self._q.task_done()
                    self._write(conn, pending)
                    return
                pending.append(item)
                total_rows += len(item[1])
            try:
                self._write(conn, pending)
            finally:
                for _ in pending:
                    self._q.task_done()

    def _write(self, conn: sqlite3.Connection, batches: Sequence[WriteBatch]) -> None:
        t0 = time.monotonic()
        rows_total = sum(len(r) for _, r in batches)
        try:
            with conn:  # 显式事务：一批一次提交
                for table, rows in batches:
                    sql = UPSERTS.get(table)
                    if sql is None:
                        _log.error(
                            "writer.unknown_table table=%s rows=%d -> 跳过", table, len(rows)
                        )
                        continue
                    for i in range(0, len(rows), EXECUTEMANY_CHUNK):
                        chunk = rows[i : i + EXECUTEMANY_CHUNK]
                        conn.executemany(sql, chunk)
                        self._written_rows += len(chunk)
        except sqlite3.Error as exc:
            _log.error("writer.write_failed err=%s rows_lost≈%d", exc, rows_total)
            return
        _log.debug(
            "writer.committed batches=%d rows=%d elapsed_ms=%.1f",
            len(batches),
            rows_total,
            (time.monotonic() - t0) * 1000.0,
        )
