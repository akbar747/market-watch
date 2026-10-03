# tests/integration/test_storage_maintenance.py
"""§8.4 顺序铁律：**rollup → 打 rolled_up 标记 → 删 change → vacuum**。

★ 本文件验证 Phase 1 实跑发现的那个缺陷的修复是否成立：
  只有「rollup 后再删」而**没有** `rolled_up` 标记时，进程在两步之间崩溃后重跑，
  会把同一批 `price_change` 再滚一遍 → `price_hourly.samples` 翻倍。
  必须真跑 sqlite（不是 mock）。

用法约定：fixture 只建库 + 建表，**不插数据**，每条用例自己插 —— 否则
`price_change` 的 ux(item_id, observed_at) 会在二次播种时直接 UNIQUE 冲突。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from market_watch.infrastructure.storage.maintenance import Maintenance, run_gc

_TS = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
_BUCKET = "2026-03-15T09:00:00Z"


def _open(db_path: Path) -> sqlite3.Connection:
    from market_watch.infrastructure.storage.sqlite import PRAGMAS, apply_migrations

    c = sqlite3.connect(str(db_path), isolation_level=None)
    c.row_factory = sqlite3.Row
    for p in PRAGMAS:
        c.execute(p)
    apply_migrations(c)
    return c


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _seed_changes(conn: sqlite3.Connection, bucket: str, n: int = 5) -> None:
    """在某小时桶内插 n 条 price_change（顺带备好 items 父行）。

    ★ `observed_at` 必须逐条不同：`price_change` 的唯一键是 (item_id, observed_at)，
    5 条同桶数据要按分钟错开，否则第二条就撞 UNIQUE。
    """
    hour = bucket[:13]  # 'YYYY-MM-DDTHH'
    conn.execute(
        "INSERT INTO items (item_id, name, item_type, rarity, first_seen_at, updated_at) "
        "VALUES (24,'Test Item','Consumable','Rare',?,?)",
        (bucket, bucket),
    )
    for i in range(n):
        ts = f"{hour}:{i:02d}:00Z"
        conn.execute(
            "INSERT INTO price_change (item_id, observed_at, cycle_id, buy_unit_price, "
            "buy_quantity, sell_unit_price, sell_quantity, prev_sell_price, delta_pct) "
            "VALUES (24, ?, 'c1', 128, ?, 200, ?, NULL, NULL)",
            (ts, 100 + i, 50 + i),
        )


@pytest.fixture
def gc_db(tmp_path) -> Path:
    path = tmp_path / "gc.db"
    _open(path).close()  # 只为把库建出来（建表），不插数据
    return path


class TestRollupIdempotency:
    def test_gc_twice_does_not_double_samples(self, gc_db: Path) -> None:
        """★ 核心：GC 跑两遍，price_hourly.samples 必须保持不变。"""
        conn = _open(gc_db)
        _seed_changes(conn, _BUCKET, 5)
        conn.close()

        run_gc(gc_db, now=_TS)  # 第一遍

        conn = _open(gc_db)
        try:
            first = conn.execute(
                "SELECT samples, high_sell, low_sell, close_sell FROM price_hourly WHERE bucket_ts=?",
                (_BUCKET,),
            ).fetchone()
            marked = conn.execute("SELECT COUNT(*) FROM price_change WHERE rolled_up=1").fetchone()[
                0
            ]
        finally:
            conn.close()
        assert first is not None, "第一遍 GC 必须产出 price_hourly 行"
        assert marked == 5, "rollup 后 rolled_up 必须被标记"

        run_gc(gc_db, now=_TS)  # 第二遍（模拟崩后重跑）

        conn = _open(gc_db)
        try:
            second = conn.execute(
                "SELECT samples, high_sell, low_sell, close_sell FROM price_hourly WHERE bucket_ts=?",
                (_BUCKET,),
            ).fetchone()
        finally:
            conn.close()

        assert second["samples"] == first["samples"], "★ samples 翻倍 = 幂等修复失效"
        assert second["high_sell"] == first["high_sell"]
        assert second["low_sell"] == first["low_sell"]
        assert second["close_sell"] == first["close_sell"]

    def test_samples_equals_change_row_count(self, gc_db: Path) -> None:
        """5 条 change 滚一次 → samples 恰好 5（不多不少）。"""
        conn = _open(gc_db)
        _seed_changes(conn, _BUCKET, 5)
        conn.close()

        run_gc(gc_db, now=_TS)

        conn = _open(gc_db)
        try:
            row = conn.execute(
                "SELECT samples FROM price_hourly WHERE bucket_ts=?", (_BUCKET,)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and row["samples"] == 5

    def test_rollup_happens_before_delete(self, gc_db: Path) -> None:
        """★ 顺序铁律的直接证据：GC 后 change 仍在库（还没轮到删），hourly 已有数据。"""
        conn = _open(gc_db)
        _seed_changes(conn, _BUCKET, 5)
        conn.close()

        run_gc(gc_db, now=_TS)

        conn = _open(gc_db)
        try:
            changes = conn.execute("SELECT COUNT(*) FROM price_change").fetchone()[0]
            hourly = conn.execute("SELECT COUNT(*) FROM price_hourly").fetchone()[0]
            marked = conn.execute("SELECT COUNT(*) FROM price_change WHERE rolled_up=1").fetchone()[
                0
            ]
        finally:
            conn.close()
        assert hourly == 1
        assert changes == 5 and marked == 5

    def test_marked_and_rollup_are_atomic(self, gc_db: Path) -> None:
        """★ rolled_up=1 的行绝不能被第二次 rollup 再吃一次（这正是本修复的落点）。"""
        conn = _open(gc_db)
        _seed_changes(conn, _BUCKET, 5)
        conn.close()

        Maintenance(gc_db).gc(now=_TS)
        conn = _open(gc_db)
        conn.execute("SELECT samples FROM price_hourly WHERE bucket_ts=?", (_BUCKET,)).fetchone()
        conn.close()

        # 手动把整条 SQL 再跑一次（绕过标记最简单粗暴），确认 rolled_up=0 才是关键
        from market_watch.infrastructure.storage.maintenance import ROLLUP_SQL

        conn = _open(gc_db)
        try:
            before = conn.execute(
                "SELECT samples FROM price_hourly WHERE bucket_ts=?", (_BUCKET,)
            ).fetchone()["samples"]
            # 模拟「没有 rolled_up 标记」的世界：把本桶的行标回去（对照实验）
            conn.execute(
                "UPDATE price_change SET rolled_up = 0 "
                "WHERE observed_at >= ? AND observed_at < ?",
                (_BUCKET, "2026-03-15T10:00:00Z"),
            )
            conn.execute(
                ROLLUP_SQL,
                {"bucket": _BUCKET, "bucket_start": _BUCKET, "bucket_end": "2026-03-15T11:00:00Z"},
            )
            after = conn.execute(
                "SELECT samples FROM price_hourly WHERE bucket_ts=?", (_BUCKET,)
            ).fetchone()["samples"]
        finally:
            conn.close()
        assert after > before, "对照实验：回滚标记后重复 rollup 确实会累加（证明标记是必要修复）"

    def test_expired_change_rows_are_deleted(self, gc_db: Path) -> None:
        """72h 窗口外的 change 行必须被删（§8.4）。"""
        conn = _open(gc_db)
        _seed_changes(conn, _BUCKET, 5)
        conn.execute(
            "INSERT INTO price_change (item_id, observed_at, cycle_id, buy_unit_price, "
            "buy_quantity, sell_unit_price, sell_quantity, prev_sell_price, delta_pct) "
            "VALUES (24, ?, 'c1', 128, 100, 200, 50, NULL, NULL)",
            (_iso(_TS - timedelta(hours=100)),),
        )
        conn.close()

        stats = run_gc(gc_db, now=_TS)

        conn = _open(gc_db)
        try:
            left = conn.execute("SELECT COUNT(*) FROM price_change").fetchone()[0]
        finally:
            conn.close()
        assert stats["deleted"]["price_change"] >= 1
        assert left == 5  # 当前桶那 5 条保留，100h 前的被删

    def test_alert_event_needs_acknowledged(self, gc_db: Path) -> None:
        """§8.4：alert_event 只有 acknowledged=1 才允许删。"""
        old = _iso(_TS - timedelta(days=120))
        conn = _open(gc_db)
        for ack in (0, 1):
            conn.execute(
                "INSERT INTO alert_event (alert_id, dedupe_key, rule, source_id, item_id, "
                "severity, metric, message, fired_at, cooldown_until, acknowledged) "
                "VALUES (?, ?, 'r', 's', 24, 'warn', 'sell_unit_price', 'm', ?, ?, ?)",
                # dedupe_key 与 cooldown_until 组成唯一键，必须错开
                (f"a{ack}", f"dedupe{ack}", old, old, ack),
            )
        conn.close()

        run_gc(gc_db, now=_TS)

        conn = _open(gc_db)
        try:
            left = conn.execute(
                "SELECT COUNT(*) FROM alert_event WHERE fired_at < ?",
                (_iso(_TS - timedelta(days=90)),),
            ).fetchone()[0]
        finally:
            conn.close()
        assert left == 1  # 未确认的那条必须还在

    def test_price_hourly_keeps_watchlisted_item(self, gc_db: Path) -> None:
        """§8.4：price_hourly 只清不在观测清单里的 item。"""
        conn = _open(gc_db)
        _seed_changes(conn, _BUCKET, 5)  # 只备好 item 24 的 items 父行
        # 999 是「不在观测清单里」的对照组，仍须有 items 父行 --
        # price_hourly.item_id REFERENCES items(item_id)，不给父行就 ForeignKey 违约。
        # 注意不能图省事 PRAGMA foreign_keys=OFF 绕开：那会把生产等价性一起验没了。
        conn.execute(
            "INSERT INTO items (item_id, name, item_type, rarity, first_seen_at, updated_at) "
            "VALUES (999,'Ghost','Consumable','Rare',?,?)",
            (_BUCKET, _BUCKET),
        )
        conn.execute(
            "INSERT INTO watchlist (item_id, reason, added_at) VALUES (24, 'top', ?)", (_BUCKET,)
        )
        # ★ 999 这条必须同时满足「超 180d 保留期」+「不在 watchlist」才会被 GC 删：
        # maintenance.py 的清理 SQL 是 `bucket_ts < cutoff AND item_id NOT IN (watchlist)`，
        # 两个条件是 AND。若只把桶设成 2026-03-01（距 _TS 才 14 天，未超期），
        # 即便不在清单里也不会删——那才是新数据被误清。故用 2025-06-01 保证真的超期。
        conn.execute(
            "INSERT INTO price_hourly (item_id, bucket_ts, samples) VALUES (999, ?, 7)",
            ("2025-06-01T10:00:00Z",),
        )
        conn.execute(
            "INSERT INTO price_hourly (item_id, bucket_ts, samples) VALUES (24, ?, 3)",
            ("2026-03-15T09:00:00Z",),
        )
        conn.close()

        Maintenance(gc_db).gc(now=_TS)

        conn = _open(gc_db)
        try:
            watchlisted = conn.execute(
                "SELECT COUNT(*) FROM price_hourly WHERE item_id=24 AND bucket_ts=?",
                ("2026-03-15T09:00:00Z",),
            ).fetchone()[0]
            orphan = conn.execute("SELECT COUNT(*) FROM price_hourly WHERE item_id=999").fetchone()[
                0
            ]
        finally:
            conn.close()
        assert watchlisted == 1  # 观测清单内的小时桶不被 GC 删掉
        assert orphan == 0  # 不在清单里的（且超 180d）被清掉

    def test_run_gc_returns_stats_dict(self, gc_db: Path) -> None:
        conn = _open(gc_db)
        _seed_changes(conn, _BUCKET, 5)
        conn.close()
        stats = run_gc(gc_db, now=_TS)
        assert isinstance(stats, dict)
        assert "rolled_buckets" in stats and "deleted" in stats


class TestMaintenanceOnFreshInstall:
    """全新安装（库文件都还不存在）时维护通路不能崩。

    ★ 2026-10-03 实跑 `market-watch gc` 发现的缺陷：迁移只在
      `writer.py` 跑过，维护通路先于 writer 打开时会看到空库，
      直接 `sqlite3.OperationalError: no such table: price_hourly`。
      修复方式：`Maintenance.gc()` 里补一次幂等 `apply_migrations`。
    """

    def _no_such_db(self, tmp_path: Path) -> Path:
        return tmp_path / "never-created.db"  # 刻意不建库、不建表

    def test_gc_on_missing_db_bootstraps_schema(self, tmp_path: Path) -> None:
        db_path = self._no_such_db(tmp_path)
        assert not db_path.exists()

        stats = run_gc(db_path, now=_TS)  # 不得抛异常

        assert stats["rolled_buckets"] == 0
        conn = _open(db_path)
        try:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        finally:
            conn.close()
        assert "price_hourly" in tables, "gc 必须自举出 rollup 依赖的表"
        assert "price_change" in tables
        assert version is not None and version[0] == "0001"

    def test_gc_on_fresh_db_is_repeatable(self, tmp_path: Path) -> None:
        db_path = self._no_such_db(tmp_path)
        first = run_gc(db_path, now=_TS)
        second = run_gc(db_path, now=_TS)  # 第二遍不得重跑迁移、不得报错
        assert first["deleted"] == second["deleted"]
        assert second["rolled_buckets"] == 0

        conn = _open(db_path)
        try:
            applied = conn.execute(
                "SELECT COUNT(*) FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert applied == 1, "幂等迁移：schema_version 只能登记一次"
