# examples/alert_webhook.py
"""示例：本地告警 → 推送到一个 Webhook（占位 URL，默认**不发请求**）。

设计取舍（为什么默认不 send）：
    占位 URL 是 `https://example.invalid/hook`（RFC 2606 保留域，注定 DNS 失败）。
    示例既要「能离线跑通」，又要「把推送姿势演示清楚」，所以默认只**组装并打印**
    payload；确认接线无误后加 `--send` 才真的 POST。这样 CI / README 里的示例
    不会意外往外网打请求。

用法::

    python examples/alert_webhook.py                                   # 离线跑一遍，打印 payload
    python examples/alert_webhook.py --send                            # 真的 POST 到 --webhook
    python examples/alert_webhook.py --webhook http://127.0.0.1:8765/hook
    python examples/alert_webhook.py --send --json                     # 输出 JSON

合规说明：本示例发出的只有 **POST 到你自己指定的接收端**（localhost 或你自己
部署的服务），**不是**向官方端点写数据——官方端点只接受 GET/HEAD（C4），
本项目从不 POST/PUT/DELETE 任何官方 endpoint。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import sys
from typing import Any

sys.path.insert(0, "src")

from market_watch.domain.anomaly import (  # noqa: E402
    DEFAULT_RULES,
    Cooldown,
    dedupe_key,
    detect,
)
from market_watch.domain.models import AlertEvent, PriceTick, utc_now  # noqa: E402

#: 占位接收端：RFC 2606 保留域，保证「不加 --send 就不会真的发出去」
PLACEHOLDER_WEBHOOK = "https://example.invalid/hook"


def build_alert(
    now: datetime, *, item_id: int = 24, sell_price: int = 1_150
) -> tuple[AlertEvent, str]:
    """跑一遍异动检测并组装 `AlertEvent`。

    返回 `(event, message)`。`message` 直接来自 `anomaly.detect()` 的产出，
    说明「异动判定的文案」这一层是可复用的，推送只负责把它送出去。
    """
    tick = PriceTick(
        source_id="gw2_prices",
        item_id=item_id,
        observed_at=now,
        cycle_id="demo-cycle",
        buy_unit_price=sell_price - 200,
        buy_quantity=128,
        sell_unit_price=sell_price,
        sell_quantity=312,
        whitelisted=True,
        quality="COMPLETE",
    )
    # 触发规则：sell_unit_price 环比 +25% 以上（DEFAULT_RULES 里的 price_spike_25pct）
    prev = {"sell_unit_price": float(sell_price) / 1.30}
    fired = [
        (rule, prev_value, delta, message, until)
        for rule, prev_value, delta, message, until in _detect(tick, prev, now)
    ]
    if not fired:
        raise SystemExit("本轮没有触发任何默认规则——可以把 --price 调高再试")
    rule, prev_value, _delta, message, until = fired[0]
    event = AlertEvent(
        alert_id="demo-alert-0001",
        dedupe_key=dedupe_key(tick.source_id, rule.rule_id, item_id, 0),
        rule=rule.rule_id,
        source_id=tick.source_id,
        item_id=item_id,
        severity=rule.severity,
        metric=rule.metric,
        prev_value=prev_value,
        curr_value=float(sell_price),
        delta_pct=30.77,
        message=message,
        fired_at=now,
        cooldown_until=until,
    )
    return event, message


def _detect(tick: PriceTick, prev: dict[str, float], now: datetime):
    return detect(tick, DEFAULT_RULES, prev=prev, now=now, cooldown=Cooldown())


def dispatch(event: AlertEvent, *, webhook: str, send: bool) -> dict[str, Any]:
    """把 `AlertEvent` 序列化成 webhook payload（默认只返回 payload，不发包）。"""
    payload: dict[str, Any] = {
        "alert_id": event.alert_id,
        "rule": event.rule,
        "severity": event.severity,
        "item_id": event.item_id,
        "metric": event.metric,
        "prev_value": event.prev_value,
        "curr_value": event.curr_value,
        "delta_pct": event.delta_pct,
        "message": event.message,
        "fired_at": event.fired_at.isoformat(),
        "cooldown_until": event.cooldown_until.isoformat(),
        "dedupe_key": event.dedupe_key,
    }
    if not send:
        return payload

    import httpx

    # C5：trust_env=False，不让环境变量里的代理把告警流量带走
    with httpx.Client(trust_env=False, timeout=10.0) as client:
        resp = client.post(webhook, json=payload)
        if resp.status_code >= 400:
            print(f"webhook 返回非 2xx: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
            return payload
    payload["delivered"] = True
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地告警推送到 Webhook（占位 URL）")
    parser.add_argument("--webhook", default=PLACEHOLDER_WEBHOOK, help="接收端 URL（默认占位域）")
    parser.add_argument("--send", action="store_true", help="真的 POST；默认只打印不发包")
    parser.add_argument("--price", type=int, default=1_150, help="本轮售价，用来触发默认涨幅规则")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    now = datetime.now(tz=UTC)
    event, message = build_alert(now, sell_price=args.price)
    payload = dispatch(event, webhook=args.webhook, send=args.send)
    payload["webhook"] = args.webhook
    payload["sent"] = args.send

    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"fired_at  : {utc_now().isoformat()}")
        print(f"message   : {message}")
        print(f"webhook   : {args.webhook}")
        print(
            f"sent      : {'是（--send）' if args.send else '否（默认只打印，把 --send 加上才真的 POST）'}"
        )
        print("-" * 60)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
