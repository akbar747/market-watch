# src/market_watch/infrastructure/alerting/stdout_sink.py
"""structlog 输出，本地运行时的即时反馈。

架构依据：docs/01-architecture.md §2（`alerting/stdout_sink.py`）。
"""

from __future__ import annotations

from ...domain.models import AlertEvent, iso_z
from ...logging_setup import get_logger

__all__ = ["StdoutAlertSink"]

_log = get_logger("market_watch.alerting.stdout")


class StdoutAlertSink:
    """实现 `domain.ports.AlertSink`。进程内即时打印（JSON 结构化）。"""

    __slots__ = ()

    def emit(self, event: AlertEvent) -> None:
        level = {"info": _log.info, "warn": _log.warning, "critical": _log.error}.get(
            event.severity, _log.warning
        )
        level(
            "alert",
            alert_id=event.alert_id,
            rule=event.rule,
            severity=event.severity,
            source_id=event.source_id,
            item_id=event.item_id,
            metric=event.metric,
            prev_value=event.prev_value,
            curr_value=event.curr_value,
            delta_pct=event.delta_pct,
            cooldown_until=iso_z(event.cooldown_until),
            message=event.message,
        )
