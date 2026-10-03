# tests/unit/test_anomaly.py
"""异动判定 + 冷却去重（§6.1 (4)、§11.1 (3)）。

关键断言：
- **被隔离（QUARANTINED）的快照不进异动检测**——否则「本轮没抓到」会被判成暴跌。
- 首轮无历史（prev=None）不告警，避免启动风暴。
- 跳变超 MAX_JUMP_RATIO 交给一致性层，不在异动层刷屏。
- 冷却桶内重复告警被抑制。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from market_watch.domain.anomaly import (
    DEFAULT_RULES,
    Cooldown,
    Rule,
    cooldown_bucket,
    dedupe_key,
    detect,
    is_quarantined,
    zscore,
)
from market_watch.domain.models import PriceTick

_TS = datetime(2026, 1, 2, 3, 0, 0, tzinfo=timezone.utc)


def _tick(sell: int = 200, buy: int = 128, quality: str = "COMPLETE") -> PriceTick:
    return PriceTick(
        source_id="gw2_prices",
        item_id=24,
        observed_at=_TS,
        cycle_id="c1",
        buy_unit_price=buy,
        buy_quantity=108640,
        sell_unit_price=sell,
        sell_quantity=282033,
        whitelisted=False,
        quality=quality,  # type: ignore[arg-type]
    )


class TestQuarantine:
    def test_quarantined_snapshot_is_skipped(self) -> None:
        tick = _tick(sell=1, quality="QUARANTINED")
        assert is_quarantined(tick.quality)
        out = detect(
            tick,
            DEFAULT_RULES,
            prev={"sell_unit_price": 200.0},
            now=_TS,
        )
        assert out == []  # ★ 暴跌 99% 也不告警

    def test_normal_quality_is_checked(self) -> None:
        assert not is_quarantined("COMPLETE")
        assert not is_quarantined("PARTIAL")


class TestPctRules:
    def test_drop_triggers(self) -> None:
        rule = Rule("drop", "sell_unit_price", "pct", -15.0)
        out = detect(_tick(sell=100), [rule], prev={"sell_unit_price": 200.0}, now=_TS)
        assert len(out) == 1
        fired, prev_value, delta_pct, message, until = out[0]
        assert fired.rule_id == "drop"
        assert prev_value == 200.0
        assert delta_pct == pytest.approx(-50.0)
        assert "下跌" in message

    def test_small_move_does_not_trigger(self) -> None:
        rule = Rule("drop", "sell_unit_price", "pct", -15.0)
        out = detect(_tick(sell=190), [rule], prev={"sell_unit_price": 200.0}, now=_TS)
        assert out == []

    def test_spike_triggers(self) -> None:
        rule = Rule("spike", "sell_unit_price", "pct", 25.0)
        out = detect(_tick(sell=300), [rule], prev={"sell_unit_price": 200.0}, now=_TS)
        assert len(out) == 1 and out[0][2] == pytest.approx(50.0)
        assert "上涨" in out[0][3]

    def test_no_prev_means_no_alert(self) -> None:
        """首轮无历史不告警（避免启动风暴）。"""
        rule = Rule("drop", "sell_unit_price", "pct", -15.0)
        assert detect(_tick(sell=1), [rule], prev={}, now=_TS) == []

    def test_jump_beyond_max_ratio_not_alerted(self) -> None:
        """超 MAX_JUMP_RATIO 的脏数据不刷屏，交给一致性层标记。"""
        rule = Rule("drop", "sell_unit_price", "pct", -15.0)
        out = detect(_tick(sell=1), [rule], prev={"sell_unit_price": 100000.0}, now=_TS)
        assert out == []

    def test_disabled_rule_skipped(self) -> None:
        rule = Rule("drop", "sell_unit_price", "pct", -15.0, enabled=False)
        assert detect(_tick(sell=100), [rule], prev={"sell_unit_price": 200.0}, now=_TS) == []


class TestZscoreAndAbs:
    def test_zscore_triggers_on_outlier(self) -> None:
        rule = Rule("z", "sell_unit_price", "zscore", 2.0, window=20)
        hist = [200.0, 201.0, 199.0, 200.0, 202.0, 198.0]
        out = detect(_tick(sell=900), [rule], history={"sell_unit_price": hist}, now=_TS)
        assert len(out) == 1 and "σ" in out[0][3]

    def test_zscore_flat_history_is_none(self) -> None:
        """总体标准差为 0 → None，不返回 inf。"""
        assert zscore(200.0, [200.0, 200.0, 200.0]) is None
        assert zscore(200.0, [200.0]) is None

    def test_abs_threshold(self) -> None:
        rule = Rule("wide", "sell_unit_price", "abs", 10000.0, severity="info")
        assert detect(_tick(sell=200), [rule], now=_TS) == []
        assert len(detect(_tick(sell=20000), [rule], now=_TS)) == 1

    def test_missing_metric_skipped(self) -> None:
        rule = Rule("wide", "nonexistent_metric", "abs", 1.0)
        assert detect(_tick(), [rule], now=_TS) == []


class TestCooldown:
    def test_same_bucket_suppressed(self) -> None:
        rule = Rule("drop", "sell_unit_price", "pct", -15.0, cooldown_seconds=3600)
        cd = Cooldown()
        first = detect(
            _tick(sell=100), [rule], prev={"sell_unit_price": 200.0}, now=_TS, cooldown=cd
        )
        assert len(first) == 1
        second = detect(
            _tick(sell=90), [rule], prev={"sell_unit_price": 200.0}, now=_TS, cooldown=cd
        )
        assert second == []  # 同桶内抑制

    def test_later_bucket_fires_again(self) -> None:
        rule = Rule("drop", "sell_unit_price", "pct", -15.0, cooldown_seconds=3600)
        cd = Cooldown()
        assert (
            len(
                detect(
                    _tick(sell=100), [rule], prev={"sell_unit_price": 200.0}, now=_TS, cooldown=cd
                )
            )
            == 1
        )
        later = _TS + timedelta(seconds=7200)
        assert (
            len(
                detect(
                    _tick(sell=100), [rule], prev={"sell_unit_price": 200.0}, now=later, cooldown=cd
                )
            )
            == 1
        )

    def test_cooldown_entry_expires(self) -> None:
        """登记 / 未过期抑制 / 已过期放行 三段。

        必须用 `now=` 显式推进业务时钟：两次 `suppress` 传同一个 `until` 时，
        第二次的 `deadline > now` 恒为 False，用例会自相矛盾。
        """
        cd = Cooldown(sweep_interval_s=0.0, clock=lambda: _TS)
        now = _TS
        assert cd.suppress("k", now + timedelta(seconds=10), now=now) is False  # 首次：登记
        assert (
            cd.suppress("k", now + timedelta(seconds=10), now=now + timedelta(seconds=5)) is True
        )  # 未过期 → 抑制
        # 已过期（now=+20 已越过登记时的 deadline +10）→ 放行，并把冷却重新登记到 +20
        assert (
            cd.suppress("k", now + timedelta(seconds=20), now=now + timedelta(seconds=20)) is False
        )

    def test_dedupe_key_is_stable_and_distinct(self) -> None:
        a = dedupe_key("gw2_prices", "drop", 24, 100)
        b = dedupe_key("gw2_prices", "drop", 24, 100)
        c = dedupe_key("gw2_prices", "drop", 25, 100)
        assert a == b and a != c
        assert len(a) == 40  # sha1 hex

    def test_cooldown_bucket_floors_at_one(self) -> None:
        assert cooldown_bucket(_TS, 0) == int(_TS.timestamp())
        assert cooldown_bucket(_TS, 3600) == int(_TS.timestamp()) // 3600
