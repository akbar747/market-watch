# tests/unit/test_breaker.py
"""熔断状态机 closed / open / half-open（§5.3）。时钟注入，全程不真等。"""

from __future__ import annotations

from market_watch.domain.errors import ErrorClass, OpenCircuit
from market_watch.infrastructure.http.breaker import (
    MAX_OPEN_DURATION_S,
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    BreakerConfig,
    CircuitBreaker,
)

_HOST = "api.guildwars2.com"


def _clock(start: float = 1000.0):  # noqa: ANN201
    class _C:
        __slots__ = ("now",)

        def __init__(self) -> None:
            self.now = start

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds

    return _C()


def _breaker(**kw) -> tuple[CircuitBreaker, object]:  # noqa: ANN003
    clock = _clock()
    cfg = BreakerConfig(**kw) if kw else BreakerConfig()
    return CircuitBreaker(cfg, clock=clock), clock


class TestClosedState:
    def test_starts_closed(self) -> None:
        br, _ = _breaker()
        assert br.state(_HOST) == STATE_CLOSED
        assert br.before(_HOST) is True

    def test_successes_keep_closed(self) -> None:
        br, _ = _breaker()
        for _ in range(50):
            br.record_success(_HOST)
        assert br.state(_HOST) == STATE_CLOSED


class TestOpening:
    def test_consecutive_5xx_opens_without_ratio(self) -> None:
        """★ 硬信号：连续 5xx ≥ 8 不等失败率直接开闸（§6 #5）。

        用例价值 = **在比率明显不达标的压力下仍开闸**，因此必须先灌足够多成功把
        total 拉过 min_samples，使 ratio 远低于 threshold，再连灌到 8 个 5xx。
        掺成功时不要用 (5xx, success) 交替——`record_success` 会重置
        `consecutive_5xx`，交替写永远攒不满连续数。
        """
        br, _ = _breaker(consecutive_5xx=8, min_samples=20)
        for _ in range(100):
            br.record_success(_HOST)  # total 拉到 100 ≥ min_samples
        for _ in range(7):
            br.record(_HOST, ErrorClass.SERVER_5XX)
        # 自证隔离干净：比率不够开闸，只能由硬信号触发
        detail = br.detail(_HOST)
        assert detail["failure_ratio"] < 0.5, detail
        assert detail["consecutive_5xx"] == 7
        assert br.state(_HOST) == STATE_CLOSED
        br.record(_HOST, ErrorClass.SERVER_5XX)  # 第 8 个 → 硬信号开闸
        assert br.state(_HOST) == STATE_OPEN

    def test_failure_ratio_opens(self) -> None:
        br, _ = _breaker(min_samples=20, failure_ratio=0.5)
        for _ in range(20):
            br.record(_HOST, ErrorClass.DNS_FAILURE)
        assert br.state(_HOST) == STATE_OPEN

    def test_ratio_below_threshold_stays_closed(self) -> None:
        br, _ = _breaker(min_samples=20, failure_ratio=0.5)
        for _ in range(20):
            br.record(_HOST, ErrorClass.DNS_FAILURE)
            br.record(_HOST, ErrorClass.DNS_FAILURE)
            br.record_success(_HOST)
            br.record_success(_HOST)
            br.record_success(_HOST)
        detail = br.detail(_HOST)
        assert detail["failure_ratio"] < 0.5, detail  # 0.4
        assert br.state(_HOST) == STATE_CLOSED

    def test_vendor_rate_limit_counts_as_5xx_signal(self) -> None:
        br, _ = _breaker(consecutive_5xx=3)
        for _ in range(3):
            br.record(_HOST, ErrorClass.RATE_LIMITED_VENDOR)
        assert br.state(_HOST) == STATE_OPEN


class TestOpenState:
    def test_open_short_circuits(self) -> None:
        br, _ = _breaker(consecutive_5xx=2)
        br.record(_HOST, ErrorClass.SERVER_5XX)
        br.record(_HOST, ErrorClass.SERVER_5XX)
        try:
            br.before(_HOST)
        except OpenCircuit as exc:
            assert exc.until_s > 0
        else:  # pragma: no cover
            raise AssertionError("open 状态必须抛 OpenCircuit")

    def test_open_transitions_to_half_open_after_duration(self) -> None:
        br, clock = _breaker(consecutive_5xx=2, open_duration_s=60.0)
        br.record(_HOST, ErrorClass.SERVER_5XX)
        br.record(_HOST, ErrorClass.SERVER_5XX)
        assert br.state(_HOST) == STATE_OPEN
        clock.advance(61.0)  # type: ignore[attr-defined]
        assert br.state(_HOST) == STATE_HALF_OPEN


