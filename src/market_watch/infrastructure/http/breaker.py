# src/market_watch/infrastructure/http/breaker.py
"""每 host 熔断状态机 closed / open / half-open + 探针。

架构依据：docs/01-architecture.md §5.3（熔断参数）、§4.5（重建信号）。

判定优先级：
    「连续 5xx ≥ 8」这个硬信号可以**不等比率**直接开闸（§6 #5）；
    常规路径是窗口内失败率 ≥ 0.5 且样本 ≥ 20。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import time
from typing import Final

from ...domain.errors import ErrorClass, OpenCircuit
from ...logging_setup import get_logger

__all__ = [
    "DEFAULT_BREAKER_CONFIG",
    "STATE_CLOSED",
    "STATE_HALF_OPEN",
    "STATE_OPEN",
    "BreakerConfig",
    "CircuitBreaker",
]

_log = get_logger("market_watch.breaker")

STATE_CLOSED: Final[str] = "closed"
STATE_OPEN: Final[str] = "open"
STATE_HALF_OPEN: Final[str] = "half_open"

#: §5.3 默认值（常用稳定起点，需实测确认）
DEFAULT_WINDOW_S: Final[float] = 60.0
DEFAULT_MIN_SAMPLES: Final[int] = 20
DEFAULT_FAILURE_RATIO: Final[float] = 0.5
DEFAULT_CONSECUTIVE_5XX: Final[int] = 8
DEFAULT_OPEN_DURATION_S: Final[float] = 60.0
DEFAULT_HALF_OPEN_PROBES: Final[int] = 3
DEFAULT_HALF_OPEN_SUCCESS_TO_CLOSE: Final[int] = 3
MAX_OPEN_DURATION_S: Final[float] = 600.0


@dataclass(slots=True)
class BreakerConfig:
    window_s: float = DEFAULT_WINDOW_S
    min_samples: int = DEFAULT_MIN_SAMPLES
    failure_ratio: float = DEFAULT_FAILURE_RATIO
    consecutive_5xx: int = DEFAULT_CONSECUTIVE_5XX
    open_duration_s: float = DEFAULT_OPEN_DURATION_S
    half_open_probes: int = DEFAULT_HALF_OPEN_PROBES
    half_open_success_to_close: int = DEFAULT_HALF_OPEN_SUCCESS_TO_CLOSE


DEFAULT_BREAKER_CONFIG: Final[BreakerConfig] = BreakerConfig()


@dataclass(slots=True)
class _HostState:
    failures: deque[float] = field(default_factory=deque)  # 失败时间戳滚动窗口
    successes: deque[float] = field(default_factory=deque)
    consecutive_5xx: int = 0
    state: str = STATE_CLOSED
    opened_at: float = 0.0
    open_duration_s: float = DEFAULT_OPEN_DURATION_S
    probes_used: int = 0
    probe_successes: int = 0


class CircuitBreaker:
    """实现 `domain.ports.Breaker`。时钟可注入（§9.1：「注入假时钟」）。"""

    __slots__ = ("_cfg", "_clock", "_hosts")

    def __init__(
        self, cfg: BreakerConfig | None = None, clock: Callable[[], float] | None = None
    ) -> None:
        """构造熔断器。

        Args:
            cfg: 参数包；缺省 `DEFAULT_BREAKER_CONFIG`。
            clock: 可注入的单调时钟（§9.1）。默认 `time.monotonic`。
                **测试必须注入假时钟**，否则「等 60s 再半开」的用例不可能跑。
        """
        self._cfg = cfg or DEFAULT_BREAKER_CONFIG
        self._hosts: dict[str, _HostState] = {}
        self._clock = clock or time.monotonic

    def _now(self) -> float:
        return float(self._clock())

    def _state(self, host: str) -> _HostState:
        st = self._hosts.get(host)
        if st is None:
            st = _HostState(open_duration_s=self._cfg.open_duration_s)
            self._hosts[host] = st
        return st

    # ---- 查询 ----
    def state(self, host: str) -> str:
        """查/推进状态：open 到期会自动迁移到 half_open（并重置探针计数）。

        ★ **所有改状态的方法（`record` / `record_success`）的第一行都必须调
        `self.state(host)`**，再读 `st.state`。这是 Phase 2 修过的一个真 bug：
        少了这一步，open 到期的 host 在没人显式调过 `state()` 之前，一直读到 OPEN，
        探针成功/失败分支都走不到，电路只能等进程重启。

        Args:
            host: 目标主机。

        Returns:
            `'closed'` / `'open'` / `'half_open'`。
        """
        st = self._state(host)
        if st.state == STATE_OPEN and self._now() - st.opened_at >= st.open_duration_s:
            st.state = STATE_HALF_OPEN
            st.probes_used = 0
            st.probe_successes = 0
            _log.info("breaker.half_open host=%s", host)
        return st.state

    def before(self, host: str) -> bool:
        """是否允许发起请求。open 直接抛 `OpenCircuit`；half_open 放行 ≤ probes 个。"""
        st = self._state(host)
        current = self.state(host)
        if current == STATE_OPEN:
            remaining = st.open_duration_s - (self._now() - st.opened_at)
            raise OpenCircuit(host=host, until_s=max(0.0, remaining))
        if current == STATE_HALF_OPEN:
            if st.probes_used >= self._cfg.half_open_probes:
                remaining = 0.0
                raise OpenCircuit(host=host, until_s=remaining)
            st.probes_used += 1
        return True

    # ---- 记录 ----
    def record(self, host: str, err: ErrorClass) -> None:
        """记录一次失败（含 5xx / 限流 / 解析失败等），可能触发开闸或探针失败回退。

        Args:
            host: 目标主机。
            err: 错误分类。分类的唯一来源是 `classification.classify_*`，
                此处不自己判断 `status_code == 429` 之类。
        """
        # ★ 第一行必须先触发时间驱动的 OPEN→HALF_OPEN 迁移，再读 st.state。
        # 否则处于 open 且 open_duration_s 已到点的 host，在没人显式调过 state()
        # 之前，本方法读到的一直是 OPEN：既走不到「探针失败」分支（下面第 136 行），
        # record_success 也走不到「探针成功」分支 —— 电路永远关不上，只能等进程重启。
        self.state(host)
        st = self._state(host)
        now = self._now()
        self._trim(st, now)

        if err in (ErrorClass.OK, ErrorClass.NOT_MODIFIED):
            self.record_success(host)
            return
        if err == ErrorClass.CANCELLED:
            return  # 取消不计失败（§6 #17）
        if err == ErrorClass.DEDUPE_CONFLICT:
            return

        st.failures.append(now)
        if err == ErrorClass.SERVER_5XX or err == ErrorClass.RATE_LIMITED_VENDOR:
            st.consecutive_5xx += 1
        else:
            st.consecutive_5xx = 0

        if st.state == STATE_HALF_OPEN:
            # 探针失败 → 回 open 并 OPEN *= 2（≤600s）
            st.open_duration_s = min(MAX_OPEN_DURATION_S, st.open_duration_s * 2)
            self._open(st, host, reason="probe_failed")
            return

        # 硬信号：连续 5xx 达阈值，不等比率
        if st.consecutive_5xx >= self._cfg.consecutive_5xx:
            self._open(st, host, reason=f"consecutive_5xx={st.consecutive_5xx}")
            return

        total = len(st.failures) + len(st.successes)
        if total >= self._cfg.min_samples:
            ratio = len(st.failures) / total
            if ratio >= self._cfg.failure_ratio:
                self._open(st, host, reason=f"failure_ratio={ratio:.2f}")

    def record_success(self, host: str) -> None:
        """记录一次成功；半开状态下累计够 `half_open_success_to_close` 即闭合。

        ★ 同 `record()`：第一行必须先调 `self.state(host)` 让时间驱动的迁移发生，
        再读 `st.state`，否则探针计数被整个吞掉（详见 `state()` 的说明）。

        Args:
            host: 目标主机。
        """
        # ★ 同 record()：先让时间驱动的迁移发生，再读 st.state，否则探针计数被整个吞掉。
        self.state(host)
        st = self._state(host)
        now = self._now()
        self._trim(st, now)
        st.successes.append(now)
        st.consecutive_5xx = 0
        if st.state == STATE_HALF_OPEN:
            st.probe_successes += 1
            if st.probe_successes >= self._cfg.half_open_success_to_close:
                st.state = STATE_CLOSED
                st.open_duration_s = self._cfg.open_duration_s  # 复位
                st.failures.clear()
                st.successes.clear()
                _log.info("breaker.closed host=%s", host)

    def _open(self, st: _HostState, host: str, *, reason: str) -> None:
        st.state = STATE_OPEN
        st.opened_at = self._now()
        _log.error(
            "breaker.opened host=%s reason=%s open_for=%.0fs", host, reason, st.open_duration_s
        )

    def _trim(self, st: _HostState, now: float) -> None:
        window = self._cfg.window_s
        while st.failures and now - st.failures[0] > window:
            st.failures.popleft()
        while st.successes and now - st.successes[0] > window:
            st.successes.popleft()

    def snapshot(self) -> dict[str, str]:
        """全部已出现过的 host 的 `{host: state}`（看板 `/api/health` 用）。"""
        return {host: self.state(host) for host in self._hosts}

    def detail(self, host: str) -> dict[str, object]:
        """取单 host 的熔断明细（失败数、失败率、连续 5xx、当前开闸时长）。"""
        st = self._state(host)
        total = len(st.failures) + len(st.successes)
        return {
            "state": self.state(host),
            "failures": len(st.failures),
            "successes": len(st.successes),
            "failure_ratio": (len(st.failures) / total) if total else 0.0,
            "consecutive_5xx": st.consecutive_5xx,
            "open_duration_s": st.open_duration_s,
        }
