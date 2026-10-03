# src/market_watch/application/news_ingest.py
"""公告用例：RSS 拉取 + 条件请求 + 增量去重。

架构依据：docs/01-architecture.md §5.4（`NEWS_INTERVAL_S=900`，304 不消耗配额）、§8.3。

增量去重：
- `guid` 是自然主键（优先 `<guid>`，缺失用 `sha1(link|title)`）。
- `content_hash = sha256(title|summary|link)[:32]`；upsert 的
  `WHERE announcement.content_hash IS NOT excluded.content_hash` 保证
  **仅内容确实变化才刷新 `updated_at`**（§8.3）。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..domain.models import Announcement, iso_z
from ..logging_setup import get_logger

__all__ = ["NewsIngest"]

_log = get_logger("market_watch.news_ingest")


class NewsIngest:
    """把一批 `Announcement` 变成写入批次。"""

    __slots__ = ("_seen", "_writer")

    def __init__(self, writer) -> None:  # noqa: ANN001 - SnapshotSink
        self._writer = writer
        self._seen: set[tuple[str, str]] = set()

    def ingest(self, items: Sequence[Announcement]) -> tuple[int, int]:
        """返回 `(提交行数, 本轮进程内新增 guid 数)`。"""
        if not items:
            return 0, 0
        now_iso = iso_z(_now())
        rows = []
        new_guids = 0
        for ann in items:
            key = (ann.source_id, ann.guid)
            if key not in self._seen:
                self._seen.add(key)
                new_guids += 1
            rows.append(
                (
                    ann.source_id,
                    ann.guid,
                    ann.title,
                    ann.link,
                    iso_z(ann.published_at) if ann.published_at else None,
                    ann.summary,
                    ann.content_hash,
                    iso_z(ann.fetched_at),
                    now_iso,
                )
            )
        self._writer.submit(("announcement", rows))
        _log.info("news_ingest rows=%d new_guids=%d", len(rows), new_guids)
        return len(rows), new_guids

    def known(self, source_id: str, guid: str) -> bool:
        return (source_id, guid) in self._seen

    def __len__(self) -> int:
        return len(self._seen)


def _now():  # noqa: ANN202
    from ..domain.models import utc_now

    return utc_now()
