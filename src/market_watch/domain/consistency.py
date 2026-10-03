# src/market_watch/domain/consistency.py
"""★ 取代被移除的「多尺度匹配」：多源冗余校验 + 数据一致性校验。

架构依据：docs/01-architecture.md §11.1 附加说明（1）（2）（3）、§6 错误矩阵 #12。

职责边界：
- 本模块**只产出** `ConsistencyViolation`，不做丢弃决策。由调用方决定
  「落 data_reject + 隔离快照（异动检测不得消费）」。
- 全部规则是纯函数：输入 domain 模型，输出违规列表。零 IO、零时钟读取。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise
from typing import Final

from .models import ConsistencyViolation, ListingLevel, PriceTick
from .policy import MAX_PRICE

__all__ = [
    "MAX_JUMP_RATIO",
    "PRICE_CROSS_TOLERANCE",
    "SUSPICIOUS",
    "UNIVERSE_DRIFT_LIMIT",
    "check_cross_source",
    "check_jump",
    "check_level_ordering",
    "check_listing_level",
    "check_pii_field",
    "check_price_range",
    "check_spread",
    "check_universe_size",
    "is_monotonic_ascending",
    "is_monotonic_descending",
    "validate_levels",
    "validate_tick",
]

#: 跨源比对的容差：±1 tick。两个端点的快照存在秒级时序差，严格相等会误报。
PRICE_CROSS_TOLERANCE: Final[int] = 1

#: 环比跳变上限（倍）。超过标记 suspicious，**不丢弃**。
MAX_JUMP_RATIO: Final[float] = 10.0

#: 本轮 universe 相对上轮允许的波动（±5%）
UNIVERSE_DRIFT_LIMIT: Final[float] = 0.05

#: 被隔离的快照标记（异动检测跳过）
SUSPICIOUS: Final[str] = "QUARANTINED"


def _violation(
    rule: str,
    item_id: int | None,
    field: str,
    observed: object,
    expected: str,
    severity: str = "warn",
) -> ConsistencyViolation:
    return ConsistencyViolation(
        rule=rule,
        item_id=item_id,
        field=field,
        observed=str(observed),
        expected=expected,
        severity="warn" if severity == "warn" else "critical",
    )


def check_price_range(
    item_id: int | None, field: str, value: int | None
) -> list[ConsistencyViolation]:
    """规则：unit_price 为 >0 的整数且 ≤ 1e9。

    None 不算违规——官方端点对无挂单的物品返回空对象，缺失是合法态（§6.1 禁止把
    「本轮没抓到」误判成「价格归零」）。
    """
    if value is None:
        return []
    if not isinstance(value, int) or isinstance(value, bool):
        return [_violation("price.is_integer", item_id, field, value, "整数", "critical")]
    if value <= 0:
        return [_violation("price.positive", item_id, field, value, "> 0", "critical")]
    if value > MAX_PRICE:
        return [
            _violation("price.upper_bound", item_id, field, value, f"<= {MAX_PRICE}", "critical")
        ]
    return []


def check_listing_level(level: ListingLevel) -> list[ConsistencyViolation]:
    """规则：unit_price > 0；listings >= 1；quantity >= 0。"""
    out: list[ConsistencyViolation] = []
    out.extend(check_price_range(level.item_id, "unit_price", level.unit_price))
    if level.listings < 1:
        out.append(_violation("listings.min", level.item_id, "listings", level.listings, ">= 1"))
    if level.quantity < 0:
        out.append(
            _violation("quantity.non_negative", level.item_id, "quantity", level.quantity, ">= 0")
        )
    return out


def is_monotonic_descending(values: Sequence[int]) -> bool:
    """判断是否单调递减（允许并列：`a >= b`）。

    Args:
        values: 档位价格序列。

    Returns:
        序列为空或只有 1 个元素时 `True`（无违例可查）。
    """
    return all(a >= b for a, b in pairwise(values))


def is_monotonic_ascending(values: Sequence[int]) -> bool:
    """判断是否单调递增（允许并列：`a <= b`）。语义同上，buy 侧之外的对称校验。"""
    return all(a <= b for a, b in pairwise(values))


def check_level_ordering(
    item_id: int, levels: Sequence[ListingLevel]
) -> list[ConsistencyViolation]:
    """规则：buy 侧 unit_price 单调递减、sell 侧单调递增（端点契约，违反即可疑数据）。"""
    out: list[ConsistencyViolation] = []
    for side, ok in (("buy", is_monotonic_descending), ("sell", is_monotonic_ascending)):
        prices = [lv.unit_price for lv in levels if lv.side == side]
        if len(prices) >= 2 and not ok(prices):
            out.append(
                _violation(
                    "levels.ordering",
                    item_id,
                    f"{side}.unit_price",
                    prices[:8],
                    "buy 降序 / sell 升序",
                )
            )
    return out


def check_spread(
    bid: int | None, ask: int | None, item_id: int | None = None
) -> list[ConsistencyViolation]:
    """规则：spread = ask - bid 必须 >= 0（允许 zero-spread，不允许负 spread）。"""
    if bid is None or ask is None:
        return []
    if ask - bid < 0:
        return [
            _violation("spread.non_negative", item_id, "spread", ask - bid, ">= 0 (ask >= bid)")
        ]
    return []


def check_cross_source(
    tick: PriceTick,
    best_bid: int | None,
    best_ask: int | None,
) -> list[ConsistencyViolation]:
    """★ 多源冗余：/v2/commerce/prices 与 /v2/commerce/listings 互相印证。

    prices.sells.unit_price 必须等于 listings 中 side='sell' 的最低 unit_price；
    prices.buys.unit_price 必须等于 side='buy' 的最高 unit_price。容忍 ±1 tick。
    """
    out: list[ConsistencyViolation] = []
    for field, cross, expected_desc in (
        ("sells.unit_price", best_ask, "listings 最低卖价"),
        ("buys.unit_price", best_bid, "listings 最高买价"),
    ):
        observed = tick.sell_unit_price if field == "sells.unit_price" else tick.buy_unit_price
        if observed is None or cross is None:
            continue  # 一侧缺失无法印证，不算违规
        if abs(observed - cross) > PRICE_CROSS_TOLERANCE:
            out.append(
                _violation(
                    "cross_source.price_mismatch",
                    tick.item_id,
                    field,
                    observed,
                    f"{expected_desc}={cross} (±{PRICE_CROSS_TOLERANCE})",
                )
            )
    return out


def check_jump(
    item_id: int, field: str, prev: float | None, curr: float | None
) -> list[ConsistencyViolation]:
    """规则：环比跳变 ≤ MAX_JUMP_RATIO 倍，否则标记 suspicious（隔离）而非丢弃。"""
    if prev is None or curr is None:
        return []
    if prev <= 0:
        return []
    ratio = max(curr, prev) / min(curr, prev) if min(curr, prev) > 0 else float("inf")
    if ratio > MAX_JUMP_RATIO:
        return [
            _violation(
                "price.jump_suspicious",
                item_id,
                field,
                f"prev={prev} curr={curr} ratio={ratio:.2f}",
                f"ratio <= {MAX_JUMP_RATIO}",
            )
        ]
    return []


def check_universe_size(prev_total: int | None, curr_total: int) -> list[ConsistencyViolation]:
    """规则：本轮 universe 总数相对上轮波动 ≤ ±5%。"""
    if prev_total is None or prev_total <= 0:
        return []
    drift = abs(curr_total - prev_total) / prev_total
    if drift > UNIVERSE_DRIFT_LIMIT:
        return [
            _violation(
                "universe.drift",
                None,
                "result_total",
                f"prev={prev_total} curr={curr_total} drift={drift:.2%}",
                f"drift <= {UNIVERSE_DRIFT_LIMIT:.0%}",
            )
        ]
    return []


def check_pii_field(
    item_id: int | None, field: str, hits: Sequence[str]
) -> list[ConsistencyViolation]:
    """C8 反向断言的落点：字段命中 PII 正则 → 记为 critical 违规，由调用方丢弃该字段。

    Args:
        item_id: 商品 id。
        field: 原始字段名（仅用于违规记录）。
        hits: `policy.scan_pii()` 返回的命中规则名列表。

    Returns:
        长度 0 或 1 的列表（命中即一条 critical 违规；调用方据此丢弃该字段 +
        记 `data_reject` + 发 warn 告警）。
    """
    return [
        _violation("pii.field_rejected", item_id, field, ",".join(hits), "不含个人信息", "critical")
        for _ in range(1 if hits else 0)
    ]


# ===========================================================================
# 组合入口
# ===========================================================================
def validate_tick(
    tick: PriceTick,
    *,
    prev: Mapping[str, float] | None = None,
    best_bid: int | None = None,
    best_ask: int | None = None,
) -> list[ConsistencyViolation]:
    """对一个 `PriceTick` 跑全部适用规则（价格侧校验的唯一组合入口）。

    本模块**只产出** `ConsistencyViolation`，不做丢弃决策；「落 `data_reject`
    + 隔离快照（异动检测不得消费）」由调用方（`price_ingest` / `orchestrator`）决定。

    Args:
        tick: 待校验快照。
        prev: `{metric: 上一轮值}`，提供后才会跑跳变检测（check_jump）。
        best_bid: 来自 `/v2/commerce/listings` 的最高买价（跨源印证的另一端）。
        best_ask: 来自 `/v2/commerce/listings` 的最低卖价。

    Returns:
        违规列表；空列表 = 通过。规则命中数与明细在看板「数据质量」卡片可见。
    """
    out: list[ConsistencyViolation] = []
    out.extend(check_price_range(tick.item_id, "buy_unit_price", tick.buy_unit_price))
    out.extend(check_price_range(tick.item_id, "sell_unit_price", tick.sell_unit_price))
    if tick.buy_quantity is not None and tick.buy_quantity < 0:
        out.append(
            _violation(
                "quantity.non_negative", tick.item_id, "buy_quantity", tick.buy_quantity, ">= 0"
            )
        )
    if tick.sell_quantity is not None and tick.sell_quantity < 0:
        out.append(
            _violation(
                "quantity.non_negative", tick.item_id, "sell_quantity", tick.sell_quantity, ">= 0"
            )
        )
    out.extend(check_spread(tick.buy_unit_price, tick.sell_unit_price, tick.item_id))
    out.extend(check_cross_source(tick, best_bid, best_ask))
    if prev:
        out.extend(
            check_jump(
                tick.item_id, "sell_unit_price", prev.get("sell_unit_price"), tick.sell_unit_price
            )
        )
        out.extend(
            check_jump(
                tick.item_id, "buy_unit_price", prev.get("buy_unit_price"), tick.buy_unit_price
            )
        )
    return out


def validate_levels(
    levels: Iterable[ListingLevel],
    *,
    tick: PriceTick | None = None,
) -> list[ConsistencyViolation]:
    """对一批 `ListingLevel` 跑全部适用规则（挂单侧组合入口）。

    Args:
        levels: 同一 item 的全部档位（buy + sell 混合）。
        tick: 可选的同一 item 的 `PriceTick`，提供后追加一次跨源印证。

    Returns:
        违规列表；空列表 = 通过。
    """
    seq = list(levels)
    out: list[ConsistencyViolation] = []
    for lv in seq:
        out.extend(check_listing_level(lv))
    if seq:
        item_id = seq[0].item_id
        out.extend(check_level_ordering(item_id, seq))
        if tick is not None:
            bids = [lv.unit_price for lv in seq if lv.side == "buy"]
            asks = [lv.unit_price for lv in seq if lv.side == "sell"]
            out.extend(check_cross_source(tick, max(bids, default=None), min(asks, default=None)))
    return out
