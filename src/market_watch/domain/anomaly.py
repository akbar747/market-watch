# src/market_watch/domain/anomaly.py
"""异动判定：环比 / z-score / 绝对阈值 + 冷却去重。纯函数，零 IO。

架构依据：docs/01-architecture.md §6.1（4）、§11.1（3）。

关键约束：
- **被隔离（`QUARANTINED`）的快照不得进入异动检测**——否则「本轮没抓到」会被误判成
  「价格归零/暴跌」（§6.1）。
- 环比跳变超 `MAX_JUMP_RATIO` 的记录只标记 suspicious，不产生告警（由一致性规则管）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import math
from typing import Final, Literal

from .models import PriceTick, utc_now
from .pricing import MAX_JUMP_RATIO, pct_change, tick_value

__all__ = [
    "DEFAULT_RULES",
    "Cooldown",
    "Rule",
    "dedupe_key",
    "detect",
    "is_quarantined",
    "zscore",
]

Severity = Literal["info", "warn", "critical"]


@dataclass(frozen=True, slots=True)
class Rule:
    """一条异动规则（对应 config/alerts.yaml 的一个条目）。"""

    rule_id: str
    metric: str  # 'sell_unit_price' | 'buy_unit_price' | 'spread' | 'depth_sell' | ...
    kind: Literal["pct", "zscore", "abs"]  # 环比 / 标准差 / 绝对阈值
    threshold: float
    window: int = 0  # zscore 需要的历史样本数
    cooldown_seconds: int = 3600
    severity: Severity = "warn"
    enabled: bool = True

    def cooldown(self) -> timedelta:
        """本规则的冷却窗口（`timedelta`）。"""
        return timedelta(seconds=self.cooldown_seconds)


#: 兜底默认规则（config/alerts.yaml 存在时以其为准）
DEFAULT_RULES: Final[tuple[Rule, ...]] = (
    Rule(
        "price_drop_15pct", "sell_unit_price", "pct", -15.0, cooldown_seconds=3600, severity="warn"
    ),
    Rule(
        "price_spike_25pct", "sell_unit_price", "pct", 25.0, cooldown_seconds=3600, severity="warn"
    ),
    Rule("spread_wide", "spread", "abs", 10000.0, cooldown_seconds=7200, severity="info"),
)


class Cooldown:
    """进程内冷却门：`dedupe_key -> cooldown_until`。命中即抑制重复告警。

    容量无界会随 item 数增长，因此按 `cooldown_until` 做惰性清扫：每次 `check` 时
    顺手丢弃已过期条目（O(1) 摊还），避免后台定时任务。
    """

    __slots__ = ("_clock", "_sweep_interval_s", "_swept_at", "_until")

    def __init__(
        self, sweep_interval_s: float = 60.0, clock: Callable[[], datetime] | None = None
    ) -> None:
        """构造冷却门。

        Args:
            sweep_interval_s: 惰性清扫间隔（秒）。到期条目在下次 `suppress` 时被
                顺手丢弃，从而无需后台定时任务。
            clock: 可注入的时钟（§9.1「注入假时钟」），与 `CircuitBreaker` /
                `TokenBucket` 同款设计。默认仍是 `utc_now`，生产行为不变。

        Raises:
            ValueError: 不抛。`sweep_interval_s <= 0` 时按 0 处理（等价于每次都扫）。
        """
        self._until: dict[str, datetime] = {}
        self._swept_at: float = 0.0
        self._sweep_interval_s = sweep_interval_s
        self._clock = clock or utc_now

    def suppress(self, key: str, until: datetime, now: datetime | None = None) -> bool:
        """True = 处于冷却期，应抑制。

        抑制期内返回 `True`，同时**刷新**该 key 的到期时间；未命中冷却时登记
        `until` 并返回 `False`。

        Args:
            key: 去重键（`dedupe_key()` 的产出）。
            until: 本次告警的冷却到期时刻（通常 `now + rule.cooldown()`）。
            now: 当前时刻；缺省取注入的时钟。`detect()` 会把已解析出的 `now`
                传进来，这样同一次告警判定链路用的是同一个时间点（否则业务时间
                与墙钟对不上）。

        Returns:
            `True` = 处于冷却期，应抑制；`False` = 未抑制（本次触发）。
        """
        now = now or self._clock()
        self._maybe_sweep(now)
        deadline = self._until.get(key)
        if deadline is not None and deadline > now:
            return True
        self._until[key] = until
        return False

    def _maybe_sweep(self, now: datetime) -> None:
        import time

        stamp = time.monotonic()
        if stamp - self._swept_at < self._sweep_interval_s:
            return
        self._swept_at = stamp
        expired = [k for k, v in self._until.items() if v <= now]
        for k in expired:
            del self._until[k]

    def __len__(self) -> int:
        """当前登记中的冷却条目数（含已过期但尚未清扫的）。"""
        return len(self._until)


def is_quarantined(quality: str) -> bool:
    """判断快照是否被一致性规则隔离。

    Args:
        quality: `PriceTick.quality` 的值。

    Returns:
        `True` = 被隔离（`QUARANTINED`），异动检测必须跳过——否则「本轮没抓到」
        会被误判成「价格归零/暴跌」（§6.1）。
    """
    return quality == "QUARANTINED"


def zscore(value: float, history: Sequence[float]) -> float | None:
    """算标准分数（样本标准差）。

    Args:
        value: 待评估的当前值。
        history: 历史样本序列（含 `value` 之前的观测）。

    Returns:
        标准分数；样本数 < 2 或标准差为 0 时返回 `None`（无法判定，
        不返回 `inf`——`inf` 会被下游当成「永远触发」）。
    """
    n = len(history)
    if n < 2:
        return None
    mean = sum(history) / n
    var = sum((x - mean) ** 2 for x in history) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return (value - mean) / sd


def dedupe_key(source_id: str, rule_id: str, item_id: int | None, cooldown_bucket: int) -> str:
    """算告警去重键（§7.2）。

    Args:
        source_id: 源标识。
        rule_id: 规则 id（对应 `config/alerts.yaml` 的一条）。
        item_id: 商品 id；非商品类告警（如 universe 漂移）为 `None`。
        cooldown_bucket: 冷却桶号（`cooldown_bucket()` 的产出），使同一桶内
            的重复告警合并为一个 key。

    Returns:
        `sha1(source_id|rule_id|item_id|cooldown_bucket)` 的 40 位十六进制串。
    """
    raw = f"{source_id}|{rule_id}|{item_id}|{cooldown_bucket}"
    # S324：sha1 只用于告警去重键（非安全场景，无碰撞韧性需求），sha256 无必要
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()  # noqa: S324


def cooldown_bucket(now: datetime, cooldown_seconds: int) -> int:
    """把时间离散成冷却桶：同一桶内的重复告警合并。

    Args:
        now: 当前时刻（tz-aware 或 naive 均可，此处只读 `timestamp()`）。
        cooldown_seconds: 冷却秒数；`<= 0` 时按 1 秒处理（避免除零）。

    Returns:
        桶号（整数）。
    """
    return int(now.timestamp()) // max(1, cooldown_seconds)


def _triggered(
    rule: Rule, curr: float, prev: float | None, history: Sequence[float]
) -> tuple[bool, float | None]:
    """返回 (是否触发, 实际观测值)。"""
    if rule.kind == "pct":
        if prev is None:
            return False, None
        delta = pct_change(prev, curr)
        if delta is None or prev <= 0:
            return False, delta
        # 跳变超 MAX_JUMP_RATIO 的一律交给一致性规则，不在异动层告警（避免脏数据刷屏）
        ratio = max(curr, prev) / min(curr, prev) if min(curr, prev) > 0 else float("inf")
        if ratio > MAX_JUMP_RATIO:
            return False, delta
        return (delta <= rule.threshold if rule.threshold < 0 else delta >= rule.threshold), delta
    if rule.kind == "zscore":
        z = zscore(curr, history)
        if z is None:
            return False, None
        return abs(z) >= abs(rule.threshold), z
    # abs
    return curr >= rule.threshold, curr


def detect(
    tick: PriceTick,
    rules: Sequence[Rule],
    *,
    prev: Mapping[str, float] | None = None,
    history: Mapping[str, Sequence[float]] | None = None,
    now: datetime | None = None,
    cooldown: Cooldown | None = None,
    source_id: str | None = None,
) -> list[tuple[Rule, float | None, float | None, str, datetime]]:
    """对单个 `PriceTick` 跑全部启用规则（异动检测的唯一入口）。

    返回 `[(rule, prev_value, delta_or_z, message, cooldown_until), ...]`；由
    `application/alert_service.py` 组装成 `AlertEvent` 并分发（本层不落库、不发告警）。

    Args:
        tick: 本轮快照；`quality == "QUARANTINED"` 时**直接返回空列表**（被隔离的
            快照不进异动检测）。
        rules: 规则序列（含 `alerts.yaml` 载入后的完整集合）。
        prev: `{metric: 上一轮值}`；缺省空 dict。
        history: `{metric: 历史样本序列}`（zscore 规则需要）；缺省空 dict。
        now: 判定时刻；缺省 `utc_now()`。显式传入可保证同一轮所有 item 同点判定。
        cooldown: 冷却门；缺省 `None`（不抑制，仅用于测试/纯函数场景）。
        source_id: 覆盖 `tick.source_id`（多源合并场景）；缺省用 tick 自带值。

    Returns:
        触发的规则列表，每项为
        `(rule, prev_value, delta_or_z, message, cooldown_until)`。
        `prev` / `history` 缺失的 metric 直接跳过（首轮无历史不告警，避免启动风暴）。
    """
    if is_quarantined(tick.quality):
        return []

    now = now or utc_now()
    prev = prev or {}
    history = history or {}
    sid = source_id or tick.source_id
    out: list[tuple[Rule, float | None, float | None, str, datetime]] = []

    for rule in rules:
        if not rule.enabled:
            continue
        curr = tick_value(tick, rule.metric)
        if curr is None:
            continue
        fired, observed = _triggered(
            rule, curr, prev.get(rule.metric), history.get(rule.metric, ())
        )
        if not fired:
            continue
        bucket = cooldown_bucket(now, rule.cooldown_seconds)
        key = dedupe_key(sid, rule.rule_id, tick.item_id, bucket)
        until = now + rule.cooldown()
        if cooldown is not None and cooldown.suppress(key, until, now=now):
            continue
        delta_pct = observed if rule.kind == "pct" else None
        prev_value = prev.get(rule.metric)
        if rule.kind == "abs":
            message = f"{rule.metric}={curr:.0f} 达到绝对阈值 {rule.threshold:.0f}"
        elif rule.kind == "zscore":
            message = f"{rule.metric}={curr:.0f} 偏离 {observed:+.2f}σ（阈值 ±{rule.threshold}）"
        else:
            direction = "下跌" if (delta_pct or 0) < 0 else "上涨"
            message = (
                f"{rule.metric} {direction} {abs(delta_pct or 0):.2f}% "
                f"（{prev_value:.0f} -> {curr:.0f}）"
            )
        out.append((rule, prev_value, delta_pct, message, until))
    return out
