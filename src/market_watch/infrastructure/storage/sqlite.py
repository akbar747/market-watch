# src/market_watch/infrastructure/storage/sqlite.py
"""连接工厂 + PRAGMA + 迁移执行 + 只读克隆。

架构依据：docs/01-architecture.md §8.1。

单写者瓶颈的解法：**一个专用写线程 + `asyncio.Queue`**（见 `writer.py`）。
看板读侧另开**只读连接**（`file:...?mode=ro` URI）。
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Final

from ...logging_setup import get_logger

__all__ = [
    "PRAGMAS",
    "SCHEMA_VERSION",
    "apply_migrations",
    "connect",
    "connect_readonly",
    "pragma_optimize",
    "table_exists",
    "wal_checkpoint_truncate",
]

_log = get_logger("market_watch.sqlite")

#: §8.1 PRAGMA 全套
PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode=WAL",  # 读写不互斥，看板读不被写阻塞
    "PRAGMA synchronous=NORMAL",  # WAL 下的性价比最优
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",  # 单写者 + 5s 等待
    "PRAGMA wal_autocheckpoint=1024",  # 每 1024 页（~4MB）自动 checkpoint
    "PRAGMA journal_size_limit=67108864",  # WAL 文件上限 64MB
    "PRAGMA temp_store=MEMORY",
    "PRAGMA mmap_size=268435456",  # 256MB 只读侧 mmap
    "PRAGMA cache_size=-65536",  # 64MB page cache
)

SCHEMA_VERSION: Final[str] = "0001"

_MIGRATIONS_DIR: Final[Path] = Path(__file__).resolve().parent / "migrations"


def connect(db_path: Path | str) -> sqlite3.Connection:
    """读写连接（**只允许写线程持有**）。"""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn


def connect_readonly(db_path: Path | str) -> sqlite3.Connection:
    """只读连接（看板读侧）。`mode=ro` 保证读侧不可能写。"""
    path = Path(db_path)
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(
        uri, uri=True, timeout=5.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    for pragma in (
        "PRAGMA query_only=ON",
        "PRAGMA mmap_size=268435456",
        "PRAGMA cache_size=-65536",
        "PRAGMA busy_timeout=5000",
    ):
        try:
            conn.execute(pragma)
        except sqlite3.DatabaseError as exc:  # pragma 在旧版 sqlite 上可能不支持
            _log.warning("pragma.skipped %s err=%s", pragma, exc)
    return conn


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """按文件名递增顺序执行 `000N_*.sql`。返回已应用的版本列表。

    幂等：DDL 全部用 `IF NOT EXISTS`；用 `meta.schema_version` 记录当前版本。
    已应用过的迁移**不会**重复执行（除非版本号未登记）。
    """
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    applied: list[str] = []
    for sql_file in sorted(_MIGRATIONS_DIR.glob("0*.sql")):
        version = sql_file.name.split("_", 1)[0]
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is not None and row["value"] >= version:
            continue
        ddl = sql_file.read_text(encoding="utf-8")
        with conn:
            conn.executescript(ddl)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (version,),
            )
        applied.append(version)
        _log.info("migration.applied version=%s file=%s", version, sql_file.name)
    return applied


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def pragma_optimize(conn: sqlite3.Connection) -> None:
    """§8.4 步骤 3：整理。退出序列与 GC 都会调。"""
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.DatabaseError as exc:
        _log.warning("pragma.optimize_failed err=%s", exc)


def wal_checkpoint_truncate(conn: sqlite3.Connection) -> None:
    """Windows 优雅退出前调用（§14 P1）：避免 WAL 残留导致下次启动慢。"""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError as exc:
        _log.warning("wal_checkpoint.failed err=%s", exc)
