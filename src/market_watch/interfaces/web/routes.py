# src/market_watch/interfaces/web/routes.py
"""页面 + JSON API。

架构依据：docs/01-architecture.md §13（看板 JSON API 清单）。

降采样在**服务端**完成（≤2000 点），浏览器端只负责画。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ...domain.models import utc_now
from ...infrastructure.storage.repositories import Repository
from ...logging_setup import get_logger

__all__ = ["render_dashboard", "router"]

_log = get_logger("market_watch.web.routes")

router = APIRouter(prefix="/api")


def _repo(request: Request) -> Repository:
    """每次请求开一条只读连接并即时关闭（看板 QPS 极低，无需池化）。"""
    settings = request.app.state.runtime.settings
    return Repository(settings.db_path)


def _with_repo[T](fn: Callable[[Repository], T]) -> Callable[[Request], T]:
    """给每个 handler 包一层「开只读连接 → 调用 → 关连接」。

    `fn` 的首参固定为 `Repository`，返回类型透传给 `wrapper`——否则 `_with_repo(...)()`
    的返回会被推成 `Any`，各 route 的 `-> dict[str, Any]` 就变成 `Returning Any`。
    """

    def wrapper(request: Request) -> T:
        repo = _repo(request)
        try:
            return fn(repo)
        finally:
            repo.aclose()

    return wrapper


# ===========================================================================
# JSON API（§13 清单）
# ===========================================================================
@router.get("/summary")
def api_summary(request: Request) -> dict[str, Any]:
    return _with_repo(lambda repo: repo.summary())(request)


@router.get("/search")
def api_search(
    request: Request, q: str = Query("", min_length=0), limit: int = Query(20, ge=1, le=100)
):
    if not q.strip():
        return []
    rows = _with_repo(lambda repo: repo.search_items(q, limit))(request)
    return [{"item_id": r["item_id"], "name": r["name"], "item_type": r["item_type"]} for r in rows]


@router.get("/series")
def api_series(
    request: Request,
    item_id: int = Query(..., ge=1),
    table: str = Query("price_change", pattern="^(price_change|price_hourly)$"),
    since_hours: int = Query(24, ge=1, le=24 * 365),
    points: int = Query(2000, ge=10, le=2000),
) -> dict[str, Any]:
    since = utc_now() - timedelta(hours=since_hours)
    rows = _with_repo(lambda repo: repo.series(item_id, table, since, points))(request)
    return {
        "item_id": item_id,
        "table": table,
        "ts": [r["ts"] for r in rows],
        "sell": [r["sell"] for r in rows],
        "buy": [r["buy"] for r in rows],
        "points": len(rows),
    }


@router.get("/alerts")
def api_alerts(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    unack: int = Query(0, ge=0, le=1),
) -> list[dict[str, Any]]:
    rows = _with_repo(lambda repo: repo.alerts(limit, bool(unack)))(request)
    return [dict(r) for r in rows]


@router.get("/announcements")
def api_announcements(
    request: Request, limit: int = Query(50, ge=1, le=500)
) -> list[dict[str, Any]]:
    rows = _with_repo(lambda repo: repo.announcements(limit))(request)
    return [dict(r) for r in rows]


@router.get("/health")
def api_health(request: Request) -> dict[str, Any]:
    runtime = request.app.state.runtime
    health = _with_repo(lambda repo: repo.health())(request)
    return {
        "db_size_mb": health.db_size_mb,
        "last_cycle": health.last_cycle,
        "items_total": health.items_total,
        "alerts_24h": health.alerts_24h,
        "rejects_24h": health.rejects_24h,
        "limiter": [
            {
                "host": s.host,
                "current_rps": round(s.current_rps, 3),
                "tokens": round(s.tokens, 2),
                "consumed_total": s.consumed_total,
                "throttled_total": s.throttled_total,
            }
            for s in runtime.limiter.all_snapshots()
        ],
        "breaker": dict(runtime.breaker.snapshot()),
        "pool": {h: runtime.fetcher._pool.stats(h) for h in runtime.fetcher._pool.hosts()},
        "writer": runtime.writer.stats,
        "metrics": runtime.metrics.snapshot(),
    }


@router.get("/quality")
def api_quality(request: Request) -> dict[str, Any]:
    return _with_repo(lambda repo: repo.quality())(request)


@router.get("/latest")
def api_latest(request: Request, limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    rows = _with_repo(lambda repo: repo.latest_snapshot(limit=limit))(request)
    return [dict(r) for r in rows]


@router.post("/alerts/{alert_id}/ack")
def api_ack(request: Request, alert_id: str) -> dict[str, Any]:
    """本地信任模型（§14 P2，无鉴权）。"""
    import sqlite3

    settings = request.app.state.runtime.settings
    conn = sqlite3.connect(str(settings.db_path), timeout=5.0)
    try:
        cur = conn.execute("UPDATE alert_event SET acknowledged=1 WHERE alert_id=?", (alert_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="alert_id 不存在")
        return {"ok": True, "alert_id": alert_id}
    finally:
        conn.close()


# ===========================================================================
# 页面
# ===========================================================================
def render_dashboard(settings) -> str:  # noqa: ANN001, ANN202
    """Jinja2 单模板渲染。ECharts 已 vendored（离线可用，禁止 CDN）。"""
    from pathlib import Path

    from fastapi.templating import Jinja2Templates

    templates_dir = Path(__file__).resolve().parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))
    env = templates.env
    template = env.get_template("dashboard.html")
    return template.render(
        {
            "request": None,
            "version": settings.version,
            "contact_url": settings.contact_url,
            "rps_cap": round(settings.per_host_rps, 2),
            "host": settings.web_host,
            "port": settings.web_port,
        }
    )
