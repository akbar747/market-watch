# src/market_watch/infrastructure/adapters/gw2_prices.py
"""`/v2/commerce/prices` 分页全量行情快照适配器。

实测契约（2026-10-03，见 docs/04-compliance.md）：
    GET https://api.guildwars2.com/v2/commerce/prices?page=0&page_size=200
    -> 200, X-Page-Total: 140, X-Page-Size: 200, X-Result-Total: 27997,
       X-Rate-Limit-Limit: 600, Cache-Control: public, max-age=120
    body: [{"id":24,"whitelisted":false,
            "buys":{"quantity":108640,"unit_price":128},   # 最高买价 best bid
            "sells":{"quantity":282033,"unit_price":200}}, # 最低卖价 best ask  → ...]

**`sells.unit_price` 是「最低卖价」、`buys.unit_price` 是「最高买价」——这是端点官方
语义，不要反推**（§1.3）。

五条硬规则（§7.3）：
1. 全部方法是同步纯函数：不 await、不 IO、不读时钟（时间走 `FetchTask.observed_at`）。
2. `next_tasks` 可终止：`page + 1 >= X-Page-Total` 或空数组 → 返回 ()。
3. `parse` 单条坏数据不中断整页 → 进 `consistency_failures`。
4. `politeness` 是声明值，会被 Config 钳制，无权突破。
5. 单一 host；**禁止**在 adapter 内 new httpx.Client。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final

from ...domain.consistency import check_pii_field, validate_tick
from ...domain.models import (
    ConsistencyViolation,
    Cursor,
    FetchTask,
    ParseResult,
    Politeness,
    PriceTick,
    Quality,
    Record,
    ResponseView,
    utc_now,
)

__all__ = ["HOST", "MAX_PAGE_SIZE", "PATH", "Gw2PricesAdapter"]

HOST: Final[str] = "api.guildwars2.com"
PATH: Final[str] = "/v2/commerce/prices"
MAX_PAGE_SIZE: Final[int] = 200  # 官方上限；**用满它是提速的唯一合法路径**


class Gw2PricesAdapter:
    """kind='price'，strategy='paged'。"""

    source_id: str = "gw2_prices"
    kind: str = "price"
    host: str = HOST

    def __init__(
        self,
        *,
        page_size: int = MAX_PAGE_SIZE,
        politeness: Politeness | None = None,
        version: str = "0.1.0",
        contact_url: str = "https://you.example/contact",
        observed_at_fallback: datetime | None = None,
    ) -> None:
        # page_size 用满 200：这是「想提速」时唯一合法的旋钮（禁止提高 rps/concurrency）
        self.page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        self.politeness = politeness or Politeness(
            rps=8.0,  # = 官方 600/min ÷ 60 × 0.8（C3 换算上限）
            burst=12,
            concurrency=4,
            min_interval_seconds=120,  # 实测 Cache-Control: max-age=120
            respect_max_age=True,
            daily_request_budget=None,
        )
        self._version = version
        self._contact_url = contact_url
        self._fallback = observed_at_fallback

    # ---- 任务区（同步纯函数） ----
    def initial_tasks(self, cursor: Cursor | None) -> Sequence[FetchTask]:
        """阶段 A 探路：page=0。cursor 对本端点无意义（每轮全量快照）。"""
        return (
            FetchTask(
                task_id=f"{self.source_id}:page=0",
                source_id=self.source_id,
                host=self.host,
                method="GET",
                path=PATH,
                params={"page": "0", "page_size": str(self.page_size)},
                cost=1.0,
                strategy="paged",
            ),
        )

    def next_tasks(self, task: FetchTask, view: ResponseView) -> Sequence[FetchTask]:
        """阶段 B 推进。终止条件：`page+1 >= X-Page-Total` 或空数组。

        恶意 mock「永不分页」（永远同样的 X-Page-Total 且永远有数据）时，
        依赖调用方 `max_pages` 硬护栏收敛；本方法自身严格按页号递增，不会原地打转。
        """
        page_total = view.page_total
        try:
            current = int(dict(task.params).get("page", "0"))
        except (TypeError, ValueError):
            return ()
        nxt = current + 1
        if page_total is None:
            # 没有 X-Page-Total：回退「取到空页为止」模式
            payload = _safe_json(view)
            if not payload:
                return ()
        elif nxt >= page_total:
            return ()
        return (
            FetchTask(
                task_id=f"{self.source_id}:page={nxt}",
                source_id=self.source_id,
                host=self.host,
                method="GET",
                path=PATH,
                params={"page": str(nxt), "page_size": str(self.page_size)},
                cost=1.0,
                strategy="paged",
                observed_at=task.observed_at,
                cycle_id=task.cycle_id,
            ),
        )

    # ---- 解析区（同步纯函数） ----
    def parse(self, task: FetchTask, view: ResponseView) -> ParseResult:
        observed_at = task.observed_at or self._fallback or utc_now()
        cycle_id = _cycle_of(task)
        payload = _safe_json(view)

        records: list[Record] = []
        failures: list[ConsistencyViolation] = []

        if not isinstance(payload, list):
            failures.append(
                ConsistencyViolation(
                    rule="schema.payload_not_list",
                    item_id=None,
                    field="body",
                    observed=type(payload).__name__,
                    expected="数组",
                    severity="critical",
                )
            )
            return ParseResult(
                task_id=task.task_id,
                cycle_id=cycle_id,
                source_id=self.source_id,
                records=(),
                quality="PARTIAL",
                consistency_failures=tuple(failures),
            )

        for raw in payload:
            tick, bad = self._one(raw, observed_at, cycle_id)
            if tick is not None:
                records.append(tick)
            failures.extend(bad)

        return ParseResult(
            task_id=task.task_id,
            cycle_id=cycle_id,
            source_id=self.source_id,
            records=tuple(records),
            cursor=None,
            quality="COMPLETE",
            consistency_failures=tuple(failures),
        )

    def _one(
        self, raw: object, observed_at: datetime, cycle_id: str
    ) -> tuple[PriceTick | None, list[ConsistencyViolation]]:
        """单条记录转换。**坏记录返回 (None, violations)，绝不抛异常**（规则 3）。"""
        failures: list[ConsistencyViolation] = []
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
                    observed=str(raw.get("id")) if isinstance(raw, dict) else "?",
                    expected="整数 id",
                    severity="critical",
                )
            ]

        buys = raw.get("buys") or {}
        sells = raw.get("sells") or {}
        if not isinstance(buys, dict):
            buys = {}
        if not isinstance(sells, dict):
            sells = {}

        tick = PriceTick(
            source_id=self.source_id,
            item_id=item_id,
            observed_at=observed_at,
            cycle_id=cycle_id,
            buy_unit_price=_as_int(buys.get("unit_price")),
            buy_quantity=_as_int(buys.get("quantity")),
            sell_unit_price=_as_int(sells.get("unit_price")),
            sell_quantity=_as_int(sells.get("quantity")),
            whitelisted=bool(raw.get("whitelisted", False)),
            quality="COMPLETE",
        )

        failures.extend(validate_tick(tick))
        # C8 反向断言：本端点无自由文本字段，但仍跑一遍（字段新增时的兜底）
        for field in ("id",):
            hits = check_pii_field(item_id, field, _pii_hits(raw.get(field)))
            failures.extend(hits)
        return tick, failures

    # ---- 增量区 ----
    def cursor_from(self, view: ResponseView, result: ParseResult) -> Cursor | None:
        """`X-Result-Total` 用于下轮的 universe 漂移比对（§11.1-2）。"""
        total = view.result_total
        max_age = view.max_age_seconds
        if total is None and max_age is None:
            return None
        cursor: Cursor = {}
        if total is not None:
            cursor["result_total"] = str(total)
        if max_age is not None:
            cursor["max_age"] = str(max_age)
        return cursor or None

    # ---- 合规区 ----
    def user_agent_suffix(self) -> str:
        return f"market-watch/{self._version} (+{self._contact_url})"


# ---------------------------------------------------------------------------
# 局部工具（保持 adapter 无 IO）
# ---------------------------------------------------------------------------
def _as_int(value: object) -> int | None:
    """官方对无挂单侧返回空对象 `{}` → None。None 是合法态，不是 0。

    Args:
        value: `view.json()` 取出的原始字段。JSON 语法把它限定为 `int` / `float` /
            `str` / `None`，其余标量（`{}` 空对象、`[]`、嵌套结构）一律 → None。

    收窄理由：`view.json()` 声明返回 `Any`，参数只能标 `object`；而 `int(object)`
    没有匹配的重载（typeshed 只接受 `str | bytes | SupportsInt | ...`），直接喂会把
    返回值退化成 `Any` 并触发 `no-any-return`。故先用 `isinstance` 收窄到 JSON 里
    真正可能出现的三种标量，再交给 `int()`。
    """
    if value is None:
        return None
    if not isinstance(value, (int, float, str)):
        # 显式覆盖 docstring 承诺的 `{}` / `[]` / 嵌套结构：整数不可转 → None
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        # 不可转整数（如 `int("abc")`）→ None，与收窄前完全一致
        return None


def _safe_json(view: ResponseView) -> object:
    try:
        return view.json()
    except Exception:  # noqa: BLE001 - 由上层分类为 JSON_DECODE；这里返回空以便不中断
        return []


def _cycle_of(task: FetchTask) -> str:
    """cycle_id 由调用方注入 `FetchTask.cycle_id`（adapter 不得自己生成）。"""
    return task.cycle_id or ""


def _pii_hits(value: object) -> list[str]:
    from ...domain.policy import scan_pii

    return scan_pii(value)


def quality_of(drift_ratio: float) -> Quality:
    return "PARTIAL" if drift_ratio > 0.10 else "COMPLETE"
