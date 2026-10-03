# src/market_watch/interfaces/web/deps.py
"""依赖 wiring：把 Settings / 只读连接 / Orchestrator 注入路由。

架构依据：docs/01-architecture.md §2（`deps.py` 注释）。

本仓看板 QPS 极低，读侧不池化：每次请求开一条 `mode=ro` 连接并即时关闭。
`Orchestrator` 是**进程级单例**，由 lifespan 创建（见 `app.py`）。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ...application.config import Settings
from ...infrastructure.storage.repositories import Repository
from ..wiring import Runtime

__all__ = ["get_repo", "get_runtime", "get_settings", "repo_dependency"]


def get_settings(request: Request) -> Settings:
    return request.app.state.runtime.settings  # type: ignore[no-any-return]


def get_runtime(request: Request) -> Runtime:
    runtime: Any = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise RuntimeError("Runtime 未就绪：lifespan 尚未启动（用 TestClient 的 with 上下文）")
    return runtime  # type: ignore[no-any-return]


def get_repo(request: Request) -> Repository:
    runtime = get_runtime(request)
    return Repository(runtime.settings.db_path)


def repo_dependency() -> Any:
    """FastAPI `Depends()` 形式。路由里显式开关更直观，故默认仍用 `get_repo`。"""
    from fastapi import Depends  # 局部导入：仅在需要 Depends 时触碰 FastAPI

    return Depends(get_repo)
