# tests/unit/test_consistency.py
"""★ 取代「多尺度匹配」的一致性校验：多源冗余 + 数据一致性（§11.1 附加说明）。

重点断言：
- **只产出违规，不做丢弃决策**（丢弃由调用方决定）。
- 缺失（None）是合法态，不得误判成 0。
- 跳变只标记 suspicious，不丢弃。
- C8 命中 PII 的字段是 critical。
"""

from __future__ import annotations

from datetime import datetime, timezone

from market_watch.domain.consistency import (
    MAX_JUMP_RATIO,
    PRICE_CROSS_TOLERANCE,
    SUSPICIOUS,
    UNIVERSE_DRIFT_LIMIT,
    check_cross_source,
    check_jump,
    check_level_ordering,
    check_listing_level,
    check_pii_field,
    check_price_range,
    check_spread,
    check_universe_size,
    validate_levels,
    validate_tick,
)
from market_watch.domain.models import ListingLevel, PriceTick

_TS = datetime(2026, 1, 2, 3, 0, 0, tzinfo=timezone.utc)


def _tick(buy: int | None = 128, sell: int | None = 200, **kw) -> PriceTick:  # noqa: ANN003
    base = dict(
        source_id="gw2_prices",
        item_id=24,
        observed_at=_TS,
        cycle_id="c1",
        buy_unit_price=buy,
        buy_quantity=108640,
        sell_unit_price=sell,
        sell_quantity=282033,
        whitelisted=False,
    )
    base.update(kw)
    return PriceTick(**base)  # type: ignore[arg-type]


def _lv(side: str, price: int, qty: int = 10, listings: int = 1) -> ListingLevel:
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


class TestContractShape:
    def test_module_exposes_expected_constants(self) -> None:
        assert PRICE_CROSS_TOLERANCE == 1
        assert MAX_JUMP_RATIO == 10.0
        assert UNIVERSE_DRIFT_LIMIT == 0.05
        assert SUSPICIOUS == "QUARANTINED"


class TestPriceRange:
    def test_none_is_legal_absence(self) -> None:
        """★ 官方对无挂单物品返回空对象；None 是合法态，不是「价格归零」。"""
        assert check_price_range(24, "sell_unit_price", None) == []

    def test_valid_price_passes(self) -> None:
        assert check_price_range(24, "sell_unit_price", 200) == []

    def test_non_positive_is_critical(self) -> None:
        for bad in (0, -1):
            hits = check_price_range(24, "sell_unit_price", bad)
            assert len(hits) == 1 and hits[0].severity == "critical"

    def test_above_max_price_is_critical(self) -> None:
        hits = check_price_range(24, "sell_unit_price", 10**9 + 1)
        assert hits and hits[0].rule == "price.upper_bound"

    def test_non_integer_is_critical(self) -> None:
        hits = check_price_range(24, "sell_unit_price", 12.5)  # type: ignore[arg-type]
        assert hits and hits[0].rule == "price.is_integer"


class TestListingLevel:
    def test_valid_level(self) -> None:
        assert check_listing_level(_lv("buy", 128, 56)) == []

    def test_zero_listings_flagged(self) -> None:
        hits = check_listing_level(_lv("buy", 128, 56, listings=0))
        assert any(h.rule == "listings.min" for h in hits)

    def test_negative_quantity_flagged(self) -> None:
        hits = check_listing_level(_lv("sell", 197, -5))
        assert any(h.rule == "quantity.non_negative" for h in hits)


class TestOrdering:
    def test_buy_descending_ok(self) -> None:
        assert check_level_ordering(24, [_lv("buy", 128), _lv("buy", 125), _lv("buy", 120)]) == []

    def test_sell_ascending_ok(self) -> None:
        assert check_level_ordering(24, [_lv("sell", 197), _lv("sell", 198)]) == []

    def test_buy_ascending_is_violation(self) -> None:
        hits = check_level_ordering(24, [_lv("buy", 120), _lv("buy", 128)])
        assert hits and hits[0].rule == "levels.ordering"

    def test_sell_descending_is_violation(self) -> None:
        hits = check_level_ordering(24, [_lv("sell", 198), _lv("sell", 197)])
        assert hits and hits[0].rule == "levels.ordering"

    def test_single_level_cannot_be_ordered(self) -> None:
        assert check_level_ordering(24, [_lv("buy", 128)]) == []


