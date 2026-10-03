# tests/unit/test_repository_empty_db.py
"""`Repository` 在「库还没建表」时必须返回空，而不是抛 traceback。

为什么这两个测试值得存在
------------------------
`connect_readonly` 用 `mode=ro`，而 **SQLite 对「文件不存在」直接
`OperationalError: unable to open database file`**（对「零字节文件」才当空库、能打开）。
于是「装好 exe 第一次跑 `market-watch status`」这条用户路径会直接崩在
`Repository.__init__`（2026-10-03 发布冒烟真机实测复现）。

这条路径在源码测试里**本来是测不到的**：仓库里的测试要么把库建好再查，要么根本
不构造 `Repository`（该模块此前覆盖率 0%）。只有把「全新安装」当成一种真实状态，
才能保证用户第一眼看到的是「还没有数据」而不是一屏 traceback。

全离线：只在 tmp_path 里造库，不产生任何网络请求（conftest 的断网闸门照样生效）。
"""

from __future__ import annotations

import pytest

from market_watch.infrastructure.storage.repositories import Repository
from market_watch.infrastructure.storage.sqlite import apply_migrations, connect

#: 与 `migrations/0001_init.sql` 的 CREATE TABLE 一一对应（加表要同步改这里，
#: 否则上面那条「迁移覆盖全部表」的断言会失真）
TABLES = (
    "alert_event",
    "announcement",
    "data_reject",
    "fetch_log",
    "items",
    "listing_level",
    "meta",
    "price_change",
    "price_hourly",
    "price_latest",
    "source_state",
    "watchlist",
)


def _init_db(path) -> None:
    """按生产同一条路径建库 + 建表（不写业务数据）。"""
    conn = connect(path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------- 未初始化
def test_repository_tolerates_missing_db_file(tmp_path):
    """文件连父目录都不存在时，构造成功并自动补零字节占位。"""
    db = tmp_path / "nested" / "fresh.db"  # 三层目录都还不存在
    repo = Repository(db)
    try:
        assert db.exists(), "应补出零字节占位文件，否则 mode=ro 打不开"
        assert db.stat().st_size == 0, "占位必须是零字节（有内容就不是「未初始化」了）"
    finally:
        repo.aclose()


def test_repository_empty_db_returns_empty_not_raises(tmp_path):
    """未初始化库上所有读接口都返回空，且不抛任何异常。"""
    repo = Repository(tmp_path / "fresh.db")
    try:
        summary = repo.summary()
        assert summary["items_total"] == 0
        assert summary["cycle_ts"] is None
        assert summary["db_size_mb"] == 0.0

        quality = repo.quality()
        assert quality["rules"] == []
        assert quality["rejects_24h"] == 0

        health = repo.health()
        assert health.items_total == 0
        assert health.last_cycle is None

        assert repo.series(1) == []
        assert repo.series(1, table="price_hourly") == []
        assert repo.alerts() == []
        assert repo.announcements() == []
        assert repo.recent_fetch_log() == []
        assert repo.search_items("x") == []
        assert repo.latest_snapshot() == []
        # `source_state` 是唯一被 cmd_status 直接读的表，单独兜一层
        assert repo.source_states() == []
    finally:
        repo.aclose()


def test_repository_empty_db_returns_empty_when_db_exists_but_empty(tmp_path):
    """库文件已存在但还没跑过迁移（零字节）：行为应与「文件不存在」一致。"""
    db = tmp_path / "empty.db"
    db.touch()
    repo = Repository(db)
    try:
        assert repo.summary()["items_total"] == 0
        assert repo.source_states() == []
    finally:
        repo.aclose()


# ---------------------------------------------------------------- 已初始化
def test_repository_initialized_db_still_works(tmp_path):
    """兜底不能把「有数据」也兜成空——建表后必须读到真实计数。"""
    db = tmp_path / "ready.db"
    _init_db(db)
    repo = Repository(db)
    try:
        assert repo.summary()["items_total"] == 0, "刚建表、还没抓数据，计数是 0"
        assert repo.source_states() == [], "建表动作本身不该写 source_state 行"
        assert repo.db_size_mb() >= 0.0
        # 建表后表结构必须在，否则上面的查询会撞 no such table
        rows = repo._query(
            # 滤掉 sqlite_sequence 这类 sqlite_ 前缀的内部表（AUTOINCREMENT 产物，
            # 不是迁移建的），否则这张名单永远对不上
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        assert [r["name"] for r in rows] == sorted(TABLES), "迁移未覆盖全部表，用例会失真"
    finally:
        repo.aclose()


@pytest.mark.parametrize("bad_table", ("price_daily", "", "ITEMS", "items; DROP TABLE items"))
def test_series_rejects_unknown_table(tmp_path, bad_table):
    """`series` 的入参校验不能被兜底逻辑顺手吃掉。

    ★ 这里必须用**非法**表名：拿 `price_change`/`price_hourly` 当反例是错的，
    那是合法值，只会走到查询分支去。`_query` 的「未就绪返回空」兜底只应作用在
    表是否存在上，不该把入参校验一起吞掉。
    """
    db = tmp_path / "ready.db"
    _init_db(db)
    repo = Repository(db)
    try:
        with pytest.raises(ValueError, match="不支持的序列表"):
            repo.series(1, table=bad_table)
    finally:
        repo.aclose()