class TestHalfOpen:
    def test_half_open_allows_limited_probes(self) -> None:
        br, clock = _breaker(consecutive_5xx=2, open_duration_s=60.0, half_open_probes=3)
        br.record(_HOST, ErrorClass.SERVER_5XX)
        br.record(_HOST, ErrorClass.SERVER_5XX)
        clock.advance(61.0)  # type: ignore[attr-defined]
        for _ in range(3):
            assert br.before(_HOST) is True
        try:
            br.before(_HOST)  # 第 4 个探针
        except OpenCircuit:
            pass
        else:  # pragma: no cover
            raise AssertionError("探针用尽后必须拒绝")

    def test_probe_successes_close_circuit(self) -> None:
        br, clock = _breaker(consecutive_5xx=2, open_duration_s=60.0, half_open_success_to_close=3)
        br.record(_HOST, ErrorClass.SERVER_5XX)
        br.record(_HOST, ErrorClass.SERVER_5XX)
        clock.advance(61.0)  # type: ignore[attr-defined]
        for _ in range(3):
            br.record_success(_HOST)
        assert br.state(_HOST) == STATE_CLOSED
        # 关闭后窗口清空，失败率不再残留
        assert br.detail(_HOST)["failure_ratio"] == 0.0

    def test_probe_failure_doubles_open_duration(self) -> None:
        br, clock = _breaker(consecutive_5xx=2, open_duration_s=60.0)
        br.record(_HOST, ErrorClass.SERVER_5XX)
        br.record(_HOST, ErrorClass.SERVER_5XX)
        clock.advance(61.0)  # type: ignore[attr-defined]
        assert br.state(_HOST) == STATE_HALF_OPEN
        br.record(_HOST, ErrorClass.SERVER_5XX)  # 探针失败
        assert br.state(_HOST) == STATE_OPEN
        assert br.detail(_HOST)["open_duration_s"] == 120.0

    def test_open_duration_caps_at_max(self) -> None:
        br, clock = _breaker(consecutive_5xx=2, open_duration_s=400.0)
        for _ in range(6):
            br.record(_HOST, ErrorClass.SERVER_5XX)
            br.record(_HOST, ErrorClass.SERVER_5XX)
            clock.advance(1000.0)  # type: ignore[attr-defined]
            br.record(_HOST, ErrorClass.SERVER_5XX)  # half_open 探针失败
        assert br.detail(_HOST)["open_duration_s"] <= MAX_OPEN_DURATION_S


class TestIgnoredErrors:
    def test_cancelled_not_counted(self) -> None:
        """§6 #17：取消不计失败。"""
        br, _ = _breaker(min_samples=1, failure_ratio=0.5)
        for _ in range(50):
            br.record(_HOST, ErrorClass.CANCELLED)
        assert br.state(_HOST) == STATE_CLOSED
        assert br.detail(_HOST)["failures"] == 0

    def test_dedupe_conflict_not_counted(self) -> None:
        br, _ = _breaker()
        for _ in range(50):
            br.record(_HOST, ErrorClass.DEDUPE_CONFLICT)
        assert br.detail(_HOST)["failures"] == 0

    def test_ok_resets_consecutive_5xx(self) -> None:
        br, _ = _breaker(consecutive_5xx=3)
        br.record(_HOST, ErrorClass.SERVER_5XX)
        br.record(_HOST, ErrorClass.SERVER_5XX)
        br.record(_HOST, ErrorClass.OK)
        assert br.detail(_HOST)["consecutive_5xx"] == 0


class TestPerHostIsolation:
    def test_one_host_open_does_not_affect_other(self) -> None:
        br, _ = _breaker(consecutive_5xx=2)
        br.record("a.example", ErrorClass.SERVER_5XX)
        br.record("a.example", ErrorClass.SERVER_5XX)
        assert br.state("a.example") == STATE_OPEN
        assert br.state("b.example") == STATE_CLOSED
        assert br.before("b.example") is True

    def test_snapshot_covers_all_hosts(self) -> None:
        br, _ = _breaker()
        br.record_success("a.example")
        br.record_success("b.example")
        assert set(br.snapshot()) == {"a.example", "b.example"}
