# src/market_watch/infrastructure/adapters/rss_generic.py
"""通用 RSS/Atom 适配器，供官方公告源复用。

实测契约（2026-10-03）：
    GET https://www.guildwars2.com/en/feed/ -> 200, text/xml; charset=utf-8, 78.8 KB, RSS 2.0

条件请求：`If-None-Match` / `If-Modified-Since`（§5.4：304 不消耗配额）。
游标由 `cursor_from` 导出，下轮由调用方注入到 `FetchTask.headers`。

关于 `feedparser`：它是**阻塞库**，`NEWS_INTERVAL_S=900` 跑一次、单份 78KB，
成本可忽略——用 `asyncio.to_thread()` 隔离。但本 adapter 的 `parse` 必须是
**同步纯函数**（§7.3 规则 1），因此解析走标准库 `xml.etree.ElementTree`：
    - 支持 RSS 2.0（channel/item）与 Atom（feed/entry）两种形态；
    - 不引 feedparser 进 domain/adapter 的纯路径，保证契约测试零依赖可跑。
`feedparser` 仍作为可选依赖保留在 `pyproject.toml`（兜底解析更复杂 feed 时用）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import hashlib
from typing import Final
from xml.etree import ElementTree as ET

from ...domain.consistency import check_pii_field
from ...domain.models import (
    Announcement,
    ConsistencyViolation,
    Cursor,
    FetchTask,
    ParseResult,
    Politeness,
    Record,
    ResponseView,
    utc_now,
)
from ...domain.policy import scan_pii

__all__ = ["GUILDWARS2_NEWS", "RssAdapter", "parse_published"]

#: 官方公告源（RSS 2.0）
GUILDWARS2_NEWS: Final[dict[str, str]] = {
    "host": "www.guildwars2.com",
    "path": "/en/feed/",
}


class RssAdapter:
    """kind='announcement'，strategy='single' + 条件请求。"""

    source_id: str = "gw2_news_rss"
    kind: str = "announcement"

    def __init__(
        self,
        *,
        host: str = GUILDWARS2_NEWS["host"],
        path: str = GUILDWARS2_NEWS["path"],
        politeness: Politeness | None = None,
        version: str = "0.1.0",
        contact_url: str = "https://you.example/contact",
        observed_at_fallback: datetime | None = None,
    ) -> None:
        self.host = host
        self.path = path
        self.politeness = politeness or Politeness(
            rps=8.0,
            burst=12,
            concurrency=4,
            min_interval_seconds=900,  # NEWS_INTERVAL_S；304 不消耗配额
            respect_max_age=False,  # RSS 不返回 max-age，按配置周期走
            daily_request_budget=None,
        )
        self._version = version
        self._contact_url = contact_url
        self._fallback = observed_at_fallback

    # ---- 任务区 ----
    def initial_tasks(self, cursor: Cursor | None) -> Sequence[FetchTask]:
        """带上条件请求头（304 时不消耗配额）。"""
        headers: dict[str, str] = {}
        if cursor:
            if cursor.get("etag"):
                headers["If-None-Match"] = cursor["etag"]
            if cursor.get("last_modified"):
                headers["If-Modified-Since"] = cursor["last_modified"]
        return (
            FetchTask(
                task_id=f"{self.source_id}:feed",
                source_id=self.source_id,
                host=self.host,
                method="GET",
                path=self.path,
                params={},
                headers=headers,
                cost=1.0,
                strategy="single",
                cursor=cursor.get("etag") if cursor else None,
            ),
        )

    def next_tasks(self, task: FetchTask, view: ResponseView) -> Sequence[FetchTask]:
        """feed 无分页 → 恒 ()（可终止）。"""
        return ()

    # ---- 解析区（同步纯函数，用标准库 XML） ----
    def parse(self, task: FetchTask, view: ResponseView) -> ParseResult:
        fetched_at = task.observed_at or self._fallback or utc_now()
        cycle_id = task.cycle_id or ""

        if view.status_code == 304:
            # 条件请求命中：没有新内容，产出空记录集（不报错）
            return ParseResult(
                task_id=task.task_id,
                cycle_id=cycle_id,
                source_id=self.source_id,
                records=(),
                quality="COMPLETE",
            )

        failures: list[ConsistencyViolation] = []
        records: list[Record] = []

        try:
            # noqa: S314 —— 必须挂在 `ET.fromstring(` 这一行：black 把长调用拆行后，
            # 挂在闭合括号上的 noqa 会漂到下一行，不再抑制真正的违规行，ruff 立刻报 S314
            # （2026-10-03 全仓格式归一时踩到，别再挪回去）。
            root = ET.fromstring(  # noqa: S314 - 官方 feed，禁用实体展开由 Python 默认保证
                view.content
            )
        except ET.ParseError as exc:
            return ParseResult(
                task_id=task.task_id,
                cycle_id=cycle_id,
                source_id=self.source_id,
                records=(),
                quality="PARTIAL",
                consistency_failures=(
                    ConsistencyViolation(
                        rule="schema.xml_parse_error",
                        item_id=None,
                        field="body",
                        observed=str(exc),
                        expected="合法 XML",
                        severity="critical",
                    ),
                ),
            )

        for node in _iter_entries(root):
            ann, bad = self._one(node, fetched_at)
            if ann is not None:
                records.append(ann)
            failures.extend(bad)

        return ParseResult(
            task_id=task.task_id,
            cycle_id=cycle_id,
            source_id=self.source_id,
            records=tuple(records),
            quality="COMPLETE",
            consistency_failures=tuple(failures),
        )

    def _one(
        self, node: ET.Element, fetched_at: datetime
    ) -> tuple[Announcement | None, list[ConsistencyViolation]]:
        title = _text(node, "title") or ""
        link = _link(node)
        guid = _text(node, "guid") or _text(node, "id")
        summary = _text(node, "description") or _text(node, "summary")
        published = parse_published(
            _text(node, "pubDate") or _text(node, "published") or _text(node, "updated")
        )

        if not title and not link:
            return None, [
                ConsistencyViolation(
                    rule="schema.entry_empty",
                    item_id=None,
                    field="title/link",
                    observed="",
                    expected="至少 title 或 link 非空",
                    severity="critical",
                )
            ]

        # C8：title/summary 是自由文本，命中 PII 即丢弃该字段并留痕
        failures: list[ConsistencyViolation] = []
        for field_name, value in (("title", title), ("summary", summary)):
            hits = scan_pii(value)
            if hits:
                failures.extend(check_pii_field(None, field_name, hits))
                if field_name == "title":
                    title = ""
                else:
                    summary = None

        return (
            Announcement(
                source_id=self.source_id,
                # S324：sha1 只用于 guid 兜底（非安全场景），sha256 无必要
                guid=guid or hashlib.sha1(f"{link}|{title}".encode()).hexdigest(),  # noqa: S324
                title=title,
                link=link,
                published_at=published,
                summary=summary,
                content_hash=hashlib.sha256(f"{title}|{summary or ''}|{link}".encode()).hexdigest()[
                    :32
                ],
                fetched_at=fetched_at,
            ),
            failures,
        )

    # ---- 增量区 ----
    def cursor_from(self, view: ResponseView, result: ParseResult) -> Cursor | None:
        cursor: Cursor = {}
        if view.etag:
            cursor["etag"] = view.etag
        if view.last_modified:
            cursor["last_modified"] = view.last_modified
        return cursor or None

    # ---- 合规区 ----
    def user_agent_suffix(self) -> str:
        return f"market-watch/{self._version} (+{self._contact_url})"


# ---------------------------------------------------------------------------
def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter_entries(root: ET.Element) -> list[ET.Element]:
    """同时支持 RSS 2.0（channel/item）与 Atom（feed/entry）。"""
    out: list[ET.Element] = []
    for child in root:
        name = _localname(child.tag)
        if name in ("item", "entry"):
            out.append(child)
        elif name in ("channel", "feed"):
            out.extend(_iter_entries(child))
    return out


def _text(node: ET.Element, name: str) -> str | None:
    for child in node:
        if _localname(child.tag) == name:
            return (child.text or "").strip() or None
    return None


def _link(node: ET.Element) -> str:
    direct = _text(node, "link")
    if direct:
        return direct
    for child in node:
        if _localname(child.tag) == "link":
            href = child.attrib.get("href")
            if href:
                return href
    return ""


def parse_published(raw: str | None) -> datetime | None:
    """RFC 822（RSS pubDate）/ ISO8601（Atom）→ tz-aware UTC datetime。"""
    if not raw:
        return None
    text = raw.strip()
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