class TestSpread:
    def test_positive_spread_ok(self) -> None:
        assert check_spread(128, 200, 24) == []

    def test_zero_spread_allowed(self) -> None:
        """允许 zero-spread（bid == ask），只禁止负 spread。"""
        assert check_spread(200, 200, 24) == []

    def test_negative_spread_flagged(self) -> None:
        hits = check_spread(200, 128, 24)
        assert hits and hits[0].rule == "spread.non_negative"

    def test_missing_side_skipped(self) -> None:
        assert check_spread(None, 200, 24) == []


class TestCrossSource:
    def test_match_within_one_tick(self) -> None:
        """★ 多源冗余：prices 与 listings 互相印证，±1 tick 容差。"""
        assert check_cross_source(_tick(sell=200), best_bid=None, best_ask=200) == []
        assert check_cross_source(_tick(sell=200), best_bid=None, best_ask=201) == []

    def test_mismatch_flagged(self) -> None:
        hits = check_cross_source(_tick(sell=200), best_bid=None, best_ask=250)
        assert hits and hits[0].rule == "cross_source.price_mismatch"

    def test_one_side_missing_skipped(self) -> None:
        """一侧缺失无法印证，不算违规（不得把「没抓到」判成不一致）。"""
        assert check_cross_source(_tick(sell=None), best_bid=None, best_ask=200) == []
        assert check_cross_source(_tick(sell=200), best_bid=None, best_ask=None) == []


class TestJump:
    def test_within_ratio_ok(self) -> None:
        assert check_jump(24, "sell_unit_price", 100.0, 900.0) == []

    def test_beyond_ratio_marked_suspicious(self) -> None:
        hits = check_jump(24, "sell_unit_price", 100.0, 2000.0)
        assert hits and hits[0].rule == "price.jump_suspicious"
        # 只标记，severity 是 warn 而非 critical —— 不丢弃
        assert hits[0].severity == "warn"

    def test_missing_history_skipped(self) -> None:
        assert check_jump(24, "sell_unit_price", None, 200.0) == []


class TestUniverseDrift:
    def test_within_five_percent_ok(self) -> None:
        assert check_universe_size(27997, 29000) == []

    def test_beyond_five_percent_flagged(self) -> None:
        hits = check_universe_size(27997, 20000)
        assert hits and hits[0].rule == "universe.drift"

    def test_no_baseline_skipped(self) -> None:
        assert check_universe_size(None, 27997) == []


class TestPii:
    def test_hits_are_critical(self) -> None:
        hits = check_pii_field(24, "description", ["email"])
        assert len(hits) == 1 and hits[0].severity == "critical"

    def test_no_hits_empty(self) -> None:
        assert check_pii_field(24, "name", []) == []


class TestComposite:
    def test_validate_tick_clean(self) -> None:
        assert validate_tick(_tick()) == []

    def test_validate_tick_negative_spread(self) -> None:
        hits = validate_tick(_tick(buy=300, sell=200))
        assert any(h.rule == "spread.non_negative" for h in hits)

    def test_validate_tick_with_prev_and_cross(self) -> None:
        hits = validate_tick(
            _tick(buy=128, sell=200),
            prev={"sell_unit_price": 199.0, "buy_unit_price": 127.0},
            best_bid=128,
            best_ask=200,
        )
        assert hits == []

    def test_validate_levels_cross_source(self) -> None:
        levels = [
            _lv("buy", 128, 56),
            _lv("buy", 125, 9),
            _lv("sell", 197, 2),
            _lv("sell", 198, 30),
        ]
        # tick 的 sell=197 与 listings 最低卖价 197 一致 → 无违规
        assert validate_levels(levels, tick=_tick(sell=197, buy=128)) == []

    def test_validate_levels_empty(self) -> None:
        assert validate_levels([]) == []
