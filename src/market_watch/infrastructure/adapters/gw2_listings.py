# src/market_watch/infrastructure/adapters/gw2_listings.py
"""`/v2/commerce/listings` 观测清单原始挂单适配器（strategy='watchlist'）。

实测契约（2026-10-03）：
    GET https://api.guildwars2.com/v2/commerce/listings/24 -> 200, 77.8 KB
      Cache-Control: public, max-age=1200  （⚠️ 20 分钟，比 prices 的 120s 长得多）
    body: {"id":24,
           "buys":[{"listings":1,"unit_price":128,"quantity":56}, ...],  # 买盘**降序**
           "sells":[{"listings":2,"unit_price":197,"quantity":2}, ...]}  # 卖盘**升序**

⚠️ Phase 2 实测发现：`listings` 的 `max-age=1200`。按 §4.4 铁律
（`next_run_at = last_run_at + max(min_interval, max_age)`），本源的实际轮询周期会被
抬到 1200s，配置里的 `LISTINGS_INTERVAL_S=300` 不生效并打 `interval.clamped`。
这是尊重缓存的必然结果；若要缩短只能等官方缩短 max-age，不得改我们的间隔绕过。

五条硬规则（§7.3）同 gw2_prices：纯函数 / 可终止 / 容错 / 被钳制 / 单 host。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final

from ...domain.consistency import check_pii_field, validate_levels
from ...domain.models import (
    ConsistencyViolation,
    Cursor,
    FetchTask,
    ListingLevel,
    ParseResult,
    Politeness,
    Record,
    ResponseView,
    utc_now,
)
from ...domain.policy import scan_pii

__all__ = ["HOST", "PATH", "Gw2ListingsAdapter"]

HOST: Final[str] = "api.guildwars2.com"
PATH_PREFIX: Final[str] = "/v2/commerce/listings"
PATH: Final[str] = PATH_PREFIX


class Gw2ListingsAdapter:
    """kind='orderbook'，strategy='watchlist'。

    观测清单由调用方注入（`watchlist: Sequence[int]`），adapter 不发请求也不读 DB。
    `WATCHLIST_MAX_ITEMS=500` → 500 请求 @8rps ≈ 62.5s 理论下限（需实测确认）。
    """

    source_id: str = "gw2_listings"
    kind: str = "orderbook"
    host: str = HOST

    def __init__(
        self,
        *,
        watchlist: Sequence[int] = (),
        politeness: Politeness | None = None,
        version: str = "0.1.0",
        contact_url: str = "https://you.example/contact",
        observed_at_fallback: datetime | None = None,
    ) -> None:
        self.watchlist = tuple(int(i) for i in watchlist)
        self.politeness = politeness or Politeness(
            rps=8.0,
            burst=12,
            concurrency=4,
            min_interval_seconds=1200,  # 实测 max-age=1200
            respect_max_age=True,
        )
        self._version = version
        self._contact_url = contact_url
        self._fallback = observed_at_fallback

    def with_watchlist(self, watchlist: Sequence[int]) -> Gw2ListingsAdapter:
        """返回带新清单的副本（adapter 在声明后视为不可变）。"""
        clone = Gw2ListingsAdapter(
            watchlist=watchlist,
            politeness=self.politeness,
            version=self._version,
            contact_url=self._contact_url,
            observed_at_fallback=self._fallback,
        )
        return clone

    # ---- 任务区 ----
    def initial_tasks(self, cursor: Cursor | None) -> Sequence[FetchTask]:
        """每个观测 item 一个任务；无清单则返回空（不产生任何请求）。"""
        return tuple(
            FetchTask(
                task_id=f"{self.source_id}:item={item_id}",
                source_id=self.source_id,
                host=self.host,
                method="GET",
                path=f"{PATH_PREFIX}/{item_id}",
                params={},
                cost=1.0,
                strategy="watchlist",
            )
            for item_id in self.watchlist
        )

    def next_tasks(self, task: FetchTask, view: ResponseView) -> Sequence[FetchTask]:
        """单品端点无分页 → 恒返回 ()。**可终止性的最简形态**。"""
        return ()

    # ---- 解析区 ----
    def parse(self, task: FetchTask, view: ResponseView) -> ParseResult:
        observed_at = task.observed_at or self._fallback or utc_now()
        cycle_id = task.cycle_id or ""
        payload = _safe_json(view)
        failures: list[ConsistencyViolation] = []
        records: list[Record] = []

        if not isinstance(payload, dict):
            return ParseResult(
                task_id=task.task_id,
                cycle_id=cycle_id,
                source_id=self.source_id,
                records=(),
                quality="PARTIAL",
                consistency_failures=(
                    ConsistencyViolation(
                        rule="schema.payload_not_object",
                        item_id=None,
                        field="body",
                        observed=type(payload).__name__,
                        expected="object",
                        severity="critical",
                    ),
                ),
            )

        id_value = payload.get("id")
        # id 只可能是 JSON 的 int / float / str；其余（None / `{}` / bool）→ 判缺失
        item_id = int(id_value) if isinstance(id_value, (int, float, str)) else None
        if item_id is None:
            return ParseResult(
                task_id=task.task_id,
                cycle_id=cycle_id,
                source_id=self.source_id,
                records=(),
                quality="PARTIAL",
                consistency_failures=(
                    ConsistencyViolation(
                        rule="schema.missing_id",
                        item_id=None,
                        field="id",
                        observed=str(payload.get("id")),
                        expected="整数 id",
                        severity="critical",
                    ),
                ),
            )

        for side in ("buys", "sells"):
            raw_levels = payload.get(side) or []
            if not isinstance(raw_levels, list):
                failures.append(
                    ConsistencyViolation(
                        rule="schema.levels_not_list",
                        item_id=item_id,
                        field=side,
                        observed=type(raw_levels).__name__,
                        expected="数组",
                        severity="critical",
                    )
                )
                continue
            mapped = "buy" if side == "buys" else "sell"
            for raw in raw_levels:
                level, bad = self._one(item_id, mapped, raw, observed_at, cycle_id)
                if level is not None:
                    records.append(level)
                failures.extend(bad)

        # 顺序与跨源一致性（买盘降序 / 卖盘升序）
        levels = [r for r in records if isinstance(r, ListingLevel)]
        failures.extend(validate_levels(levels))

        # C8：本端点无自由文本字段，但对 id 做一次反向断言留痕
        failures.extend(check_pii_field(item_id, "id", scan_pii(payload.get("id"))))

        return ParseResult(
            task_id=task.task_id,
            cycle_id=cycle_id,
            source_id=self.source_id,
            records=tuple(records),
            quality="COMPLETE",
            consistency_failures=tuple(failures),
        )

    def _one(
        self,
        item_id: int,
        side: str,
        raw: object,
        observed_at: datetime,
        cycle_id: str,
    ) -> tuple[ListingLevel | None, list[ConsistencyViolation]]:
        if not isinstance(raw, dict):
            return None, [
                ConsistencyViolation(
                    rule="schema.level_not_object",
                    item_id=item_id,
                    field=side,
                    observed=type(raw).__name__,
                    expected="object",
                    severity="critical",
                )
            ]
        unit_price = _as_int(raw.get("unit_price"))
        quantity = _as_int(raw.get("quantity"))
        listings = _as_int(raw.get("listings"))
        if unit_price is None or quantity is None:
            return None, [
                ConsistencyViolation(
                    rule="schema.level_missing_field",
                    item_id=item_id,
                    field=f"{side}.unit_price/quantity",
                    observed=f"unit_price={unit_price} quantity={quantity}",
                    expected="两个字段均为整数",
                    severity="critical",
                )
            ]
        level = ListingLevel(
            source_id=self.source_id,
            item_id=item_id,
            observed_at=observed_at,
            cycle_id=cycle_id,
            side="buy" if side == "buy" else "sell",
            unit_price=unit_price,
            quantity=quantity,
            listings=1 if listings is None else listings,
        )
        return level, []

    # ---- 增量区 ----
    def cursor_from(self, view: ResponseView, result: ParseResult) -> Cursor | None:
        max_age = view.max_age_seconds
        if max_age is None:
            return None
        return {"max_age": str(max_age)}

    # ---- 合规区 ----
    def user_agent_suffix(self) -> str:
        return f"market-watch/{self._version} (+{self._contact_url})"


def _as_int(value: object) -> int | None:
    """把 JSON 原始字段转成整数；不可转（None / bool / `{}` / 其他结构）→ None。

    Args:
        value: `view.json()` 取出的原始字段。JSON 语法把它限定为 `int` / `float` /
            `str` / `None`，其余标量（`{}` 空对象、`[]`、嵌套结构）一律 → None。

    收窄理由：`view.json()` 声明返回 `Any`，参数只能标 `object`；而 `int(object)`
    没有匹配的重载（typeshed 只接受 `str | bytes | SupportsInt | ...`），直接喂会把
    返回值退化成 `Any` 并触发 `no-any-return`。故先用 `isinstance` 收窄再交给 `int()`。
    """
    if value is None or isinstance(value, bool):
        # bool 是真 int 子类：JSON 里 `true` 不可能当 id / 单价，判 None 而非 int(True)=1
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        # 不可转整数（如 `int("abc")`）→ None，与收窄前完全一致
        return None


def _safe_json(view: ResponseView) -> object:
    try:
        return view.json()
    except Exception:  # noqa: BLE001
        return {}
