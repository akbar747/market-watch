# src/market_watch/infrastructure/http/metrics.py
"""计数器 / 直方图（内存 + 落 fetch_log），给看板健康页与 `/api/health`。

架构依据：docs/01-architecture.md §3（`metrics.py` 注释）、§8.2（`fetch_log` 表）。

内存计数器是有界的：直方图只保留最近 N 个样本，避免长跑进程无限膨胀。
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
import math
from typing import Final

from ...domain.models import iso_z, utc_now
from ...logging_setup import get_logger

__all__ = ["HISTOGRAM_SAMPLE_LIMIT", "Metrics"]

_log = get_logger("market_watch.metrics")

#: 每个直方图最多保留的样本数（P99 计算用）；超出丢弃最旧的
HISTOGRAM_SAMPLE_LIMIT: Final[int] = 2_000


class Metrics:
    """进程内指标。`fetch_log` 行通过 `sink` 回调交给 writer 落库（本类不碰 sqlite3）。"""

    __slots__ = ("_counters", "_fetch_log_sink", "_gauges", "_histograms")

    def __init__(self, fetch_log_sink: Callable[[tuple], None] | None = None) -> None:
        """构造指标容器。

        Args:
            fetch_log_sink: 接收一条 `fetch_log` 行元组（12 列）的回调，由调用方
                接到 writer 落库。本类**不碰 sqlite3**，因此单测可以传一个 list.append
                就跑通；传 `None` 则只做内存计数（合规留痕会缺失，生产必须传）。
        """
        self._counters: dict[str, int] = defaultdict(int)
        self._histograms: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=HISTOGRAM_SAMPLE_LIMIT)
        )
        self._gauges: dict[str, float] = {}
        self._fetch_log_sink = fetch_log_sink

    # ---- 计数 ----
    def count(self, name: str, value: int = 1, **labels: object) -> None:
        """累加一个计数器。

        Args:
            name: 指标名。
            value: 增量（可为负）。
            **labels: 维度标签，会被排序后拼进 `{k=v,...}` 组成复合键。
        """
        key = _key(name, labels)
        self._counters[key] += value

    def gauge(self, name: str, value: float) -> None:
        """写一个瞬时值（覆盖式，不是累加）。

        Args:
            name: 指标名。
            value: 瞬时值（如「当前队列积压深度」）。
        """
        self._gauges[name] = value

    def observe(self, name: str, *, duration_ms: float, **labels: object) -> None:
        """记录一个耗时样本（进直方图 + 累加 `name.total` 计数）。

        Args:
            name: 指标名（通常是前缀，如 `'http.request'`）。
            duration_ms: 观测耗时（毫秒）。
            **labels: 维度标签。
        """
        key = _key(name, labels)
        self._histograms[key].append(float(duration_ms))
        self._counters[f"{name}.total"] += 1

    # ---- 读取 ----
    def counter(self, name_prefix: str) -> dict[str, int]:
        """取所有以 `name_prefix` 开头的计数器快照。

        Args:
            name_prefix: 指标名前缀。

        Returns:
            `{复合键名: 计数值}` 的拷贝。
        """
        return {k: v for k, v in self._counters.items() if k.startswith(name_prefix)}

    def percentile(self, name: str, q: float = 0.99) -> float:
        """算单个直方图指标的第 `q` 分位（近似值，样本有上限）。

        Args:
            name: 指标的**完整复合键名**（含标签），不是前缀。
            q: 分位，0~1。

        Returns:
            分位值（毫秒）；无样本返回 `0.0`（不返回 `inf`）。
        """
        samples = sorted(self._histograms.get(name, ()))
        if not samples:
            return 0.0
        idx = _quantile_index(samples, q)
        return samples[idx]

    def p99(self, name_prefix: str = "http.request") -> float:
        """算所有以 `name_prefix` 开头的直方图合并后的 P99（看板健康页用）。"""
        candidates = [k for k in self._histograms if k.startswith(name_prefix)]
        if not candidates:
            return 0.0
        merged: list[float] = []
        for k in candidates:
            merged.extend(self._histograms[k])
        if not merged:
            return 0.0
        merged.sort()
        return merged[_quantile_index(merged, 0.99)]

    def snapshot(self) -> dict[str, object]:
        """导出全量指标快照（`/api/health` 的直接序列化结果）。

        Returns:
            含 `counters` / `gauges` / `p99_ms` / `histograms` 的字典。
        """
        return {
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "p99_ms": self.p99(),
            "histograms": {k: len(v) for k, v in self._histograms.items()},
        }

    # ---- fetch_log 留痕（合规审计，§8.2） ----
    def record_fetch_log(
        self,
        *,
        host: str,
        path: str,
        method: str,
        status_code: int | None,
        attempts: int,
        duration_ms: float,
        resp_bytes: int,  # noqa: ARG002 - 留痕字段，接口契约要求记录任意响应体大小
        error_class: str,
        ua: str,
        cycle_id: str | None = None,
    ) -> None:
        """写一行 `fetch_log`（合规审计留痕，§8.2）。

        即使 `cycle_id` 为 `None` 也照写——审计表的每一行都必须有留痕，
        缺轮次信息只影响归因，不影响「我们请求过什么」这一事实。

        Raises:
            异常被吞：`fetch_log_sink` 抛任何异常都只记一条 warning，
            指标落库失败不得影响正在进行的采集。
        """
        row = (
            cycle_id,
            host,
            path,
            method,
            status_code,
            attempts,
            round(duration_ms, 2),
            resp_bytes,  # noqa: ARG002
            error_class,
            1,  # robots_ok：能走到这里说明 robots 已放行
            ua,
            iso_z(utc_now()),
        )
        if self._fetch_log_sink is not None:
            try:
                self._fetch_log_sink(row)
            except Exception as exc:  # noqa: BLE001 - 指标落库失败不得影响采集
                _log.warning("metrics.fetch_log_sink_failed err=%s", exc)

    def reset(self) -> None:
        """清空全部内存指标（仅供测试与 GC 后的对照用）。"""
        self._counters.clear()
        self._histograms.clear()
        self._gauges.clear()


def _quantile_index(sorted_values: Sequence[float], q: float) -> int:
    """算升序样本里第 `q` 分位对应的下标（`ceil(q * n) - 1`，夹到边界内）。"""
    n = len(sorted_values)
    idx = math.ceil(q * n) - 1
    return min(n - 1, max(0, idx))


def _key(name: str, labels: Mapping[str, object]) -> str:
    """把标签排序后拼成稳定的复合键 `{k=v,...}`。"""
    if not labels:
        return name
    parts = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{parts}}}"
