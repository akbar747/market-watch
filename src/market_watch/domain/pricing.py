# src/market_watch/domain/pricing.py
"""盘口计算与 OHLC/VWAP 聚合。纯函数，零 IO。

架构依据：docs/01-architecture.md §7.2（PriceAggregate）、§8.3（price_hourly rollup）。

语义锚点（§1.3，不要反推）：
    buys.unit_price  = 最高买价 best bid
    sells.unit_price = 最低卖价 best ask
    listings 的 buy 侧按 unit_price **降序**、sell 侧按 **升序** 返回。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Final, NamedTuple

from .models import ListingLevel, PriceAggregate, PriceTick

__all__ = [
    "MAX_JUMP_RATIO",
    "OhlcAccumulator",
    "OhlcBar",
    "aggregate_levels",
    "best_bid_ask",
    "bucket_start",
    "depth",
    "fold_ohlc",
    "mid_of",
    "pct_change",
    "spread_of",
    "tick_value",
    "vwap",
]

#: §11.1：环比跳变上限。超过标记为 suspicious（隔离），**不是丢弃**。
MAX_JUMP_RATIO: Final[float] = 10.0


class _BidAsk(NamedTuple):
    bid: int | None
    ask: int | None


def _levels(rows: Iterable[ListingLevel], side: str) -> list[ListingLevel]:
    return [r for r in rows if r.side == side]


def best_bid_ask(levels: Sequence[ListingLevel]) -> tuple[int | None, int | None]:
    """由原始档位求 best bid / best ask。

    best bid = buy 侧最高 unit_price；best ask = sell 侧最低 unit_price。
    空盘返回 None（不猜 0——0 会被下游误读成「价格归零」，见 §6.1）。
    """
    buys = _levels(levels, "buy")
    sells = _levels(levels, "sell")
    bid = max((r.unit_price for r in buys), default=None)
    ask = min((r.unit_price for r in sells), default=None)
    return bid, ask


def spread_of(bid: int | None, ask: int | None) -> int | None:
    """spread = ask - bid。任一侧缺失返回 None（不臆造）。"""
    if bid is None or ask is None:
        return None
    return ask - bid


def mid_of(bid: int | None, ask: int | None) -> int | None:
    """算中价 `(bid + ask) // 2`（整数除法，铜币不能带小数）。

    Args:
        bid: best bid；`None` 表示买盘为空。
        ask: best ask；`None` 表示卖盘为空。

    Returns:
        中价；任一侧缺失返回 `None`（不臆造 0——0 会被下游误读成「价格归零」）。
    """
    if bid is None or ask is None:
        return None
    return (bid + ask) // 2


def vwap(levels: Sequence[ListingLevel]) -> float | None:
    """算成交量加权均价 = Σ(price*qty)/Σ(qty)。

    Args:
        levels: 某侧的全部档位。

    Returns:
        加权均价；档位为空或总量为 0 时返回 `None`（避免 `ZeroDivisionError`）。
    """
    total_qty = sum(r.quantity for r in levels)
    if total_qty <= 0:
        return None
    return sum(r.unit_price * r.quantity for r in levels) / total_qty


def depth(levels: Sequence[ListingLevel], side: str) -> int:
    """算盘口水深（该侧累计挂单量）。

    Args:
        levels: 全部档位。
        side: `'buy'` 或 `'sell'`。

    Returns:
        该侧 `quantity` 之和（非负整数；空侧为 0）。
    """
    return sum(r.quantity for r in levels if r.side == side)


def aggregate_levels(
    item_id: int, bucket_ts: datetime, levels: Sequence[ListingLevel]
) -> PriceAggregate:
    """把一批原始档位聚合成一个桶的 `PriceAggregate`。

    Args:
        item_id: 商品 id。
        bucket_ts: 桶起始时刻（通常 `bucket_start(ts)` 的产出）。
        levels: 该桶内的原始挂单档位（可含两侧）。

    Returns:
        `PriceAggregate`；空 levels 也会返回结构体（全 `None` / `samples=0`），
        不抛异常——空桶本身是合法态（§6.1）。
    """
    buys = _levels(levels, "buy")
    sells = _levels(levels, "sell")
    bid, ask = best_bid_ask(levels)
    return PriceAggregate(
        item_id=item_id,
        bucket_ts=bucket_ts,
        best_bid=bid,
        best_ask=ask,
        spread=spread_of(bid, ask),
        mid=mid_of(bid, ask),
        vwap_buy=vwap(buys),
        vwap_sell=vwap(sells),
        depth_buy=depth(levels, "buy"),
        depth_sell=depth(levels, "sell"),
        samples=len(levels),
    )


def bucket_start(ts: datetime, unit: str = "hour") -> datetime:
    """把时间戳对齐到桶起点。

    Args:
        ts: 任意时刻（tz-aware 会被保留，naive 按 UTC 处理——`iso_z` 落库时再归一）。
        unit: `'hour'`（默认，与 `price_hourly.bucket_ts` 的 `'YYYY-MM-DDTHH:00:00Z'` 对齐）
            / `'minute'` / `'day'`。

    Returns:
        桶起点时间戳（与入参 tzinfo 一致）。

    Raises:
        ValueError: `unit` 不是上述三者之一。
    """
    if unit == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    if unit == "day":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "minute":
        return ts.replace(second=0, microsecond=0)
    raise ValueError(f"不支持的桶单位: {unit!r}")


class OhlcBar(NamedTuple):
    """一个 (open, high, low, close, avg, samples) 元组，供 rollup 与看板共用。"""

    open: int | None
    high: int | None
    low: int | None
    close: int | None
    avg: float | None
    samples: int


class OhlcAccumulator:
    """增量累加 OHLC。首次值决定 open，之后 close 跟随最新值。

    这样 rollup 无论重跑几次（幂等重算）都能得到同样的 high/low/close；
    `samples` 由调用方按已滚行数累加，不在本类内自增。
    """

    __slots__ = ("_close", "_high", "_low", "_n", "_open", "_sum")

    def __init__(self) -> None:
        self._open: int | None = None
        self._high: int | None = None
        self._low: int | None = None
        self._close: int | None = None
        self._sum: float = 0.0
        self._n: int = 0

    def add(self, value: int | None) -> None:
        """累加一个观测值。

        `None` 直接忽略（缺失不等于 0）。首次非空值锁定为 `open`，
        之后 `close` 跟随最新值。

        Args:
            value: 单价（铜币整数）；`None` 表示这条记录没有有效价。
        """
        if value is None:
            return
        if self._open is None:
            self._open = value
        self._high = value if self._high is None else max(self._high, value)
        self._low = value if self._low is None else min(self._low, value)
        self._close = value
        self._sum += float(value)
        self._n += 1

    def merge(self, other: OhlcBar) -> None:
        """把一个已落盘的桶的统计量合并进来。

        用于 rollup 的幂等路径（桶已存在时把旧 `open` 接回来，避免整点重算后
        `open` 变成该小时内第一条而非历史那条）。

        Args:
            other: 已存在的桶。
        """

        if other.open is not None and self._open is None:
            self._open = other.open
        for value in (other.high, other.low, other.close):
            if value is None:
                continue
            self._high = value if self._high is None else max(self._high, value)
            self._low = value if self._low is None else min(self._low, value)
        if other.close is not None:
            self._close = other.close
        if other.samples:
            self._n += other.samples
            if other.avg is not None:
                self._sum += other.avg * other.samples

    @property
    def bar(self) -> OhlcBar:
        """导出当前桶的 `OhlcBar`。

        Returns:
            `(open, high, low, close, avg, samples)`；`samples == 0` 时
            前五项为 `None`。
        """
        avg = (self._sum / self._n) if self._n else None
        return OhlcBar(self._open, self._high, self._low, self._close, avg, self._n)


def fold_ohlc(values: Sequence[int | None]) -> OhlcBar:
    acc = OhlcAccumulator()
    for v in values:
        acc.add(v)
    return acc.bar


def pct_change(prev: float | None, curr: float | None) -> float | None:
    """算环比百分比。

    Args:
        prev: 上一轮值。
        curr: 本轮值。

    Returns:
        百分比（`(curr - prev) / prev * 100`）；`prev` 为 `None` / `0` 或
        `curr` 为 `None` 时返回 `None`（无法定义百分比，**不返回 `inf`**）。
    """
    if prev is None or curr is None or prev == 0:
        return None
    return (curr - prev) / prev * 100.0


def tick_value(tick: PriceTick, metric: str) -> float | None:
    """按 metric 名从 `PriceTick` 取数值（anomaly 规则表驱动的取值入口）。

    Args:
        tick: 快照。
        metric: `'sell_unit_price'` / `'buy_unit_price'` / `'sell_quantity'` /
            `'buy_quantity'`。

    Returns:
        该指标的整数值转 `float`；字段为 `None` 或 metric 名未登记时返回 `None`
        （anomaly 会跳过该条规则，不报错）。
    """
    table = {
        "sell_unit_price": tick.sell_unit_price,
        "buy_unit_price": tick.buy_unit_price,
        "sell_quantity": tick.sell_quantity,
        "buy_quantity": tick.buy_quantity,
    }
    value = table.get(metric)
    return None if value is None else float(value)
