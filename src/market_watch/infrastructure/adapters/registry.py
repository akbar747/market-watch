# src/market_watch/infrastructure/adapters/registry.py
"""adapter 注册表 + 契约自检入口。

架构依据：docs/01-architecture.md §2、§9.2（契约测试对 `all_adapters()` 参数化跑）。

**否决源登记表（§1.4）在此处显式留痕**：
    `esi.evetech.net`（EVE ESI）因其 `robots.txt` = `Disallow: /` 被判定否决，
    **不得**在本注册表里出现。若要启用，必须先取得运营方书面授权并在
    `docs/04-compliance.md` 登记 + `RobotsGuard` 显式放行（代码变更 + PR 评审，
    不允许环境变量开关）。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Final

from ...domain.models import Politeness
from ...domain.ports import SourceAdapter
from .gw2_items import Gw2ItemsAdapter
from .gw2_listings import Gw2ListingsAdapter
from .gw2_prices import Gw2PricesAdapter
from .rss_generic import RssAdapter

__all__ = [
    "AUTHORIZED_KINDS",
    "REJECTED_SOURCES",
    "AdapterRegistry",
    "all_adapters",
    "build_default_registry",
    "get_adapter",
]

#: §1.4 否决源登记表（禁止静默加回）
REJECTED_SOURCES: Final[Mapping[str, str]] = {
    "esi.evetech.net": "robots.txt = 'User-agent: * / Disallow: /' → 命中 C2 红线；需书面授权方可启用",
    "any_login_walled": "需登录/付费墙/验证码 → 红线否决",
    "any_third_party_mirror": "非官方镜像/第三方黑盒聚合 → 数据可追溯性不可证明，且存在转售风险",
}

#: 本仓允许的授权来源判定值（C9 归档用）
AUTHORIZED_KINDS: Final[frozenset[str]] = frozenset({"official_public"})


class AdapterRegistry:
    """source_id → adapter。进程内单例式，构造期即完成合规钳制检查。"""

    __slots__ = ("_adapters", "_source_kind")

    def __init__(self, adapters: Sequence[SourceAdapter]) -> None:
        self._adapters: dict[str, SourceAdapter] = {}
        for adapter in adapters:
            if adapter.source_id in self._adapters:
                raise ValueError(f"source_id 重复: {adapter.source_id!r}")
            if adapter.host in REJECTED_SOURCES or any(
                reject in adapter.host for reject in ("esi.evetech.net",)
            ):
                raise ValueError(
                    f"否决源禁止注册: {adapter.host} —— {REJECTED_SOURCES.get(adapter.host)}"
                )
            self._adapters[adapter.source_id] = adapter
        self._source_kind: dict[str, str] = dict.fromkeys(self._adapters, "official_public")

    def __iter__(self) -> Iterator[SourceAdapter]:
        return iter(self._adapters.values())

    def __len__(self) -> int:
        return len(self._adapters)

    def get(self, source_id: str) -> SourceAdapter:
        try:
            return self._adapters[source_id]
        except KeyError:
            raise KeyError(f"未注册的 source_id: {source_id!r}") from None

    def source_ids(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def host_of(self, source_id: str) -> str:
        return self.get(source_id).host

    def hosts(self) -> tuple[str, ...]:
        return tuple({a.host for a in self._adapters.values()})

    def host_map(self) -> dict[str, str]:
        return {sid: a.host for sid, a in self._adapters.items()}

    def politeness_map(self) -> dict[str, Politeness]:
        return {sid: a.politeness for sid, a in self._adapters.items()}

    def kind_of(self, source_id: str) -> str:
        return self.get(source_id).kind

    def authorization_of(self, source_id: str) -> str:
        """C9：每个源的授权来源判定。本仓全部为 `official_public`。"""
        return self._source_kind[source_id]


def build_default_registry(
    *,
    page_size: int = 200,
    watchlist: Sequence[int] = (),
    item_ids: Sequence[int] = (),
    version: str = "0.1.0",
    contact_url: str = "https://you.example/contact",
) -> AdapterRegistry:
    """默认四个官方公开源。全部 `authorization = official_public`（免鉴权、只读 GET）。"""
    return AdapterRegistry(
        [
            Gw2PricesAdapter(page_size=page_size, version=version, contact_url=contact_url),
            Gw2ListingsAdapter(watchlist=watchlist, version=version, contact_url=contact_url),
            Gw2ItemsAdapter(item_ids=item_ids, version=version, contact_url=contact_url),
            RssAdapter(version=version, contact_url=contact_url),
        ]
    )


_REGISTRY: AdapterRegistry | None = None


def all_adapters(**kwargs: object) -> tuple[SourceAdapter, ...]:
    """契约测试的入口：返回全部已注册 adapter。"""
    registry = build_default_registry(**kwargs)  # type: ignore[arg-type]
    return tuple(registry)


def get_adapter(source_id: str, **kwargs: object) -> SourceAdapter:
    return build_default_registry(**kwargs).get(source_id)  # type: ignore[arg-type]
