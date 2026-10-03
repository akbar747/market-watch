# src/market_watch/infrastructure/alerting/sqlite_sink.py
"""落 `alert_event`，供看板展示与去重。

架构依据：docs/01-architecture.md §7.2（AlertEvent）、§8.2（`ux_alert_dedupe`）。

去重键 `ux_alert_dedupe(dedupe_key, cooldown_until)` 在库侧兜底；进程内还有
`anomaly.Cooldown` 一道门（双保险，避免写放大）。
"""

from __future__ import annotations

from ...domain.models import AlertEvent, iso_z
from ...logging_setup import get_logger

__all__ = ["SqliteAlertSink"]

_log = get_logger("market_watch.alerting.sqlite")


class SqliteAlertSink:
    """实现 `domain.ports.AlertSink`。通过 writer 提交（不直接持有连接）。"""

    __slots__ = ("_cycle_id", "_writer")

    def __init__(self, writer, cycle_id: str = "") -> None:  # noqa: ANN001 - SnapshotSink
        self._writer = writer
        self._cycle_id = cycle_id

    def emit(self, event: AlertEvent) -> None:
        row = (
            event.alert_id,
            event.dedupe_key,
            event.rule,
            event.source_id,
            event.item_id,
            event.severity,
            event.metric,
            event.prev_value,
            event.curr_value,
            event.delta_pct,
            event.message,
            iso_z(event.fired_at),
            iso_z(event.cooldown_until),
            1 if event.acknowledged else 0,
        )
        self._writer.submit(("alert_event", [row]))
        _log.warning(
            "alert.emitted rule=%s severity=%s item=%s metric=%s delta_pct=%s",
            event.rule,
            event.severity,
            event.item_id,
            event.metric,
            event.delta_pct,
        )
