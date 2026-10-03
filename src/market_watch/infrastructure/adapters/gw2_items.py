# src/market_watch/infrastructure/adapters/gw2_items.py
"""`/v2/items` 物品元数据（名称/类型/稀有度）补齐适配器。

实测契约（2026-10-03）：
    GET https://api.guildwars2.com/v2/items?ids=24,68 -> 200, 1.4 KB
      Cache-Control: public, max-age=3600
    body: [{"name":"Sealed Package of Snowballs","description":"...","type":"Consumable",
            "level":0,"rarity":"Basic","vendor_value":0,"game_types":["Pve"],
            "flags":["NoSalvage"],"restrictions":[],"id":24,
            "chat_link":"[&AgEYAAAA]","icon":"https://render.guildwars2.com/file/..."}]

C8 落点：本端点是**唯一含自由文本**（name / description）的源，因此对 `name`
做 PII 反向断言——命中即丢弃该字段（置空）并记 `data_reject`。`description`
我们本就不落库（不采集正文，只落元数据），从源头就不解析。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final

from ...domain.consistency import check_pii_field
from ...domain.models import (
    ConsistencyViolation,
    Cursor,
    FetchTask,
    ItemMeta,
    ParseResult,
    Politeness,
    Record,
    ResponseView,
    utc_now,
)
from ...domain.policy import scan_pii

__all__ = ["HOST", "MAX_IDS_PER_REQUEST", "PATH", "Gw2ItemsAdapter"]

HOST: Final[str] = "api.guildwars2.com"
PATH: Final[str] = "/v2/items"

#: 官方 `ids` 参数单次上限（保守取 200，与 page_size 对齐；超限官方会 400）
MAX_IDS_PER_REQUEST: Final[int] = 200


class Gw2ItemsAdapter:
    """kind='item_meta'，strategy='single'（批量 ids）。"""

    source_id: str = "gw2_items"
    kind: str = "item_meta"
    host: str = HOST

    def __init__(
        self,
        *,
        item_ids: Sequence[int] = (),
        politeness: Politeness | None = None,
        version: str = "0.1.0",
        contact_url: str = "https://you.example/contact",
        observed_at_fallback: datetime | None = None,
    ) -> None:
        self.item_ids = tuple(int(i) for i in item_ids)
        self.politeness = politeness or Politeness(
            rps=8.0,
            burst=12,
            concurrency=4,
            min_interval_seconds=3600,  # 实测 max-age=3600；日级刷新
            respect_max_age=True,
        )
        self._version = version
        self._contact_url = contact_url
        self._fallback = observed_at_fallback

    def with_item_ids(self, item_ids: Sequence[int]) -> Gw2ItemsAdapter:
        return Gw2ItemsAdapter(
            item_ids=item_ids,
            politeness=self.politeness,
            version=self._version,
            contact_url=self._contact_url,
            observed_at_fallback=self._fallback,
        )

    # ---- 任务区 ----
    def initial_tasks(self, cursor: Cursor | None) -> Sequence[FetchTask]:
        """按 MAX_IDS_PER_REQUEST 切片，减少请求数（唯一的合法提速路径）。"""
        if not self.item_ids:
            return ()
        chunks = [
            self.item_ids[i : i + MAX_IDS_PER_REQUEST]
            for i in range(0, len(self.item_ids), MAX_IDS_PER_REQUEST)
        ]
        return tuple(
            FetchTask(
                task_id=f"{self.source_id}:ids={chunk[0]}-{chunk[-1]}",
                source_id=self.source_id,
                host=self.host,
                method="GET",
                path=PATH,
                params={"ids": ",".join(str(i) for i in chunk)},
                cost=1.0,
                strategy="single",
            )
            for chunk in chunks
        )

    def next_tasks(self, task: FetchTask, view: ResponseView) -> Sequence[FetchTask]:
        """本适配器一次性把 id 切片排完，无需续页 → 恒 ()（可终止）。"""
        return ()

    # ---- 解析区 ----
    def parse(self, task: FetchTask, view: ResponseView) -> ParseResult:
        updated_at = task.observed_at or self._fallback or utc_now()
        cycle_id = task.cycle_id or ""
        payload = _safe_json(view)
        failures: list[ConsistencyViolation] = []
        records: list[Record] = []

        if not isinstance(payload, list):
            return ParseResult(
                task_id=task.task_id,
                cycle_id=cycle_id,
                source_id=self.source_id,
                records=(),
                quality="PARTIAL",
                consistency_failures=(
                    ConsistencyViolation(
                        rule="schema.payload_not_list",
                        item_id=None,
                        field="body",
                        observed=type(payload).__name__,
                        expected="数组",
                        severity="critical",
                    ),
                ),
            )

        for raw in payload:
            meta, bad = self._one(raw, updated_at)
            if meta is not None:
                records.append(meta)
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
        self, raw: object, updated_at: datetime
    ) -> tuple[ItemMeta | None, list[ConsistencyViolation]]:
        if not isinstance(raw, dict):
            return None, [
                ConsistencyViolation(
                    rule="schema.not_object",
                    item_id=None,
                    field="item",
                    observed=type(raw).__name__,
                    expected="object",
                    severity="critical",
                )
            ]
        try:
            item_id = int(raw["id"])
        except (KeyError, TypeError, ValueError):
            return None, [
                ConsistencyViolation(
                    rule="schema.missing_id",
                    item_id=None,
                    field="id",
                    observed=str(raw.get("id")),
                    expected="整数 id",
                    severity="critical",
                )
            ]

        # ---- C8 反向断言：name 是自由文本，命中 PII 即丢弃该字段 ----
        name = _as_str(raw.get("name")) or ""
        pii_hits = scan_pii(name)
        failures: list[ConsistencyViolation] = []
        if pii_hits:
            failures.extend(check_pii_field(item_id, "name", pii_hits))
            name = ""  # 字段级丢弃，行仍然保留（item_id 仍有意义）

        return (
            ItemMeta(
                source_id=self.source_id,
                item_id=item_id,
                name=name,
                item_type=_as_str(raw.get("type")),
                rarity=_as_str(raw.get("rarity")),
                updated_at=updated_at,
            ),
            failures,
        )

    # ---- 增量区 ----
    def cursor_from(self, view: ResponseView, result: ParseResult) -> Cursor | None:
        max_age = view.max_age_seconds
        return None if max_age is None else {"max_age": str(max_age)}

    # ---- 合规区 ----
    def user_agent_suffix(self) -> str:
        return f"market-watch/{self._version} (+{self._contact_url})"


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _safe_json(view: ResponseView) -> object:
    try:
        return view.json()
    except Exception:  # noqa: BLE001
        return []
