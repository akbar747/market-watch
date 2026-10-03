# src/market_watch/infrastructure/storage/repositories.py
"""读侧：看板用的聚合查询（降采样 / 分页 / 搜索 / 健康）。

架构依据：docs/01-architecture.md §13（签名清单）、§10.1（看板首屏 ≤300ms）。

全部走**只读连接**（`mode=ro`），降采样在**服务端**完成（≤2000 点），
浏览器端只负责画。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from typing import Final

from ...domain.models import AlertEvent, Announcement, HealthReport, iso_z, utc_now
from ...logging_setup import get_logger
from .sqlite import connect_readonly

__all__ = ["DOWNSAMPLE_LIMIT", "Repository"]

_log = get_logger("market_watch.repositories")

#: §10.1：服务端降采样上限
DOWNSAMPLE_LIMIT: Final[int] = 2_000


#: 「库已建表」的判据：items 表存在即视为库已就绪（其余表都是它的伴生表）
_READY_TABLE: Final[str] = "items"


class Repository:
    """只读查询。构造时开一条只读连接；`aclose()` 关闭。

    ★ 必须容忍「库还没建」的状态（全新安装、从没跑过 `once`/`gc`）：
      `connect_readonly` 用的是 `mode=ro`，而 **SQLite 对「文件不存在」直接
      OperationalError: unable to open database file**（对「零字节文件」才当空库，
      能打开）。所以 2026-10-03 发布冒烟实测里，exe 装好第一次跑 `status` 就
      traceback 崩在 `Repository.__init__`。这里补一个零字节占位文件，并把
      「还没建表」归一成空结果，读侧因此**不可能**因「没数据」抛异常。
    """

    __slots__ = ("_conn", "_ready")

    def __init__(self, db_path: Path | str) -> None:
        path = Path(db_path)
        if not path.exists():
            if path.parent and not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()  # 零字节占位：让 mode=ro 也能打开（SQLite 视其为「空库」）
        self._conn: sqlite3.Connection = connect_readonly(path)
        self._ready: bool = self._probe_ready()

    def _probe_ready(self) -> bool:
        """库是否已完成建表。零字节/空库返回 False，读侧据此返回空而不是报错。"""
        try:
            row = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_READY_TABLE,)
            ).fetchone()
        except sqlite3.Error:  # pragma: no cover - 极端情况（库损坏等），按未就绪处理
            return False
        return row is not None

    def _query(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        """读侧统一出口：库未就绪时返回空列表，而不是抛 `no such table`。"""
        if not self._ready:
            return []
        return list(self._conn.execute(sql, tuple(params)).fetchall())

    def aclose(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error as exc:  # pragma: no cover
            _log.warning("repo.close_failed err=%s", exc)

    # ---- §13 签名 ----
    def latest_snapshot(
        self, item_ids: Sequence[int] | None = None, limit: int = 100
    ) -> list[sqlite3.Row]:
        """`price_latest` JOIN `items`，带名称。"""
        sql = """
            SELECT p.item_id, i.name, p.observed_at, p.cycle_id,
                   p.buy_unit_price, p.buy_quantity, p.sell_unit_price, p.sell_quantity,
                   p.whitelisted, p.quality
              FROM price_latest p LEFT JOIN items i ON i.item_id = p.item_id
        """
        params: list[object] = []
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            sql += f" WHERE p.item_id IN ({placeholders})"
            params.extend(int(i) for i in item_ids)
        sql += " ORDER BY p.sell_unit_price DESC NULLS LAST LIMIT ?"
        params.append(int(limit))
        return self._query(sql, params)

    def series(
        self,
        item_id: int,
        table: str = "price_change",
        since: datetime | None = None,
        limit: int = DOWNSAMPLE_LIMIT,
    ) -> list[sqlite3.Row]:
        """时间序列。**服务端降采样到 ≤2000 点**（§10.1）。

        降采样策略：先用 SQL 取总数，若超 `limit` 则按 `ROW_NUMBER % step == 0`
        均匀取样（保留首尾）。
        """
        if table not in ("price_change", "price_hourly"):
            raise ValueError(f"不支持的序列表: {table!r}")
        time_col = "observed_at" if table == "price_change" else "bucket_ts"
        since_iso = iso_z(since) if since else iso_z(utc_now() - timedelta(days=1))

        counts = self._query(
            f"SELECT COUNT(*) AS c FROM {table} WHERE item_id = ? AND {time_col} >= ?",
            (int(item_id), since_iso),
        )
        total = int(counts[0]["c"]) if counts else 0
        if total == 0:
            return []

        if total <= limit:
            return self._query(
                f"SELECT {time_col} AS ts, buy_unit_price AS buy, sell_unit_price AS sell "
                f"FROM {table} WHERE item_id = ? AND {time_col} >= ? ORDER BY {time_col} ASC",
                (int(item_id), since_iso),
            )

        step = max(1, total // limit)
        # SQLite 3.25+ 支持窗口函数；用 ROW_NUMBER 均匀取样
        sql = f"""
            SELECT ts, buy, sell FROM (
                SELECT {time_col} AS ts, buy_unit_price AS buy, sell_unit_price AS sell,
                       ROW_NUMBER() OVER (ORDER BY {time_col} ASC) AS rn
                  FROM {table}
                 WHERE item_id = ? AND {time_col} >= ?
            ) WHERE rn % ? = 1 OR rn = ?
              ORDER BY ts ASC
        """
        return self._query(sql, (int(item_id), since_iso, step, total))

    def alerts(self, limit: int = 50, unack_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM alert_event"
        if unack_only:
            sql += " WHERE acknowledged = 0"
        sql += " ORDER BY fired_at DESC LIMIT ?"
        return self._query(sql, (int(limit),))

    def announcements(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM announcement ORDER BY published_at DESC LIMIT ?", (int(limit),)
        )

    def search_items(self, q: str, limit: int = 20) -> list[sqlite3.Row]:
        like = f"%{q}%"
        return self._query(
            "SELECT item_id, name, item_type, rarity FROM items "
            "WHERE name LIKE ? ORDER BY name LIMIT ?",
            (like, int(limit)),
        )

    def health(self, cursor_limit: int = 100) -> HealthReport:
        """给看板「健康」卡片 + `/api/health`。"""
        since_24h = iso_z(utc_now() - timedelta(hours=24))

        def _first_int(sql: str, params: Sequence[object] = ()) -> int:
            rows = self._query(sql, params)
            return int(rows[0]["c"]) if rows else 0

        items_total = _first_int("SELECT COUNT(*) AS c FROM price_latest")
        alerts_24h = _first_int(
            "SELECT COUNT(*) AS c FROM alert_event WHERE fired_at >= ?", (since_24h,)
        )
        rejects_24h = _first_int(
            "SELECT COUNT(*) AS c FROM data_reject WHERE created_at >= ?", (since_24h,)
        )
        cycle_rows = self._query(
            "SELECT cycle_id FROM price_latest ORDER BY observed_at DESC LIMIT 1"
        )
        last_cycle = cycle_rows[0]["cycle_id"] if cycle_rows else None
        # 留作审计展示，暂不进 HealthReport
        _ = _first_int("SELECT COUNT(*) AS c FROM fetch_log")
        return HealthReport(
            db_size_mb=self.db_size_mb(),
            last_cycle=last_cycle,
            items_total=items_total,
            alerts_24h=alerts_24h,
            rejects_24h=rejects_24h,
        )

    def db_size_mb(self) -> float:
        try:
            row = self._conn.execute("PRAGMA page_count").fetchone()
            size = self._conn.execute("PRAGMA page_size").fetchone()
            pages = int(row[0]) if row else 0
            page_size = int(size[0]) if size else 0
            return round(pages * page_size / (1024 * 1024), 2)
        except sqlite3.Error:  # pragma: no cover
            return 0.0

    # ---- 补充：看板首页 summary ----
    def summary(self) -> dict[str, object]:
        since_24h = iso_z(utc_now() - timedelta(hours=24))
        items_total = self._scalar("SELECT COUNT(*) FROM price_latest")
        alerts_24h = self._scalar(
            "SELECT COUNT(*) FROM alert_event WHERE fired_at >= ?", (since_24h,)
        )
        ann_24h = self._scalar(
            "SELECT COUNT(*) FROM announcement WHERE published_at >= ?", (since_24h,)
        )
        partial = self._scalar("SELECT COUNT(*) FROM price_latest WHERE quality = 'PARTIAL'")
        cycle_rows = self._query("SELECT MAX(observed_at) AS ts FROM price_latest")
        cycle = cycle_rows[0] if cycle_rows else None
        return {
            "cycle_ts": cycle["ts"] if cycle and cycle["ts"] else None,
            "items_total": items_total,
            "alerts_24h": alerts_24h,
            "ann_24h": ann_24h,
            "quality_partial": partial,
            "db_size_mb": self.db_size_mb(),
        }

    def quality(self) -> dict[str, object]:
        """`/api/quality`：数据质量卡片（§11.1-3）。"""
        since_24h = iso_z(utc_now() - timedelta(hours=24))
        rules = self._query(
            "SELECT rule, COUNT(*) AS hits FROM data_reject WHERE created_at >= ? "
            "GROUP BY rule ORDER BY hits DESC",
            (since_24h,),
        )
        return {
            "rejects_24h": sum(int(r["hits"]) for r in rules),
            "rules": [{"rule": r["rule"], "hits": int(r["hits"])} for r in rules],
            "partial_cycles": self._scalar(
                "SELECT COUNT(*) FROM price_latest WHERE quality='PARTIAL'"
            ),
        }

    def recent_fetch_log(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._query(
            "SELECT host, path, method, status_code, attempts, duration_ms, error_class, ts "
            "FROM fetch_log ORDER BY ts DESC LIMIT ?",
            (int(limit),),
        )

    def source_states(self) -> list[sqlite3.Row]:
        """各数据源最近一次运行状态（给 `status` 与看板）。未就绪时返回空列表。"""
        return self._query("SELECT * FROM source_state ORDER BY updated_at DESC")

    def _scalar(self, sql: str, params: Sequence[object] = ()) -> int:
        if not self._ready:
            return 0
        row = self._conn.execute(sql, tuple(params)).fetchone()
        return int(row[0]) if row else 0


def row_to_announcement(row: sqlite3.Row) -> Announcement:

    def _dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    return Announcement(
        source_id=row["source_id"],
        guid=row["guid"],
        title=row["title"],
        link=row["link"],
        published_at=_dt(row["published_at"]),
        summary=row["summary"],
        content_hash=row["content_hash"],
        fetched_at=_dt(row["first_seen_at"]) or utc_now(),
    )


def row_to_alert(row: sqlite3.Row) -> AlertEvent:

    def _dt(value: str) -> datetime:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    return AlertEvent(
        alert_id=row["alert_id"],
        dedupe_key=row["dedupe_key"],
        rule=row["rule"],
        source_id=row["source_id"],
        item_id=row["item_id"],
        severity=row["severity"],
        metric=row["metric"],
        prev_value=row["prev_value"],
        curr_value=row["curr_value"],
        delta_pct=row["delta_pct"],
        message=row["message"],
        fired_at=_dt(row["fired_at"]),
        cooldown_until=_dt(row["cooldown_until"]),
        acknowledged=bool(row["acknowledged"]),
    )
