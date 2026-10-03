# tests/unit/test_alert_service.py
"""`_rules_from_mapping` 的配置边界（外部 YAML → `Rule`）。

★ 这是**行为锁**，不是凑覆盖率。三条必须守住的语义：

1. `kind` / `severity` 是**无自然默认值**的枚举，非法值一律
   「记 error 日志 + 跳过该规则」，**绝不退回默认**。退回默认会把非法
   `kind` 静默变成 `abs` 语义（`_triggered` 没有「未知 kind」分支，非法值
   一路掉到 `curr >= threshold`），而文案仍按 pct 拼——用户照常收警报、
   语义却是错的，比不告警难发现得多。
2. 数值字段（`threshold` / `window` / `cooldown_seconds`）非法时仍然是
   「记 error 日志 + 跳过该规则」（走原有的 except），两种非法处理**同一条线**。
3. 整条边界**对任何输入都不抛**（`load_rules` 是启动期路径，抛了就是整站起不来）。
"""

from __future__ import annotations

import pytest

from market_watch.application.alert_service import (
    _KINDS,
    _SEVERITIES,
    DEFAULT_RULES_YAML,
    _rules_from_mapping,
)

#: 一条合法规则的最小字段集；其余用例只覆盖地被改
_BASE: dict = {"id": "r1", "metric": "spread", "kind": "abs", "threshold": 1.0}


def _rule(**over: object) -> dict:
    return {**_BASE, **over}


def test_alert_service_drops_unknown_kind() -> None:
    """`kind` 拼错 -> 跳过该规则，且不会退化成 pct/abs。"""
    rules = _rules_from_mapping({"rules": [_rule(kind="pct_change")]})
    assert rules == ()


def test_alert_service_drops_unknown_severity() -> None:
    """`severity` 拼错 -> 跳过该规则，且不会退化成 warn。"""
    rules = _rules_from_mapping({"rules": [_rule(severity="blocker")]})
    assert rules == ()


def test_alert_service_keeps_valid_kinds() -> None:
    """三个合法 `kind` 原样透传（白名单不是过滤器，是防退化）。"""
    rules = _rules_from_mapping({"rules": [_rule(kind=kind) for kind in ("pct", "zscore", "abs")]})
    assert [r.kind for r in rules] == ["pct", "zscore", "abs"]


def test_alert_service_keeps_valid_severities() -> None:
    rules = _rules_from_mapping(
        {"rules": [_rule(severity=s) for s in ("info", "warn", "critical")]}
    )
    assert [r.severity for r in rules] == ["info", "warn", "critical"]


def test_alert_service_omits_optional_fields() -> None:
    """缺省字段仍走默认（`Rule` 自带 window=0 / cooldown=3600 / severity='warn'）。"""
    rules = _rules_from_mapping({"rules": [{"id": "r1", "metric": "spread", "threshold": 1.0}]})
    assert len(rules) == 1
    assert rules[0].kind == "pct" and rules[0].severity == "warn"
    assert rules[0].window == 0 and rules[0].cooldown_seconds == 3600


def test_alert_service_enum_bounds_are_exactly_three() -> None:
    """白名单 == `Rule` 声明的 `Literal`：不允许悄悄放宽（放宽等于放非法值进来）。"""
    assert _KINDS == frozenset({"pct", "zscore", "abs"})
    assert _SEVERITIES == frozenset({"info", "warn", "critical"})


def test_alert_service_drops_non_str_kind() -> None:
    """`kind: 1` / `kind: null` 这类非字符串也拦在边界上。"""
    assert _rules_from_mapping({"rules": [_rule(kind=1)]}) == ()
    assert _rules_from_mapping({"rules": [_rule(kind=None)]}) == ()


@pytest.mark.parametrize("field", ("threshold", "window", "cooldown_seconds"))
def test_alert_service_drops_invalid_number(field: str) -> None:
    """数值字段非法仍走原有 except：跳过（与枚举非法同一条处理线）。"""
    bad = dict(_BASE)
    bad[field] = "not-a-number"
    assert _rules_from_mapping({"rules": [bad]}) == ()


def test_alert_service_drops_malformed_rule() -> None:
    """缺 `metric` / `threshold` -> KeyError -> 跳过（既有行为保持）。"""
    assert _rules_from_mapping({"rules": [{"id": "r1"}]}) == ()


def test_alert_service_never_raises() -> None:
    """四种坏输入混在一起：只留下合法的那条，整条调用零抛出。"""
    rules = _rules_from_mapping(
        {
            "rules": [
                _rule(id="a", kind="NOPE", severity="NOPE"),
                {"id": "b"},  # 缺 metric / threshold -> KeyError
                {"id": "c", "metric": "spread", "kind": "abs", "threshold": "x"},  # ValueError
                _rule(id="d", kind="pct", severity="critical"),
            ]
        }
    )
    assert [r.rule_id for r in rules] == ["d"]
    assert rules[0].kind == "pct" and rules[0].severity == "critical"


def test_alert_service_default_rules_yaml_is_untouched() -> None:
    """`DEFAULT_RULES_YAML` 全绿：内置规则集经同一条边界后一条不丢。"""
    rules = _rules_from_mapping(DEFAULT_RULES_YAML)
    assert [r.rule_id for r in rules] == ["price_drop_15pct", "price_spike_25pct", "spread_wide"]
