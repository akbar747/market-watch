# src/market_watch/domain/errors.py
"""错误分类枚举 + 异常类型 + 「错误 → 处置策略」唯一真相源。

架构依据：docs/01-architecture.md §6（错误分类与处理矩阵）、§13（唯一输入清单）。

落位说明（Phase 2 实现决策，避免循环 import）：
    `ErrorClass` / `AttemptPolicy` 在文档 §7.1 里被画在 `models.py` 中，但 `models.py`
    的 `ResponseView.json()` 需要抛 `PayloadError`，而 `POLICY` 又需要 `ErrorClass`——
    两者互相需要会形成 models <-> errors 环。因此本模块是「错误语义」的唯一定义处，
    `models.py` 通过 `from .errors import ...` 再导出，两条文档里的 import 路径
    （`domain.models.ErrorClass` 与 `domain.errors.POLICY`）均可用且无环。

本层禁止 import httpx / sqlite3 / fastapi（依赖方向铁律 §3）。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final, Literal, TypedDict

__all__ = [
    "POLICY",
    "TERMINAL_ERRORS",
    "AttemptPolicy",
    "BudgetExhausted",
    "ErrorClass",
    "MarketWatchError",
    "OpenCircuit",
    "PayloadError",
    "PolicyViolation",
    "ReadOnlyViolation",
    "RobotsDenied",
    "SchemaError",
    "SourceUnavailable",
    "policy_for",
]


class ErrorClass(StrEnum):
    """出站请求与解析结果的分类。分类的唯一出口是 infrastructure/http/classification.py。"""

    OK = "ok"
    DNS_FAILURE = "dns_failure"
    CONNECT_TIMEOUT = "connect_timeout"
    READ_TIMEOUT = "read_timeout"
    POOL_TIMEOUT = "pool_timeout"
    REMOTE_PROTOCOL = "remote_protocol"
    RATE_LIMITED_429 = "rate_limited_429"
    RATE_LIMITED_VENDOR = "rate_limited_vendor"
    RATE_LIMIT_HEADER_LOW = "rate_limit_header_low"
    SERVER_5XX = "server_5xx"
    AUTH_403 = "auth_403"
    ROBOTS_DENIED = "robots_denied"
    JSON_DECODE = "json_decode"
    SCHEMA_INVALID = "schema_invalid"
    PAGINATION_DRIFT = "pagination_drift"
    DEDUPE_CONFLICT = "dedupe_conflict"
    CONSISTENCY_FAILED = "consistency_failed"
    SSL_INVALID = "ssl_invalid"
    NOT_MODIFIED = "not_modified"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class AttemptPolicy(TypedDict):
    """§6 矩阵一行的机器可读形式。`retry.py::decide` 是唯一消费者。"""

    retryable: bool
    max_attempts: int  # 含首次；= 1 + 允许的重试次数
    count_as_failure: bool  # 是否计入熔断失败率
    cool_host: bool  # 是否立即按 cool_factor 对该主机降速
    cool_factor: float  # 降速系数 0.5 / 0.6 / 0.7
    open_breaker_now: bool  # 是否无条件立刻开闸
    alert_level: Literal["none", "info", "warn", "critical"]
    respect_retry_after: bool  # 是否优先采用 Retry-After 头


def _p(
    *,
    retryable: bool,
    max_attempts: int,
    count_as_failure: bool,
    cool_host: bool = False,
    cool_factor: float = 1.0,
    open_breaker_now: bool = False,
    alert_level: Literal["none", "info", "warn", "critical"] = "none",
    respect_retry_after: bool = False,
) -> AttemptPolicy:
    return {
        "retryable": retryable,
        "max_attempts": max_attempts,
        "count_as_failure": count_as_failure,
        "cool_host": cool_host,
        "cool_factor": cool_factor,
        "open_breaker_now": open_breaker_now,
        "alert_level": alert_level,
        "respect_retry_after": respect_retry_after,
    }


# ---------------------------------------------------------------------------
# §6 错误分类与处理矩阵 —— 唯一实现点
# ---------------------------------------------------------------------------
# 列含义对照 §6 表格：重试 / 退避策略 / 计熔断失败 / 告警 / 备注
POLICY: Final[Mapping[ErrorClass, AttemptPolicy]] = {
    # 0. 成功
    ErrorClass.OK: _p(retryable=False, max_attempts=1, count_as_failure=False),
    # 1. DNS 解析失败：重试 ≤2 次（共 3 次）；第 3 次前 invalidate(host)
    ErrorClass.DNS_FAILURE: _p(
        retryable=True, max_attempts=3, count_as_failure=True, alert_level="warn"
    ),
    # 2. 连接超时：重试 ≤3 次（共 4 次）；累计 ≥2 次 → invalidate(host)
    ErrorClass.CONNECT_TIMEOUT: _p(
        retryable=True, max_attempts=4, count_as_failure=True, alert_level="warn"
    ),
    # 3. 读超时：重试 ≤3 次；同 host 60s 内 ≥5 次 → rate *= 0.7 + 重建池
    ErrorClass.READ_TIMEOUT: _p(
        retryable=True,
        max_attempts=4,
        count_as_failure=True,
        cool_host=True,
        cool_factor=0.7,
        alert_level="warn",
    ),
    # 4. PoolTimeout：多半是连接泄漏，重建池最快；重试 ≤1 次
    ErrorClass.POOL_TIMEOUT: _p(
        retryable=True,
        max_attempts=2,
        count_as_failure=True,
        cool_host=True,
        cool_factor=0.7,
        alert_level="warn",
    ),
    # 5. RemoteProtocolError（半开连接）：重建池 + 重试 ≤2 次
    ErrorClass.REMOTE_PROTOCOL: _p(
        retryable=True,
        max_attempts=3,
        count_as_failure=True,
        cool_host=True,
        cool_factor=0.7,
        alert_level="warn",
    ),
    # 6. 429：优先 Retry-After（钳到 ≤120s），并立即 rate *= 0.5；必告
    ErrorClass.RATE_LIMITED_429: _p(
        retryable=True,
        max_attempts=4,
        count_as_failure=True,
        cool_host=True,
        cool_factor=0.5,
        alert_level="critical",
        respect_retry_after=True,
    ),
    # 7. 420 / vendor 自定义限流码：强制 max(Retry-After, 30s) 起步 + rate *= 0.5
    ErrorClass.RATE_LIMITED_VENDOR: _p(
        retryable=True,
        max_attempts=3,
        count_as_failure=True,
        cool_host=True,
        cool_factor=0.5,
        alert_level="critical",
        respect_retry_after=True,
    ),
    # 8. 限流头预警：请求本身成功，不重试，只主动降速 rate *= 0.6
    ErrorClass.RATE_LIMIT_HEADER_LOW: _p(
        retryable=False,
        max_attempts=1,
        count_as_failure=False,
        cool_host=True,
        cool_factor=0.6,
        alert_level="info",
    ),
    # 9. 5xx：指数+抖动；连续 8 次由 breaker 直接开闸（此处不 unconditional open）
    ErrorClass.SERVER_5XX: _p(
        retryable=True,
        max_attempts=4,
        count_as_failure=True,
        alert_level="warn",
        respect_retry_after=True,
    ),
    # 10. 401/403：上下文变了（限额/封禁/条款）→ 停机等人工，不得换 UA/走代理
    ErrorClass.AUTH_403: _p(
        retryable=False, max_attempts=1, count_as_failure=True, alert_level="critical"
    ),
    # 11. robots 拒绝：立即终止该源，不可配置绕过
    ErrorClass.ROBOTS_DENIED: _p(
        retryable=False, max_attempts=1, count_as_failure=False, alert_level="critical"
    ),
    # 12. JSON 解析失败：不指数（固定 2.0s * n），≤2 次，之后落样本
    ErrorClass.JSON_DECODE: _p(
        retryable=True, max_attempts=3, count_as_failure=True, alert_level="critical"
    ),
    # 13. Schema 校验失败：隔离该 item，不阻断整轮
    ErrorClass.SCHEMA_INVALID: _p(
        retryable=False, max_attempts=1, count_as_failure=True, alert_level="critical"
    ),
    # 14. 分页漂移：数据层面处理，不重试已成功的页
    ErrorClass.PAGINATION_DRIFT: _p(
        retryable=False, max_attempts=1, count_as_failure=False, alert_level="warn"
    ),
    # 15. 去重冲突：合法情况，仅计数
    ErrorClass.DEDUPE_CONFLICT: _p(
        retryable=False, max_attempts=1, count_as_failure=False, alert_level="none"
    ),
    # 16. 一致性失败：落 data_reject + 隔离快照，不进异动检测
    ErrorClass.CONSISTENCY_FAILED: _p(
        retryable=False, max_attempts=1, count_as_failure=False, alert_level="warn"
    ),
    # 17. SSL 校验失败：修环境，禁止 verify=False
    ErrorClass.SSL_INVALID: _p(
        retryable=False, max_attempts=1, count_as_failure=True, alert_level="critical"
    ),
    # 18. 304：条件请求正常路径，计成功
    ErrorClass.NOT_MODIFIED: _p(
        retryable=False, max_attempts=1, count_as_failure=False, alert_level="none"
    ),
    # 19. 任务取消：必须原样传播
    ErrorClass.CANCELLED: _p(
        retryable=False, max_attempts=1, count_as_failure=False, alert_level="none"
    ),
    # 20. 未知异常：默认不重试，宁可丢一轮也不触发不可控行为
    ErrorClass.UNKNOWN: _p(
        retryable=False, max_attempts=1, count_as_failure=True, alert_level="critical"
    ),
}

#: 命中即「停机级」：orchestrator 见到这些分类会终止该源本轮并告警
TERMINAL_ERRORS: Final[frozenset[ErrorClass]] = frozenset(
    {
        ErrorClass.ROBOTS_DENIED,
        ErrorClass.AUTH_403,
        ErrorClass.SSL_INVALID,
    }
)


def policy_for(err: ErrorClass) -> AttemptPolicy:
    """查表；未登记的分类一律按 UNKNOWN 处置（兜底保守）。"""
    return POLICY.get(err, POLICY[ErrorClass.UNKNOWN])


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------
class MarketWatchError(Exception):
    """全部自定义异常的基类。"""

    error_class: ErrorClass = ErrorClass.UNKNOWN


class PayloadError(MarketWatchError):
    """响应体无法解析（JSON / XML）。对应 ErrorClass.JSON_DECODE。"""

    error_class = ErrorClass.JSON_DECODE


class SchemaError(MarketWatchError):
    """字段缺失或类型不符。对应 ErrorClass.SCHEMA_INVALID。"""

    error_class = ErrorClass.SCHEMA_INVALID


class RobotsDenied(MarketWatchError):
    """robots.txt 不允许该 path。不可配置绕过（C2）。"""

    error_class = ErrorClass.ROBOTS_DENIED

    def __init__(self, host: str, path: str) -> None:
        super().__init__(f"robots.txt 拒绝: {host}{path}")
        self.host = host
        self.path = path


class ReadOnlyViolation(MarketWatchError):
    """出网方法白名单违规（C4）：只允许 GET/HEAD 且无 body。"""

    error_class = ErrorClass.UNKNOWN

    def __init__(self, method: str, has_body: bool) -> None:
        detail = "带 body" if has_body else ""
        super().__init__(f"只读约束违反: method={method}{detail}（仅允许 GET/HEAD 且无 body）")
        self.method = method
        self.has_body = has_body


class OpenCircuit(MarketWatchError):
    """熔断器处于 open，请求被短路。"""

    error_class = ErrorClass.UNKNOWN

    def __init__(self, host: str, until_s: float) -> None:
        super().__init__(f"熔断开启: host={host} 剩余 {until_s:.1f}s")
        self.host = host
        self.until_s = until_s


class PolicyViolation(MarketWatchError):
    """合规检查点（C1/C5/C6/C7）失败。"""

    error_class = ErrorClass.UNKNOWN


class SourceUnavailable(MarketWatchError):
    """数据源本轮不可用（robots / 连续失败 / 停机级错误）。"""

    error_class = ErrorClass.UNKNOWN


class BudgetExhausted(MarketWatchError):
    """单请求累计耗时预算（TOTAL_ATTEMPT_BUDGET_S）用尽。"""

    error_class = ErrorClass.UNKNOWN
