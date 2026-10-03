# tests/unit/test_migration_probe.py
"""钉住 `apply_migrations` 的落地探针（commit 7765566）。

为什么要有这个文件：`meta.schema_version` 只记录「我以为我跑过」，**不等于**库里
真有那批对象。空壳库（`meta` 登记了 0001、业务表却被删空/被截页/半迁移态残留）会走
「版本已登记 → 跳过」这条短路，下一次 `gc` / `once` 一碰表就 `OperationalError:
no such table`，且崩在没有兜底的地方。

自愈靠的是「版本命中后再验一次落地探针」。这条语义很容易在下一次重构里被悄悄改回
「看 meta 就跳过」——没有测试的话，248 条全绿也不会有人发现。三条用例：

1. 健康库幂等：再跑 `applied == []`（探针不能误伤正常库）；
2. 空壳库自愈：不抛，`applied == ['0001']`，表被补回来；
3. 探针身份：取的是 0001 建的第一张**非 meta** 表（→ `source_state`）而非 `meta`。

只覆盖这三条。空库/零字节那两种走的是「版本未登记」分支、早被上一次修复挡住了，
再塞进来只是把历史重放一遍，故不加。
"""

from __future__ import annotations

from pathlib import Path

from market_watch.infrastructure.storage.sqlite import apply_migrations, connect

#: `0001_init.sql` 里建的第一张**非 meta** 表，`_migration_probe` 取的就是它。
_PROBE_TABLE = "source_state"


def _table_names(db_path: Path) -> list[str]:
    """库里的表名清单（用生产同一套 `connect`，避免口径不一致）。"""
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return [row["name"] for row in rows]


def _shell_db(db_path: Path) -> None:
    """空壳库：`meta` 已登记 '0001'，业务表一张都没有。"""
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '0001')")
        conn.commit()
    finally:
        conn.close()


def test_healthy_db_apply_is_idempotent(tmp_path: Path) -> None:
    """健康库：首跑 `['0001']`、再跑 `[]`——探针绝不能把正常库当成未迁移。"""
    conn = connect(tmp_path / "healthy.db")
    try:
        assert apply_migrations(conn) == ["0001"]
        assert apply_migrations(conn) == []
    finally:
        conn.close()


def test_shell_db_self_heals(tmp_path: Path) -> None:
    """空壳库：不抛，重跑 DDL 把表补回来（cmd_gc 路径不再崩）。"""
    db = tmp_path / "shell.db"
    _shell_db(db)
    assert _table_names(db) == ["meta"]

    conn = connect(db)
    try:
        assert apply_migrations(conn) == ["0001"]
    finally:
        conn.close()

    names = _table_names(db)
    assert _PROBE_TABLE in names
    assert "items" in names  # 全量 DDL 都补回来了，不是只补探针那一张


def test_probe_is_first_non_meta_table(tmp_path: Path) -> None:
    """探针必须是 `source_state` 而不是 `meta`。

    `meta` 由 `apply_migrations` 自己第一步就建好，**恒存在**；拿它当探针，版本命中后
    永远为真 → 空壳库被当成「已迁移」直接跳过 → 自愈逻辑一次都触发不了（7765566 里
    真踩过这个坑）。所以这里钉死：自愈前探针表不在（而 `meta` 在），自愈后探针表在。
    """
    db = tmp_path / "probe.db"
    _shell_db(db)

    before = _table_names(db)
    assert "meta" in before  # meta 自始至终都在 …
    assert _PROBE_TABLE not in before  # … 探针表却不在；谁把探针换回 meta，这条立刻红

    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()

    assert _PROBE_TABLE in _table_names(db)
