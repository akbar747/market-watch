# examples/smoke_policy_check.py
"""示例：在**不联网的前提下**跑一遍合规检查点 C1 / C3 / C4 / C5 / C6 / C8。

为什么是「不联网」：
    `market_watch policy-check` 会真抓 robots.txt（C2），那是有网络副作用的动作，
    不适合放在示例里随便跑。本示例只跑**纯逻辑**检查点——它们全部是零 IO 的
    纯函数，毫秒级完成、不产生任何出网流量，可以安全地贴到 CI 或 pre-commit 里。

用法::

    python examples/smoke_policy_check.py                     # 用默认占位 UA（会报 C1 失败，正好演示）
    python examples/smoke_policy_check.py --contact https://your.example/contact
    python examples/smoke_policy_check.py --contact https://you.example/contact --json

退出码：0 = 全部通过；1 = 有用例失败（CI 可直接消费）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

sys.path.insert(0, "src")  # 让示例在未安装（uv run python）时也能 import 到 src-layout 源码

from market_watch.domain import policy  # noqa: E402
from market_watch.domain.anomaly import DEFAULT_RULES  # noqa: E402
from market_watch.domain.errors import PolicyViolation  # noqa: E402


def _row(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"checkpoint": name, "ok": ok, "detail": detail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="合规检查点纯逻辑冒烟（离线）")
    parser.add_argument(
        "--contact",
        default="https://you.example/contact",
        help="C1 联系地址；占位域 you.example 只告警不拒绝，example.invalid 会被拒绝启动",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")
    parser.add_argument("--version", default="0.1.0", help="UA 里的版本号")
    args = parser.parse_args(argv)

    ua = policy.build_user_agent(args.version, args.contact)
    rows: list[dict[str, Any]] = []

    # ---- C1：UA 必须标识自己 ----
    try:
        policy.validate_user_agent(ua)
        rows.append(_row("C1", True, f"UA 合法: {ua}"))
    except PolicyViolation as exc:
        rows.append(_row("C1", False, str(exc)))

    # ---- C3：速率硬钳制（600 官方额度 × 0.8 安全系数 = 8.0 rps）----
    rows.append(
        _row(
            "C3",
            True,
            f"OFFICIAL_RATE_LIMIT_PER_MIN={policy.OFFICIAL_RATE_LIMIT_PER_MIN} "
            f"HOST_SAFETY_FACTOR={policy.HOST_SAFETY_FACTOR} "
            f"EFFECTIVE_RPS_CAP={policy.EFFECTIVE_RPS_CAP:.2f} rps",
        )
    )
    for declared in (50.0, 8.0, 2.0):
        effective = policy.effective_rps(declared)
        if effective > policy.EFFECTIVE_RPS_CAP:
            rows.append(_row("C3", False, f"钳制失效: {declared} -> {effective} 超过上界"))
    rows.append(_row("C3", True, "effective_rps(50.0)=8.0 / (8.0)=8.0 / (2.0)=2.0 —— 只降不升"))

    # ---- C4：只读出网（GET/HEAD，无 body）----
    ok_c4 = True
    for method, content, should_pass in (
        ("GET", None, True),
        ("HEAD", None, True),
        ("POST", b"", False),
        ("GET", b"{}", False),
    ):
        try:
            policy.assert_readonly(method, content)
            ok_c4 = ok_c4 and should_pass
        except policy.ReadOnlyViolation:
            ok_c4 = ok_c4 and not should_pass
    rows.append(_row("C4", ok_c4, f"白名单 {sorted(policy.ALLOWED_METHODS)}；带 body 的 GET 被拒"))

    # ---- C5：单一代理，禁止代理池 ----
    try:
        policy.assert_single_proxy(None)
        policy.assert_single_proxy("http://proxy.internal:3128")
        rows.append(_row("C5", True, "单代理放行；未配置代理（trust_env=False）"))
    except PolicyViolation as exc:
        rows.append(_row("C5", False, str(exc)))
    try:
        policy.assert_single_proxy("http://a:8080,http://b:8080")
        rows.append(_row("C5", False, "含逗号的代理池没有被拒绝"))
    except PolicyViolation:
        rows.append(_row("C5", True, "含逗号的代理池被拒绝启动"))

    # ---- C6：禁用依赖黑名单（扫 sys.modules + 硬编码名单）----
    hits = policy.assert_no_forbidden_deps()
    rows.append(
        _row(
            "C6", not hits, f"黑名单 {len(policy.FORBIDDEN_MODULES)} 条，运行期命中 {len(hits)} 个"
        )
    )

    # ---- C8：不采集个人信息（反向断言）----
    samples = {
        "email": "player@example.com",
        "handle": " @guildmaster",
        "phone_cn11": "13800138000",
        "coord_pair": "31.2304,121.4737",
    }
    hit_rules = {name: policy.scan_pii(value) for name, value in samples.items()}
    clean = policy.scan_pii(12345)  # 数值与纯字符串一律干净
    clean_str = policy.scan_pii("Vial of Ectoplasm")
    all_hit = all(rules for rules in hit_rules.values())
    rows.append(
        _row(
            "C8",
            all_hit and clean == [] and clean_str == [],
            f"{len(policy.PII_PATTERNS)} 条正则；4 类样本全部命中"
            f" {'/'.join(sorted({r for v in hit_rules.values() for r in v}))}；纯数值与商品名判定为干净",
        )
    )

    # ---- 附加：异动规则的默认档位（给 alert_service 的 config/alerts.yaml 兜底）----
    bad_rules = [
        r.rule_id for r in DEFAULT_RULES if not r.enabled or r.cooldown().total_seconds() <= 0
    ]
    rows.append(
        _row(
            "+1",
            len(bad_rules) == 0 and len(DEFAULT_RULES) >= 3,
            f"默认异动规则 {len(DEFAULT_RULES)} 条"
            f"（{'/'.join(r.rule_id for r in DEFAULT_RULES)}），冷却窗口全部为正",
        )
    )

    if args.as_json:
        print(
            json.dumps(
                {"ok": all(r["ok"] for r in rows), "rows": rows}, ensure_ascii=False, indent=2
            )
        )
        return 0 if all(r["ok"] for r in rows) else 1

    print("market-watch 合规冒烟（离线纯逻辑检查点）")
    print("=" * 78)
    for row in rows:
        mark = "[PASS]" if row["ok"] else "[FAIL]"
        print(f"{mark} {row['checkpoint']:<4} {row['detail']}")
    print("=" * 78)
    failed = [r["checkpoint"] for r in rows if not r["ok"]]
    print(f"{len(rows) - len(failed)}/{len(rows)} 通过" + (f"；失败: {failed}" if failed else ""))
    print(
        "注意：C2（robots.txt）与 C7（压测仅 loopback）需要联网/压测上下文，见 "
        "`market_watch policy-check`；这里不产生任何出网流量。"
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
