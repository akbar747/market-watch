# src/market_watch/application/alert_service.py
"""异动 → `AlertEvent` → 多 sink 分发 + 冷却。

架构依据：docs/01-architecture.md §3、§11.1（3）。

关键约束（§6.1-4 / §11.1-3）：
- **被一致性规则隔离（`QUARANTINED`）的快照不得进入异动检测**——否则「本轮没抓到」
  会被误判成「价格归零/暴跌」。
- `quality='PARTIAL'` 的那一轮：**跳过本轮未采到（missing）物品的异动检测**。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final, Literal
import uuid

from ..domain.anomaly import Cooldown, Rule, dedupe_key, detect
from ..domain.models import (
    AlertEvent,
    ConsistencyViolation,
    PriceTick,
    iso_z,
    utc_now,
)
from ..domain.ports import AlertSink
from ..logging_setup import get_logger

__all__ = ["DEFAULT_RULES_YAML", "AlertService", "load_rules"]

_log = get_logger("market_watch.alert_service")

#: config/alerts.yaml 缺失时的兜底（与 domain.anomaly.DEFAULT_RULES 一致）
DEFAULT_RULES_YAML: Final[dict] = {
    "rules": [
        {
            "id": "price_drop_15pct",
            "metric": "sell_unit_price",
            "kind": "pct",
            "threshold": -15.0,
            "cooldown_seconds": 3600,
            "severity": "warn",
            "enabled": True,
        },
        {
            "id": "price_spike_25pct",
            "metric": "sell_unit_price",
            "kind": "pct",
            "threshold": 25.0,
            "cooldown_seconds": 3600,
            "severity": "warn",
            "enabled": True,
        },
        {
            "id": "spread_wide",
            "metric": "spread",
            "kind": "abs",
            "threshold": 10000.0,
            "cooldown_seconds": 7200,
            "severity": "info",
            "enabled": True,
        },
    ]
}


def load_rules(path) -> tuple[Rule, ...]:  # noqa: ANN001 - Path
    """从 `config/alerts.yaml` 读规则；缺失/损坏时回落默认并告警。

    本仓不引 PyYAML（依赖清单 §12 未列），因此用一个极小的 YAML 子集解析：
    支持顶层 `rules:` 列表 + 每项的 `key: value`（标量）。
    若环境里装了 PyYAML 则优先用它（行为一致，只是更稳）。
    """
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        _log.warning("alerts.yaml.missing path=%s -> 使用默认规则", p)
        return _rules_from_mapping(DEFAULT_RULES_YAML)

    text = p.read_text(encoding="utf-8")
    data = None
    try:
        import yaml

        data = yaml.safe_load(text)
    except ImportError:
        data = _mini_yaml(text)
    except Exception as exc:  # noqa: BLE001
        _log.error("alerts.yaml.parse_failed err=%s -> 使用默认规则", exc)
        return _rules_from_mapping(DEFAULT_RULES_YAML)

    if not isinstance(data, dict) or "rules" not in data:
        _log.error("alerts.yaml.invalid -> 使用默认规则")
        return _rules_from_mapping(DEFAULT_RULES_YAML)
    return _rules_from_mapping(data)


#: `kind` / `severity` 的白名单。`Rule` 是 frozen dataclass——它字段上的 `Literal`
#: 只给类型检查器看，运行时**不校验**；配置又是外部 YAML，所以合法值集合必须在
#: 这条边界上显式写死。非法值若放过去，`detect` → `_triggered` **没有「未知 kind」
#: 分支**，直接落到 `curr >= threshold`（abs 语义），而告警文案却仍按 pct 拼——
#: 配置的语义被悄悄换掉，日志里一点痕迹都没有，比直接报错更危险。
_KINDS: Final[frozenset[Literal["pct", "zscore", "abs"]]] = frozenset({"pct", "zscore", "abs"})
_KIND_FALLBACK: Final[Literal["pct", "zscore", "abs"]] = "pct"
_SEVERITIES: Final[frozenset[Literal["info", "warn", "critical"]]] = frozenset(
    {"info", "warn", "critical"}
)
_SEVERITY_FALLBACK: Final[Literal["info", "warn", "critical"]] = "warn"


def _rules_from_mapping(data: Mapping) -> tuple[Rule, ...]:
    out: list[Rule] = []
    for raw in data.get("rules", []):
        # ★ 白名单校验放在 try 之外：不合法只记 error 日志 + 退回默认值，绝不外抛
        kind_raw = str(raw.get("kind", _KIND_FALLBACK))
        kind: Literal["pct", "zscore", "abs"] = _KIND_FALLBACK
        if kind_raw in _KINDS:
            kind = kind_raw
        else:
            _log.error(
                "alerts.rule.kind_invalid raw=%s kind=%r -> 退回 %r", raw, kind_raw, _KIND_FALLBACK
            )

        sev_raw = str(raw.get("severity", _SEVERITY_FALLBACK))
        severity: Literal["info", "warn", "critical"] = _SEVERITY_FALLBACK
        if sev_raw in _SEVERITIES:
            severity = sev_raw
        else:
            _log.error(
                "alerts.rule.severity_invalid raw=%s severity=%r -> 退回 %r",
                raw,
                sev_raw,
                _SEVERITY_FALLBACK,
            )

        try:
            out.append(
                Rule(
                    rule_id=str(raw["id"]),
                    metric=str(raw["metric"]),
                    kind=kind,
                    threshold=float(raw["threshold"]),
                    window=int(raw.get("window", 0)),
                    cooldown_seconds=int(raw.get("cooldown_seconds", 3600)),
                    severity=severity,
                    enabled=bool(raw.get("enabled", True)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            _log.error("alerts.rule_invalid raw=%s err=%s -> 跳过该规则", raw, exc)
    return tuple(out)


def _mini_yaml(text: str) -> dict:
    """极小的 YAML 子集解析器：`rules:` + `- key: value` 缩进块。

    仅为本仓的 `alerts.yaml` 服务；不追求通用性（引 PyYAML 会让 exe 体积变大）。
    """
    rules: list[dict] = []
    current: dict | None = None
    in_rules = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "rules:":
            in_rules = True
            continue
        if not in_rules:
            continue
        if stripped.startswith("- "):
            current = {}
            rules.append(current)
            rest = stripped[2:].strip()
            if ":" in rest:
                k, v = rest.split(":", 1)
                current[k.strip()] = _coerce(v.strip())
            continue
        if current is not None and ":" in stripped:
            k, v = stripped.split(":", 1)
            current[k.strip()] = _coerce(v.strip())
    return {"rules": rules}


def _coerce(value: str):  # noqa: ANN201
    low = value.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value.strip("'\"")


class AlertService:
    """把 `PriceTick` 序列编排成 `AlertEvent` 并分发到全部 sink。"""

    __slots__ = ("_cooldown", "_emitted", "_rules", "_sinks")

    def __init__(
        self,
        sinks: Sequence[AlertSink],
        rules: Sequence[Rule],
        *,
        cooldown: Cooldown | None = None,
    ) -> None:
        self._sinks = tuple(sinks)
        self._rules = tuple(rules)
        self._cooldown = cooldown or Cooldown()
        self._emitted = 0

    @property
    def emitted(self) -> int:
        return self._emitted

    def evaluate(
        self,
        ticks: Sequence[PriceTick],
        *,
        prev_values: Mapping[int, Mapping[str, float]] | None = None,
        histories: Mapping[int, Mapping[str, Sequence[float]]] | None = None,
        missing_item_ids: frozenset[int] | None = None,
        quality: str = "COMPLETE",
        now: datetime | None = None,
    ) -> list[AlertEvent]:
        """跑异动检测并分发。返回本次产生的 `AlertEvent` 列表。

        - `quality='PARTIAL'` 时跳过 `missing_item_ids`（§6.1-4）。
        - 被隔离（`QUARANTINED`）的 tick 由 `detect` 内部跳过。
        """
        now = now or utc_now()
        prev_values = prev_values or {}
        histories = histories or {}
        missing = missing_item_ids or frozenset()
        events: list[AlertEvent] = []

        for tick in ticks:
            if quality == "PARTIAL" and tick.item_id in missing:
                continue
            fired = detect(
                tick,
                self._rules,
                prev=prev_values.get(tick.item_id),
                history=histories.get(tick.item_id),
                now=now,
                cooldown=self._cooldown,
                source_id=tick.source_id,
            )
            for rule, prev_value, delta_pct, message, until in fired:
                bucket = int(now.timestamp()) // max(1, rule.cooldown_seconds)
                event = AlertEvent(
                    alert_id=str(uuid.uuid4()),
                    dedupe_key=dedupe_key(tick.source_id, rule.rule_id, tick.item_id, bucket),
                    rule=rule.rule_id,
                    source_id=tick.source_id,
                    item_id=tick.item_id,
                    severity=rule.severity,
                    metric=rule.metric,
                    prev_value=prev_value,
                    curr_value=_curr_of(tick, rule.metric),
                    delta_pct=delta_pct,
                    message=message,
                    fired_at=now,
                    cooldown_until=until,
                )
                events.append(event)

        for event in events:
            for sink in self._sinks:
                try:
                    sink.emit(event)
                except Exception as exc:  # noqa: BLE001 - 单个 sink 故障不得影响其它 sink
                    _log.error("alert.sink_failed sink=%s err=%s", type(sink).__name__, exc)
            self._emitted += 1
        return events

    def emit_data_reject(
        self,
        writer,  # noqa: ANN001 - SnapshotSink
        *,
        cycle_id: str,
        source_id: str,
        violation: ConsistencyViolation,
        payload: str = "",
        now: datetime | None = None,
    ) -> None:
        """数据质量隔离区落库（§11.1-3）：不静默丢弃。"""
        now = now or utc_now()
        writer.submit(
            (
                "data_reject",
                [
                    (
                        cycle_id,
                        source_id,
                        violation.item_id,
                        violation.rule,
                        violation.field,
                        violation.observed,
                        violation.expected,
                        violation.severity,
                        (payload or "")[:4096],
                        iso_z(now),
                    )
                ],
            )
        )


def _curr_of(tick: PriceTick, metric: str) -> float | None:
    from ..domain.pricing import tick_value

    return tick_value(tick, metric)
