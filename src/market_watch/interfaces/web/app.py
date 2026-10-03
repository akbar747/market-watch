# src/market_watch/interfaces/web/app.py
"""FastAPI 工厂 + lifespan（启停共用一个 Orchestrator）。

架构依据：docs/01-architecture.md §2、§13（看板 JSON API）、§14 P2（默认 bind 127.0.0.1）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ...application.config import Settings
from ...logging_setup import configure_logging, get_logger
from ..wiring import build_runtime
from .routes import router

__all__ = ["create_app"]

_log = get_logger("market_watch.web")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(json_logs=settings.log_json, level=settings.log_level)
    runtime = await build_runtime(settings)
    app.state.runtime = runtime
    # ★ 打**实际生效**的 host/port，不是 settings 里的默认值：
    #   `market-watch serve --port 8765` 时 uvicorn 确实绑 8765，但这里原来读的是
    #   settings.web_port(=8000)，日志会谎报 8000，运维照着日志去开 8000 会扑空。
    #   （2026-10-03 发布冒烟实测复现：日志 port=8000 / uvicorn running on 8765。）
    _log.info(
        "web.started host=%s port=%s",
        getattr(app.state, "web_host", settings.web_host),
        getattr(app.state, "web_port", settings.web_port),
    )
    try:
        yield
    finally:
        await runtime.aclose()
        _log.info("web.stopped")


def create_app(settings: Settings, *, host: str | None = None, port: int | None = None) -> FastAPI:
    """造看板 app。

    Args:
        settings: 运行配置。
        host: **实际生效**的绑定地址；None 表示用 `settings.web_host`。
        port: **实际生效**的绑定端口；None 表示用 `settings.web_port`。

    `host` / `port` 必须显式传 CLI 覆盖后的值，否则 `web.started` 日志会报默认值。
    """
    app = FastAPI(
        title="market-watch",
        version=settings.version,
        description="合规版公开数据物价看板（数据源：Guild Wars 2 官方 v2 API，免鉴权、只读）",
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.web_host = host or settings.web_host
    app.state.web_port = port or settings.web_port

    static_dir = _static_dir()
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def _index() -> HTMLResponse:
        from .routes import render_dashboard

        return HTMLResponse(render_dashboard(app.state.runtime.settings))

    @app.get("/healthz", include_in_schema=False)
    async def _healthz() -> dict[str, Any]:
        return {"ok": True}

    return app


def _static_dir():
    from pathlib import Path

    return Path(__file__).resolve().parent / "static"
