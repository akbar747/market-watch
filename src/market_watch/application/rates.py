# src/market_watch/application/rates.py
"""把 config + `config/sources.yaml` 编译成每主机的 Politeness 预算。

架构依据：docs/01-architecture.md §4.2/§4.4、§5.1（`MIN_INTERVAL_FROM_MAX_AGE=True`）。

两条硬约束：
1. **C3 钳制**：`rps = min(声明值, EFFECTIVE_RPS_CAP)`，adapter 无权突破。
2. **max-age 尊重**：`next_run_at = last_run_at + max(min_interval, observed_max_age)`。
   实测 `prices` 的 `Cache-Control: public, max-age=120` → 轮询周期硬底 120s。
   ⚠️ 实测补充（Phase 2 发现）：`listings` 单品响应为 `max-age=1200`（20 分钟）、
   `items` 为 `max-age=3600`。按 §4.4 铁律，这两类的实际周期会被抬到 1200s / 3600s，
   并在日志打印 `interval.clamped`。这是**尊重缓存的必然结果**，不是 bug；
   若要缩短，唯一合法方式是等官方缩短 max-age，不得靠改我们的间隔绕过。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
import logging
from typing import Final

from ..domain.models import Politeness
from .config import EFFECTIVE_RPS_CAP, Settings

__all__ = [
    "DEFAULT_MAX_AGE_S",
    "PolitenessBook",
    "clamp_interval",
    "compile_politeness",
    "next_run_at",
]

_log = logging.getLogger("market_watch.rates")

#: 端点未返回 max-age 时的保守兜底（= 实测 prices 的 120s）
DEFAULT_MAX_AGE_S: Final[int] = 120


class PolitenessBook:
    """每 source_id → 已钳制的 Politeness。构造期一次性编译，运行期只读。"""

    __slots__ = ("_by_host", "_by_source")

    def __init__(
        self, by_source: Mapping[str, Politeness], host_of: Mapping[str, str] | None = None
    ) -> None:
        self._by_source = dict(by_source)
        self._by_host: dict[str, Politeness] = {}
        if host_of:
            for source_id, host in host_of.items():
                pol = self._by_source.get(source_id)
                if pol is None:
                    continue
                # 同一 host 被多个 source 共享时取更保守（更小）的 rps
                existing = self._by_host.get(host)
                if existing is None or pol.rps < existing.rps:
                    self._by_host[host] = pol

    def for_source(self, source_id: str) -> Politeness:
        try:
            return self._by_source[source_id]
        except KeyError:
            raise KeyError(
                f"未登记的 source_id: {source_id!r}（检查 config/sources.yaml）"
            ) from None

    def for_host(self, host: str) -> Politeness:
        return self._by_host[host]

    def hosts(self) -> tuple[str, ...]:
        return tuple(self._by_host)

    def sources(self) -> tuple[str, ...]:
        return tuple(self._by_source)

    def as_dict(self) -> dict[str, Politeness]:
        return dict(self._by_source)


def compile_politeness(
    settings: Settings,
    declared: Mapping[str, Politeness],
    host_of: Mapping[str, str] | None = None,
) -> PolitenessBook:
    """把 adapter 声明的 Politeness 按 C3 钳制后编译成 book。

    钳制项：
    - `rps` → `min(rps, EFFECTIVE_RPS_CAP)`（只降不升）
    - `burst` → ≤ 64
    - `concurrency` → ≤ settings.per_host_concurrency（配置层再收一道）
    """
    out: dict[str, Politeness] = {}
    for source_id, pol in declared.items():
        capped = pol.clamped(EFFECTIVE_RPS_CAP)
        if capped.rps < pol.rps:
            _log.warning(
                "rate.clamped source=%s declared=%.3f effective=%.3f cap=%.3f "
                "（想提速请用满 page_size=%d 减少请求数，不得提高 rps）",
                source_id,
                pol.rps,
                capped.rps,
                EFFECTIVE_RPS_CAP,
                settings.page_size,
            )
        if capped.concurrency > settings.per_host_concurrency:
            _log.warning(
                "concurrency.clamped source=%s declared=%d effective=%d",
                source_id,
                capped.concurrency,
                settings.per_host_concurrency,
            )
            capped = replace(capped, concurrency=settings.per_host_concurrency)
        out[source_id] = capped
    return PolitenessBook(out, host_of)


def clamp_interval(
    configured_s: int,
    observed_max_age_s: int | None,
    *,
    respect_max_age: bool = True,
) -> tuple[int, bool]:
    """返回 (实际间隔秒, 是否被抬升)。

    §4.4 铁律：无论如何第二轮循环不得在 `max-age` 内启动。
    """
    if not respect_max_age:
        return max(1, configured_s), False
    floor = observed_max_age_s if observed_max_age_s is not None else DEFAULT_MAX_AGE_S
    effective = max(int(configured_s), int(floor))
    return effective, effective > int(configured_s)


def next_run_at(
    last_run_at: datetime | None,
    configured_s: int,
    observed_max_age_s: int | None = None,
    *,
    respect_max_age: bool = True,
    now: datetime | None = None,
) -> datetime:
    """计算下次可运行时刻。抬升时打 `interval.clamped` WARNING（§4.4）。"""
    from ..domain.models import utc_now

    now = now or utc_now()
    if last_run_at is None:
        return now
    effective, clamped = clamp_interval(
        configured_s, observed_max_age_s, respect_max_age=respect_max_age
    )
    if clamped:
        _log.warning(
            "interval.clamped configured=%ds max_age=%ss effective=%ds "
            "（尊重 Cache-Control: max-age，见 §4.4）",
            configured_s,
            observed_max_age_s,
            effective,
        )
    return last_run_at + timedelta(seconds=effective)
