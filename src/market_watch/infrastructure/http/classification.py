# src/market_watch/infrastructure/http/classification.py
"""`(exception | ResponseView) -> ErrorClass` 的**唯一出口**。

架构依据：docs/01-architecture.md §6（「retry / breaker / metrics / 告警四处的判定
全部读这一个出口，禁止各处自己判 `status_code == 429`」）。
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Final

from ...domain.errors import ErrorClass, PayloadError, SchemaError
from ...domain.models import ResponseView

__all__ = [
    "VENDOR_RATE_LIMIT_MARKERS",
    "classify",
    "classify_exc",
    "classify_response",
]

#: body 里出现这些关键字（大小写不敏感）→ 视为 vendor 自定义限流码（§6 #6）
VENDOR_RATE_LIMIT_MARKERS: Final[tuple[str, ...]] = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "throttl",
    "quota exceeded",
    "slow down",
)


def _module_names(exc: BaseException) -> set[str]:
    """收集异常链上所有类名与模块名，用于不依赖 import 的判定。

    这样即便 httpx 未安装（纯 domain 单测），本模块也能被 import 且行为一致。
    """
    names: set[str] = set()
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 16:
        names.add(type(cur).__name__)
        names.add(type(cur).__module__)
        cause = cur.__cause__ or cur.__context__
        cur = cause if isinstance(cause, BaseException) else None
        seen += 1
    return names


def classify_exc(exc: BaseException) -> ErrorClass:
    """异常 → ErrorClass。用类名/模块名判定，避免硬 import httpx 到 domain 层。"""
    if isinstance(exc, asyncio.CancelledError):
        return ErrorClass.CANCELLED
    if isinstance(exc, PayloadError):
        return ErrorClass.JSON_DECODE
    if isinstance(exc, SchemaError):
        return ErrorClass.SCHEMA_INVALID
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError)):
        return ErrorClass.SSL_INVALID

    names = _module_names(exc)
    joined = " ".join(names).lower()

    if "cancellederror" in joined:
        return ErrorClass.CANCELLED
    if "remoteprotocolerror" in joined:
        return ErrorClass.REMOTE_PROTOCOL
    if "pooltimeout" in joined:
        return ErrorClass.POOL_TIMEOUT
    if "connecttimeout" in joined:
        return ErrorClass.CONNECT_TIMEOUT
    if "readtimeout" in joined:
        return ErrorClass.READ_TIMEOUT
    if "timeout" in joined and "connect" in joined:
        return ErrorClass.CONNECT_TIMEOUT
    if "connecterror" in joined:
        # DNS 失败的链路里带 gaierror / socket.gaierror（§6 #1）
        if "gaierror" in joined or "getaddrinfo" in joined or "dns" in joined:
            return ErrorClass.DNS_FAILURE
        return ErrorClass.DNS_FAILURE
    if "ssl" in joined or "certificate" in joined:
        return ErrorClass.SSL_INVALID
    if "gaierror" in joined:
        return ErrorClass.DNS_FAILURE
    if "httpx" in joined or "httpcore" in joined:
        return ErrorClass.UNKNOWN
    return ErrorClass.UNKNOWN


def classify_response(view: ResponseView) -> ErrorClass:
    """ResponseView → ErrorClass。

    404 在 §6 矩阵里没有专门分类：官方端点的「不存在」由 adapter 用空结果处理，
    落到 UNKNOWN（默认不重试，§6 #18）。
    """
    code = view.status_code
    if 200 <= code < 300:
        return ErrorClass.OK
    if code == 304:
        return ErrorClass.NOT_MODIFIED
    if code in (401, 403):
        return ErrorClass.AUTH_403
    if code == 420:
        return ErrorClass.RATE_LIMITED_VENDOR
    if code == 429:
        return ErrorClass.RATE_LIMITED_429
    if 500 <= code < 600:
        return ErrorClass.SERVER_5XX
    return ErrorClass.UNKNOWN


def classify(exc_or_view: BaseException | ResponseView) -> ErrorClass:
    """按入参类型分派到 `classify_exc` / `classify_response`。

    Args:
        exc_or_view: 异常对象或响应快照。

    Returns:
        对应的 `ErrorClass`；无法判定一律 `UNKNOWN`（默认不重试，宁可丢一轮）。

    Raises:
        不抛：`ResponseView.json()` 抛的 `PayloadError` 会被 `classify_exc`
        正确映射成 `JSON_DECODE`，不需要调用方兜底。
    """
    if isinstance(exc_or_view, BaseException):
        return classify_exc(exc_or_view)
    return classify_response(exc_or_view)
