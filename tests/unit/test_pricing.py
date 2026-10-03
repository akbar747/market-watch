# tests/unit/test_pricing.py
"""盘口聚合与 OHLC（§7.2 / §8.3）。语义锚点：buys=最高买价，sells=最低卖价。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from market_watch.domain.models import ListingLevel
from market_watch.domain.pricing import (
    MAX_JUMP_RATIO,
    OhlcAccumulator,
    aggregate_levels,
    best_bid_ask,
    bucket_start,
    depth,
    fold_ohlc,
    mid_of,
    pct_change,
    spread_of,
    tick_value,
    vwap,
)

_TS = datetime(2026, 1, 2, 3, 45, 12, tzinfo=timezone.utc)


def _lv(side: str, price: int, qty: int, listings: int = 1) -> ListingLevel:
    return ListingLevel(
        source_id="gw2_listings",
        item_id=24,
        observed_at=_TS,
        cycle_id="c1",
        side=side,  # type: ignore[arg-type]
        unit_price=price,
        quantity=qty,
        listings=listings,
    )


class TestBestBidAsk:
    def test_buy_side_max_is_bid(self) -> None:
        levels = [_lv("buy", 128, 56), _lv("buy", 125, 10), _lv("buy", 120, 3)]
        bid, ask = best_bid_ask(levels)
        assert bid == 128  # ★ 最高买价
        assert ask is None

    def test_sell_side_min_is_ask(self) -> None:
        levels = [_lv("sell", 197, 2), _lv("sell", 198, 5), _lv("sell", 210, 1)]
        bid, ask = best_bid_ask(levels)
        assert ask == 197  # ★ 最低卖价
        assert bid is None

    def test_empty_book_returns_none_not_zero(self) -> None:
        """空盘必须返回 None，绝不能是 0（§6.1：0 会被下游读成「价格归零」）。"""
        assert best_bid_ask([]) == (None, None)
        assert spread_of(None, None) is None
        assert mid_of(None, None) is None

    def test_real_fixture_shape(self) -> None:
        """对齐真实样本：buy 降序 / sell 升序，bid=128 ask=197。"""
        levels = [
            _lv("buy", 128, 56),
            _lv("buy", 125, 9),
            _lv("sell", 197, 2),
            _lv("sell", 198, 30),
        ]
        bid, ask = best_bid_ask(levels)
        assert (bid, ask) == (128, 197)
        assert spread_of(bid, ask) == 69
        assert mid_of(bid, ask) == 162


class TestVwapDepth:
    def test_vwap_is_quantity_weighted(self) -> None:
        levels = [_lv("buy", 100, 10), _lv("buy", 200, 10)]
        # (100*10 + 200*10) / 20 = 150
        assert vwap(levels) == pytest.approx(150.0)

    def test_vwap_zero_quantity_is_none(self) -> None:
        """总量为 0 → None，不抛 ZeroDivisionError。"""
        assert vwap([_lv("buy", 100, 0)]) is None
        assert vwap([]) is None

    def test_depth_sums_side(self) -> None:
        levels = [_lv("buy", 100, 10), _lv("buy", 200, 5), _lv("sell", 300, 7)]
        assert depth(levels, "buy") == 15
        assert depth(levels, "sell") == 7


class TestAggregate:
    def test_aggregate_levels_fills_all_fields(self) -> None:
        levels = [
            _lv("buy", 128, 56),
            _lv("buy", 125, 9),
            _lv("sell", 197, 2),
            _lv("sell", 198, 30),
        ]
        agg = aggregate_levels(24, _TS, levels)
        assert agg.best_bid == 128 and agg.best_ask == 197
        assert agg.spread == 69
        assert agg.mid == 162
        assert agg.vwap_buy == pytest.approx((128 * 56 + 125 * 9) / 65)
        assert agg.vwap_sell == pytest.approx((197 * 2 + 198 * 30) / 32)
        assert agg.depth_buy == 65 and agg.depth_sell == 32
        assert agg.samples == 4


class TestBucketStart:
    def test_hour_bucket(self) -> None:
        assert bucket_start(_TS) == datetime(2026, 1, 2, 3, 0, 0, tzinfo=timezone.utc)

    def test_day_bucket(self) -> None:
        assert bucket_start(_TS, "day") == datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

    def test_minute_bucket(self) -> None:
        assert bucket_start(_TS, "minute") == datetime(2026, 1, 2, 3, 45, 0, tzinfo=timezone.utc)

    def test_unknown_unit_raises(self) -> None:
        with pytest.raises(ValueError):
            bucket_start(_TS, "week")


class TestOhlc:
    def test_fold_ohlc(self) -> None:
        bar = fold_ohlc([100, 130, 90, 110])
        assert bar.open == 100
        assert bar.high == 130
        assert bar.low == 90
        assert bar.close == 110  # close 跟随最新值
        assert bar.avg == pytest.approx((100 + 130 + 90 + 110) / 4)
        assert bar.samples == 4

    def test_none_values_are_skipped(self) -> None:
        """None 是合法缺失态（无挂单），不得被当作 0 计入 high/low。"""
        bar = fold_ohlc([None, 100, None, 120])
        assert bar.open == 100
        assert bar.low == 100
        assert bar.samples == 2

    def test_all_none_is_empty_bar(self) -> None:
        bar = fold_ohlc([None, None])
        assert bar.open is None and bar.avg is None and bar.samples == 0

    def test_accumulator_idempotent_merge(self) -> None:
        """rollup 幂等：重跑得到同样的 high/low/close。"""
        acc = OhlcAccumulator()
        for v in (100, 130, 90):
            acc.add(v)
        first = acc.bar
        acc2 = OhlcAccumulator()
        acc2.merge(first)
        for v in (100, 130, 90):
            acc2.add(v)
        assert acc2.bar.high == first.high
        assert acc2.bar.low == first.low
        assert acc2.bar.close == first.close
        assert acc2.bar.open == first.open


class TestPctChange:
    def test_normal(self) -> None:
        assert pct_change(200.0, 250.0) == pytest.approx(25.0)
        assert pct_change(200.0, 150.0) == pytest.approx(-25.0)

    def test_zero_or_none_prev_is_none(self) -> None:
        """prev=0/None → None，不返回 inf（下游拿 None 就是不告警）。"""
        assert pct_change(0.0, 100.0) is None
        assert pct_change(None, 100.0) is None
        assert pct_change(100.0, None) is None


class TestTickValue:
    def test_metric_table(self) -> None:
        from market_watch.domain.models import PriceTick

        tick = PriceTick(
            source_id="gw2_prices",
            item_id=24,
            observed_at=_TS,
            cycle_id="c1",
            buy_unit_price=128,
            buy_quantity=108640,
            sell_unit_price=200,
            sell_quantity=282033,
            whitelisted=False,
        )
        assert tick_value(tick, "sell_unit_price") == 200.0
        assert tick_value(tick, "buy_unit_price") == 128.0
        assert tick_value(tick, "sell_quantity") == 282033.0
        assert tick_value(tick, "nonexistent") is None

    def test_max_jump_ratio_is_ten(self) -> None:
        assert MAX_JUMP_RATIO == 10.0
