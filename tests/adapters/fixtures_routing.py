# tests/adapters/fixtures_routing.py
"""把 JSON/XML 样本映射成 httpx.MockTransport 路由。

架构依据：docs/01-architecture.md §9.1。

**禁止 monkeypatch `httpx.AsyncClient.get`**（§9.3）——那测不到 transport 层的
重试与连接池逻辑。一律用 `httpx.MockTransport` / `respx`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx

ADAPTER_FIXTURES: Path = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "market_watch"
    / "infrastructure"
    / "adapters"
    / "fixtures"
)

__all__ = [
    "FixtureRouter",
    "build_transport",
    "price_page_response",
    "make_view",
    "MALICIOUS_EVER_PAGINATED",
]


class FixtureRouter:
    """URL → (status, headers, body) 的路由表。"""

    def __init__(self) -> None:
        self._json: dict[str, bytes] = {}
        self._text: dict[str, str] = {}
        for path in sorted(ADAPTER_FIXTURES.glob("*.json")):
            if path.name.endswith(".headers.json"):
                continue
            self._json[path.name] = path.read_bytes()
        for path in sorted(ADAPTER_FIXTURES.glob("*.xml")):
            self._text[path.name] = path.read_text(encoding="utf-8")

    def json_body(self, name: str) -> bytes:
        return self._json[name]

    def text_body(self, name: str) -> str:
        return self._text[name]


def price_page_response(page: int = 0, per_page: int = 200, page_total: int = 3) -> httpx.Response:
    """合成一页价格数据（结构对齐真实录制样本）。"""
    base = json.loads((ADAPTER_FIXTURES / "gw2_prices_page1.json").read_bytes().decode("utf-8"))
    rows = []
    for i in range(per_page):
        src = base[i % len(base)]
        rows.append(
            {
                "id": src["id"] + page * per_page,
                "whitelisted": src.get("whitelisted", False),
                "buys": dict(src.get("buys") or {}),
                "sells": dict(src.get("sells") or {}),
            }
        )
    return httpx.Response(
        200,
        json=rows,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "public, max-age=120",
            "X-Page-Total": str(page_total),
            "X-Page-Size": str(per_page),
            "X-Result-Total": str(page_total * per_page),
            "X-Result-Count": str(per_page),
            "X-Rate-Limit-Limit": "600",
        },
    )


def build_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def make_view(
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    content: bytes = b"[]",
    url: str = "https://api.guildwars2.com/v2/commerce/prices?page=0",
) -> Any:
    """构造 `ResponseView`（domain 层不 import httpx，因此返回 Any）。"""
    from market_watch.domain.models import ResponseView

    return ResponseView(
        status_code=status_code,
        headers=headers or {},
        content=content,
        url=url,
        elapsed_ms=1.0,
    )


#: 恶意 mock 的标记：永远返回同样的 X-Page-Total 且永远有数据（测终止性）
MALICIOUS_EVER_PAGINATED: str = "malicious_ever_paginated"
