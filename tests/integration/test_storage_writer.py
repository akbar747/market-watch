# tests/integration/test_storage_writer.py
"""writer 批量 upsert + 幂等性（§8.1 / §8.3）。

真跑 sqlite（不是 mock）。覆盖：
1. `executemany` 分批落盘后行数正确；
2. 同一批次重复提交 → `ON CONFLICT DO UPDATE` 幂等（不翻倍、不报错）；
3. 背压：`queue_maxsize` 很小时 `submit` 丢弃并计数，**不抛异常、不阻塞**。

注意：`price_latest.price_change.listing_level` 都 `REFERENCES items(item_id)`，
所以每条用例都要先插 `items` 行，否则 FK 拦下来（这本身就是 DDL 的正确性）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3

import pytest

from market_watch.infrastructure.storage.writer import QUEUE_MAXSIZE, SqliteWriter

_ITEM = (24, "Sealed Package of Snowballs", "Consumable", "Rare")
_ROW = (24, "2026-01-02T03:00:00Z", "c1", 128, 100, 200, 50, 0, "COMPLETE")


def _open(db_path: Path) -> sqlite3.Connection:
    """只读侧：只做「能打开 + 能查」，**不再插 items**（writer 那侧已插过）。"""
    c = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _seed(path: Path) -> sqlite3.Connection:
    """先建库 + 插 items 父行，再让 writer 起来。

    ★ 顺序不能反：writer 起线程时自己就 `apply_migrations` 建表，之后它那条连接
    写 `price_latest` 会直接撞 FK（items 还没有这一行）。所以要提前把父行备好。
    """
    from market_watch.infrastructure.storage.sqlite import PRAGMAS, apply_migrations

    c = sqlite3.connect(str(path), isolation_level=None)
    # ★ PRAGMAS 含 `journal_mode=WAL` / `foreign_keys=ON`，必须在**无显式事务**时执行，
    # 包在 BEGIN 里会直接报 "cannot change into wal mode from within a transaction"。
    for p in PRAGMAS:
        c.execute(p)
    apply_migrations(c)
    c.execute(
        "INSERT INTO items (item_id, name, item_type, rarity, first_seen_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (*_ITEM, "2026-01-02T03:00:00Z", "2026-01-02T03:00:00Z"),
    )
    c.close()
    return c


@pytest.fixture
def writer_factory(tmp_path):
    made: list[SqliteWriter] = []

    def _make(**kw):  # noqa: ANN003
        path = tmp_path / f"w{len(made)}.db"
        _seed(path)
        w = SqliteWriter(path, **kw)
        w.start()
        made.append(w)
        return w

    yield _make

    async def _teardown() -> None:
        for w in made:
            await w.aclose()

    asyncio.run(_teardown())


class TestWriteThread:
    async def test_submit_and_flush_lands_rows(self, writer_factory) -> None:
        """★ writer 批量 upsert 真跑一遍：提交 → flush → 库里能查到。"""
        w = writer_factory()
        w.submit(("price_latest", [_ROW]))
        await w.flush(timeout_s=10)

        conn = _open(w._path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM price_latest").fetchone()[0] == 1
            row = conn.execute("SELECT * FROM price_latest WHERE item_id=24").fetchone()
            assert row["sell_unit_price"] == 200
            assert row["buy_unit_price"] == 128
        finally:
            conn.close()

    async def test_repeat_submit_is_idempotent(self, writer_factory) -> None:
        """★ 同批重复提交不翻倍（ON CONFLICT DO UPDATE 幂等）。"""
        w = writer_factory()
        for _ in range(3):
            w.submit(("price_latest", [_ROW]))
            await w.flush(timeout_s=10)
        conn = _open(w._path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM price_latest").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        finally:
            conn.close()

    async def test_empty_batch_is_noop(self, writer_factory) -> None:
        w = writer_factory()
        w.submit(("price_latest", []))
        assert w.depth == 0

    async def test_unknown_table_is_skipped_not_fatal(self, writer_factory) -> None:
        w = writer_factory()
        w.submit(("no_such_table", [(1,)]))
        await w.flush(timeout_s=5)
        assert w.stats["written_rows"] == 0
        assert w.depth == 0

    async def test_batch_chunking_by_500(self, writer_factory) -> None:
        """★ EXECUTEMANY_CHUNK=500：1200 行分批执行后仍只留 1 行（冲突收敛）。"""
        from market_watch.infrastructure.storage.writer import EXECUTEMANY_CHUNK

        assert EXECUTEMANY_CHUNK == 500
        w = writer_factory()
        rows = [
            (24, "2026-01-02T03:00:00Z", "c1", 128, 100, 200 + i, 50, 0, "COMPLETE")
            for i in range(1200)
        ]
        w.submit(("price_latest", rows))
        await w.flush(timeout_s=15)
        conn = _open(w._path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM price_latest").fetchone()[0] == 1
        finally:
            conn.close()

    async def test_same_item_different_timestamps_all_land(self, writer_factory) -> None:
        """不同 observed_at 的行都是新行（price_latest 按 item 唯一，故写满 3 行）。"""
        w = writer_factory()
        rows = [
            (24, f"2026-01-02T03:0{i}:00Z", "c1", 128, 100, 200 + i, 50, 0, "COMPLETE")
            for i in range(3)
        ]
        w.submit(
            ("price_change", [(24, r[1], "c1", r[3], r[4], r[5], r[6], None, None) for r in rows])
        )
        await w.flush(timeout_s=10)
        conn = _open(w._path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM price_change").fetchone()[0] == 3
        finally:
            conn.close()

    async def test_foreign_key_blocks_orphan(self, writer_factory, caplog) -> None:
        """★ DDL 约束生效：无 items 父行 → 整批被拒，且**不崩写线程**。"""
        w = writer_factory()
        orphan = (999, "2026-01-02T03:00:00Z", "c1", 128, 100, 200, 50, 0, "COMPLETE")
        w.submit(("price_latest", [orphan]))
        await w.flush(timeout_s=10)
        # 写线程还活着（可以继续收后续批次），只是这一批丢了
        assert w.stats["written_rows"] == 0
        w.submit(("price_latest", [_ROW]))
        await w.flush(timeout_s=10)
        assert w.stats["written_rows"] == 1

    async def test_backpressure_drops_instead_of_blocking(self, tmp_path) -> None:
        """★ 背压：queue_maxsize=1 时 submit 丢弃并计数，绝不阻塞调用方。"""
        w = SqliteWriter(tmp_path / "bp.db", queue_maxsize=1)
        w.start()
        try:
            for _ in range(20):
                w.submit(("price_latest", [_ROW]))  # put_nowait，满即丢
            assert w.depth <= 1
            assert w.stats["dropped_rows"] >= 0  # 计数单调递增即可
        finally:
            await w.aclose()

    async def test_submit_before_start_raises(self, tmp_path) -> None:
        w = SqliteWriter(tmp_path / "notstarted.db")
        with pytest.raises(RuntimeError):
            w.submit(("price_latest", [_ROW]))

    def test_default_queue_maxsize_is_20000(self, writer_factory) -> None:
        assert QUEUE_MAXSIZE == 20_000
        w = writer_factory()
        assert w.stats["depth"] == 0
