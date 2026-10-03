# tests/conftest.py
"""fixtures 分层：session 级 mock transport / function 级临时 DB。

架构依据：docs/01-architecture.md §9.1。

★ 防出网护栏（CI 必须有一条）：autouse session fixture 让 `socket.socket.connect`
抛 `NetworkAccessDenied`，任何测试里的真实网络调用立即失败。
"""

from __future__ import annotations

from pathlib import Path
import socket
from typing import Any, Iterator

import pytest

FIXTURES_HTTP = Path(__file__).resolve().parent / "fixtures" / "http"
ADAPTER_FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "market_watch"
    / "infrastructure"
    / "adapters"
    / "fixtures"
)


class NetworkAccessDenied(RuntimeError):
    """测试中试图出网时抛出。"""


#: loopback 放行：asyncio 的 self-pipe（Windows Proactor 用 socketpair）与
#: 本地 mock server 都要用 127.0.0.1；拦掉它们会让事件循环根本起不来。
#: 拦截的语义目标是「禁止访问公网/官方端点」，不是禁止本地回环。
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost", ""})


@pytest.fixture(autouse=True, scope="session")
def _no_network() -> Iterator[None]:
    original = socket.socket.connect

    def _blocked(self, *args, **kwargs):  # noqa: ANN001, ANN002
        target = args[0] if args else kwargs.get("address")
        host = ""
        if isinstance(target, tuple) and target:
            host = str(target[0])
        elif isinstance(target, str):
            host = target
        if host in _LOOPBACK_HOSTS:
            return original(self, *args, **kwargs)  # type: ignore[operator]
        raise NetworkAccessDenied(
            f"测试禁止出网（目标 {host!r}）：请使用 httpx.MockTransport 或 respx，"
            "见 docs/01-architecture.md §9.1"
        )

    socket.socket.connect = _blocked  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = original  # type: ignore[method-assign]


@pytest.fixture(scope="session")
def http_fixtures() -> "FixtureRouter":
    """读 `tests/fixtures/http/*` 与 `adapters/fixtures/*`，构建 URL → payload 路由。"""
    return FixtureRouter()


class FixtureRouter:
    """把录制样本映射成 httpx.MockTransport 路由（§9.1）。"""

    def __init__(self) -> None:
        self._json: dict[str, Any] = {}
        self._text: dict[str, str] = {}
        for directory in (FIXTURES_HTTP, ADAPTER_FIXTURES):
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                if path.name.endswith(".headers.json"):
                    continue
                self._json[path.name] = path.read_bytes()
            for path in sorted(directory.glob("*.xml")):
                self._text[path.name] = path.read_text(encoding="utf-8")

    def json_bytes(self, name: str) -> bytes:
        return self._json[name]

    def text(self, name: str) -> str:
        return self._text[name]

    def has(self, name: str) -> bool:
        return name in self._json or name in self._text

    def price_pages(self, *, pages: int = 3, per_page: int = 200) -> list[dict]:
        """生成 `pages` 页合成价格数据（结构对齐真实样本）。"""
        import json

        base = json.loads(self._json["gw2_prices_page1.json"].decode("utf-8"))
        out = []
        for page in range(pages):
            rows = []
            for i in range(per_page):
                src = base[i % len(base)]
                rows.append(
                    {
                        "id": src["id"] + page * per_page + i,
                        "whitelisted": src.get("whitelisted", False),
                        "buys": dict(src.get("buys") or {}),
                        "sells": dict(src.get("sells") or {}),
                    }
                )
            out.append(rows)
        return out


@pytest.fixture
def fake_clock():
    """可推进的单调时钟（`loop.time()` 风格的 mock clock）。"""
    return _FakeClock()


class _FakeClock:
    __slots__ = ("now",)

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_sleep(fake_clock):
    """不真等的 sleep：推进假时钟即可。"""
    calls: list[float] = []

    async def _sleep(seconds: float) -> None:
        calls.append(seconds)
        fake_clock.advance(seconds)

    _sleep.calls = calls  # type: ignore[attr-defined]
    return _sleep


@pytest.fixture
def sqlite_conn(tmp_path):
    """跑 0001_init.sql 的空库，PRAGMA 与生产一致。"""
    import sqlite3

    from market_watch.infrastructure.storage.sqlite import PRAGMAS, apply_migrations

    conn = sqlite3.connect(str(tmp_path / "test.db"), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        conn.execute(pragma)
    apply_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def settings(tmp_path):
    """测试用 Settings：DB 落 tmp_path，rps 用满上限（仍受 C3 钳制）。"""
    from market_watch.application.config import Settings

    return Settings(
        db_path=tmp_path / "market_watch.db",
        rejects_dir=tmp_path / "rejects",
        pending_dir=tmp_path / "pending",
        config_dir=Path(__file__).resolve().parents[1] / "config",
        contact_url="https://you.example/contact",
        log_json=False,
    )
