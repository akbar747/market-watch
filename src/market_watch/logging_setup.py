# src/market_watch/logging_setup.py
"""structlog 结构化 JSON 初始化 + 敏感字段脱敏处理器。

架构依据：docs/01-architecture.md §2（`logging_setup.py` 注释）、
技术底座「服务向 structlog（JSON 结构化）」「脚本向 loguru」。

安全约束（技术底座 §安全）：
    Cookie / Token / 代理口令一律走环境变量，且**不得进日志**。本模块的
    `_scrub_processor` 对已知敏感键与值做脱敏，双保险。
"""

from __future__ import annotations

from collections.abc import MutableMapping
import logging
import os
import sys
from typing import Any, Final

import structlog

__all__ = [
    "SENSITIVE_KEYS",
    "bind_context",
    "configure_logging",
    "get_logger",
]

#: 命中这些 key 的字段值一律替换为 '***'
SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "proxy_url",
        "password",
        "token",
        "api_key",
        "apikey",
        "secret",
        "cookie",
        "set-cookie",
        "x-api-key",
        "mw_proxy_url",
    }
)

_SCRUBBED: Final[str] = "***"


def _scrub_processor(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor：脱敏 + 剔除 None 值（让 JSON 更紧凑）。"""
    for key in list(event_dict.keys()):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = _SCRUBBED
    # 环境变量里的真实代理口令若意外出现在 message 中，也脱掉
    secret_values = [
        v
        for k, v in os.environ.items()
        if k.lower() in {"https_proxy", "http_proxy", "all_proxy"} and "://" in v and "@" in v
    ]
    if secret_values:
        msg = event_dict.get("event")
        if isinstance(msg, str):
            for secret in secret_values:
                if secret in msg:
                    msg = msg.replace(secret, _SCRUBBED)
            event_dict["event"] = msg
    return {k: v for k, v in event_dict.items() if v is not None}


_CONFIGURED: bool = False


def configure_logging(*, json_logs: bool = True, level: str = "INFO") -> None:
    """幂等地初始化 structlog。重复调用只更新 level。"""
    global _CONFIGURED
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _scrub_processor,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    if not _CONFIGURED:
        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(
                getattr(logging, level.upper(), logging.INFO)
            ),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
            cache_logger_on_first_use=True,
        )
        _CONFIGURED = True
    else:  # pragma: no cover - 仅测试里会重复调用
        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(
                getattr(logging, level.upper(), logging.INFO)
            ),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
            cache_logger_on_first_use=True,
        )


def get_logger(name: str = "market_watch") -> Any:
    """返回一个 structlog 绑定 logger。"""
    return structlog.get_logger(name)


def bind_context(**kwargs: Any) -> None:
    """把 cycle_id / source_id 等上下文绑到 contextvars，后续日志自动带上。"""
    structlog.contextvars.bind_contextvars(**kwargs)
