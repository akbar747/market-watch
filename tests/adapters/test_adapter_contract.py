# tests/adapters/test_adapter_contract.py
"""§9.2 契约测试清单：对 `registry.all_adapters()` 里的**每一个** adapter 参数化跑 9 条。

1. `politeness.rps <= 有效上限`
2. `politeness.min_interval_seconds >= 0` 且 `respect_max_age` 为 True（RSS 例外，见注）
3. `host` 是单个 hostname（不含 scheme / path / 端口）
4. `user_agent_suffix()` 非空、含版本号、含联系 URI
5. **分页终止性**：注入「永不分页」恶意 mock，`next_tasks` 必须有限步收敛
6. **纯函数性**：同一 `(task, view)` 调两次 `parse`，结果深度相等
7. **容错性**：1 条坏记录的全页，`parse` 不得抛异常，坏记录必须进 `consistency_failures`
8. **只读性**：`initial_tasks` 产出 task `method ∈ {"GET","HEAD"}` 且无 body
9. **robots 一致性**：所有 task 的 path 必须通过 `RobotsGuard`

注：第 2 条对 `gw2_news_rss` 放宽 `respect_max_age`——RSS 端点实测不返回
`Cache-Control`，架构 §5.4 让它按配置周期（900s）走。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

import pytest

from market_watch.application.config import EFFECTIVE_RPS_CAP
from market_watch.domain.models import FetchTask, ResponseView
from market_watch.domain.policy import ALLOWED_METHODS
from market_watch.infrastructure.adapters.registry import all_adapters
from market_watch.infrastructure.http.robots import RobotsGuard, RobotsRecord

from .fixtures_routing import ADAPTER_FIXTURES

FIXED_NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
CYCLE_ID = "test-cycle-0001"

#: 需要清单才能产出任务的 adapter → 预置样本 id
WATCHLIST = (24, 68)
ITEM_IDS = (24, 68)


def _adapters():
    return all_adapters(watchlist=WATCHLIST, item_ids=ITEM_IDS)


ADAPTER_IDS = [a.source_id for a in _adapters()]


def _param():
    return pytest.mark.parametrize("adapter", _adapters(), ids=ADAPTER_IDS)


def _stamp(task: FetchTask) -> FetchTask:
    from dataclasses import replace

    return replace(task, observed_at=FIXED_NOW, cycle_id=CYCLE_ID)


# ===========================================================================
# 1. rps 不得突破合规上限
# ===========================================================================
@_param()
def test_contract_1_rps_within_cap(adapter) -> None:
    assert (
        adapter.politeness.rps <= EFFECTIVE_RPS_CAP
    ), f"{adapter.source_id} 声明 rps={adapter.politeness.rps} 超过合规上限 {EFFECTIVE_RPS_CAP}"


# ===========================================================================
# 2. min_interval >= 0 且（除 RSS 外）respect_max_age
# ===========================================================================
@_param()
def test_contract_2_min_interval_and_max_age(adapter) -> None:
    assert adapter.politeness.min_interval_seconds >= 0
    if adapter.kind == "announcement":
        return  # RSS 不返回 Cache-Control，按配置周期走（§5.4）
    assert adapter.politeness.respect_max_age is True


# ===========================================================================
# 3. host 是单个 hostname
# ===========================================================================
@_param()
def test_contract_3_single_hostname(adapter) -> None:
    host = adapter.host
    assert host, "host 不得为空"
    assert "://" not in host, f"host 不得含 scheme: {host}"
    assert "/" not in host, f"host 不得含 path: {host}"
    assert "?" not in host and "#" not in host
    assert "," not in host, "host 不得是列表"
    assert re.match(r"^[A-Za-z0-9.\-]+$", host), f"host 不是合法 hostname: {host}"


# ===========================================================================
# 4. UA 声明
# ===========================================================================
@_param()
def test_contract_4_user_agent_suffix(adapter) -> None:
    ua = adapter.user_agent_suffix()
    assert ua, "UA 后缀不得为空"
    assert re.match(r"^market-watch/\d+\.\d+\.\d+ \(\+https?://", ua), f"UA 格式不符: {ua}"
    assert "you.example" in ua or "http" in ua


# ===========================================================================
# 5. 分页终止性（★ 恶意 mock：永远同样的 X-Page-Total 且永远有数据）
# ===========================================================================
@_param()
def test_contract_5_pagination_terminates(adapter) -> None:
    """注入「永不分页」的恶意响应，断言 `next_tasks` 在有限步内返回 ()。

    恶意响应特征：`X-Page-Total` 恒为 3（永远不会让 page+1 >= total 自然成立到
    足够大），且 body 恒非空。adapter 自身必须严格按 page 号递增推进，
    由调用方 `max_pages` 硬护栏兜底收敛。
    """
    from market_watch.domain.models import FetchTask as FT

    task = _stamp(
        FT(
            task_id="probe:page=0",
            source_id=adapter.source_id,
            host=adapter.host,
            method="GET",
            path=_probe_path(adapter),
            params={"page": "0"},
            strategy="paged",
        )
    )
    total = adapter.politeness.concurrency * 5 + 5  # 允许步数上界
    seen: set[str] = set()

    for _ in range(total + 5):  # 迭代上界即可，`step` 不参与断言（收敛见下 `return`）
        view = _malicious_view(adapter, task)
        nxt = adapter.next_tasks(task, view)
        if not nxt:
            return  # ★ 收敛
        assert len(nxt) >= 1
        task = _stamp(nxt[0])
        key = str(dict(task.params).get("page", task.path))
        assert key not in seen, f"{adapter.source_id} 分页原地打转，重复推进到 {key}"
        seen.add(key)
    pytest.fail(f"{adapter.source_id} 在 {total + 5} 步内未收敛（终止性违反）")


def _probe_path(adapter) -> str:
    return {
        "price": "/v2/commerce/prices",
        "orderbook": "/v2/commerce/listings/24",
        "item_meta": "/v2/items",
        "announcement": "/en/feed/",
    }[adapter.kind]


def _malicious_view(adapter, task: FetchTask) -> ResponseView:
    """恶意响应：永远同样的 X-Page-Total + 永远有数据。"""
    if adapter.kind == "price":
        return ResponseView(
            status_code=200,
            headers={
                "X-Page-Total": "3",
                "X-Page-Size": "200",
                "X-Result-Total": "600",
                "Cache-Control": "public, max-age=120",
            },
            content=json.dumps([{"id": 1, "whitelisted": False, "buys": {}, "sells": {}}]).encode(),
            url="https://api.guildwars2.com/v2/commerce/prices",
            elapsed_ms=1.0,
        )
    if adapter.kind == "orderbook":
        return ResponseView(
            status_code=200,
            headers={"Cache-Control": "public, max-age=1200"},
            content=json.dumps({"id": 24, "buys": [], "sells": []}).encode(),
            url="https://api.guildwars2.com/v2/commerce/listings/24",
            elapsed_ms=1.0,
        )
    if adapter.kind == "item_meta":
        return ResponseView(
            status_code=200,
            headers={"Cache-Control": "public, max-age=3600"},
            content=json.dumps([{"id": 24, "name": "x", "type": "Consumable"}]).encode(),
            url="https://api.guildwars2.com/v2/items",
            elapsed_ms=1.0,
        )
    return ResponseView(
        status_code=200,
        headers={"Content-Type": "text/xml"},
        content=b"<rss><channel><item><title>t</title><link>l</link></item></channel></rss>",
        url="https://www.guildwars2.com/en/feed/",
        elapsed_ms=1.0,
    )


# ===========================================================================
# 6. 纯函数性：同一 (task, view) 两次 parse 结果深度相等
# ===========================================================================
@_param()
def test_contract_6_parse_is_pure(adapter) -> None:
    task = _stamp(_first_task(adapter))
    view = _real_view(adapter, task)
    first = adapter.parse(task, view)
    second = adapter.parse(task, view)
    assert _as_tuple(first) == _as_tuple(second), f"{adapter.source_id} 的 parse 不是纯函数"


def _as_tuple(result) -> tuple:
    return (
        result.task_id,
        result.cycle_id,
        result.source_id,
        tuple(result.records),
        tuple(result.next_tasks),
        tuple(sorted((result.cursor or {}).items())),
        result.quality,
        tuple(result.consistency_failures),
    )


# ===========================================================================
# 7. 容错性：坏记录不中断整页，且必须出现在 consistency_failures
# ===========================================================================
@_param()
def test_contract_7_bad_record_isolated(adapter) -> None:
    task = _stamp(_first_task(adapter))
    view = _bad_record_view(adapter, task)
    # 不得抛异常
    result = adapter.parse(task, view)
    assert (
        result.consistency_failures
    ), f"{adapter.source_id} 对坏记录没有产出 consistency_failures（静默丢弃 = 违规）"


def _bad_record_view(adapter, task: FetchTask) -> ResponseView:
    """注入 1 条坏记录（负价格 / 缺字段）的全页。"""
    if adapter.kind == "price":
        body = [
            {
                "id": 1,
                "whitelisted": False,
                "buys": {"quantity": 10, "unit_price": 128},
                "sells": {"quantity": 5, "unit_price": 200},
            },
            {
                "id": 2,
                "whitelisted": False,
                "buys": {"quantity": -5, "unit_price": -1},
                "sells": {"quantity": -1, "unit_price": -999},
            },  # 坏记录
            {"id": "not-an-int", "whitelisted": False, "buys": {}, "sells": {}},  # 坏记录
        ]
        return ResponseView(
            200,
            {"X-Page-Total": "1", "Content-Type": "application/json"},
            json.dumps(body).encode(),
            "https://api.guildwars2.com/v2/commerce/prices",
            1.0,
        )
    if adapter.kind == "orderbook":
        body = {
            "id": 24,
            "buys": [
                {"listings": 1, "unit_price": 128, "quantity": 56},
                {"listings": 1, "unit_price": -5, "quantity": -1},  # 坏记录
            ],
            "sells": [{"listings": 1, "unit_price": 0, "quantity": 3}],  # 坏记录：unit_price<=0
        }
        return ResponseView(
            200,
            {"Content-Type": "application/json"},
            json.dumps(body).encode(),
            "https://api.guildwars2.com/v2/commerce/listings/24",
            1.0,
        )
    if adapter.kind == "item_meta":
        body = [
            {"id": 24, "name": "ok", "type": "Consumable"},
            {"id": "bad", "name": "缺 id"},  # 坏记录
        ]
        return ResponseView(
            200,
            {"Content-Type": "application/json"},
            json.dumps(body).encode(),
            "https://api.guildwars2.com/v2/items",
            1.0,
        )
    body = (
        "<rss><channel>"
        "<item><title>bad</title><link></link></item>"
        "<item></item>"  # 坏记录：空 entry
        "</channel></rss>"
    )
    return ResponseView(
        200,
        {"Content-Type": "text/xml"},
        body.encode(),
        "https://www.guildwars2.com/en/feed/",
        1.0,
    )


# ===========================================================================
# 8. 只读性：method ∈ {GET, HEAD} 且无 body
# ===========================================================================
@_param()
def test_contract_8_readonly(adapter) -> None:
    tasks = adapter.initial_tasks(None)
    for task in tasks:
        assert task.method in ALLOWED_METHODS, f"{adapter.source_id} 产生非只读方法 {task.method}"
        # FetchTask 无 body 字段（§7.1 契约），只读由 fetcher 的 assert_readonly 再兜一道
        assert not hasattr(task, "content")
    # 复核 fetcher 侧的强制检查点确实拒绝 POST
    from market_watch.domain.errors import ReadOnlyViolation
    from market_watch.domain.policy import assert_readonly

    with pytest.raises(ReadOnlyViolation):
        assert_readonly("POST", None)


# ===========================================================================
# 9. robots 一致性：全部 task 的 path 必须通过 RobotsGuard
# ===========================================================================
def test_contract_9_robots_allows_all_paths() -> None:
    """用仓库留存的 robots 快照（404 → 无指令 → allow）判定全部 task 的 path。"""
    import asyncio

    snapshot_path = Path(__file__).resolve().parents[2] / "docs" / "_robots_snapshot.json"
    preloaded: dict[str, RobotsRecord] = {}
    raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    for item in raw:
        preloaded[item["host"]] = RobotsRecord(
            host=item["host"],
            status_code=int(item["status"]),
            allowed=int(item["status"]) == 404,
            raw=str(item.get("body", ""))[:2000],
            fetched_at_iso="2026-10-03T00:00:00Z",
            note=f"HTTP {item['status']}",
        )
    guard = RobotsGuard("market-watch/0.1.0 (+https://you.example/contact)", preloaded=preloaded)

    async def _check() -> None:
        for adapter in _adapters():
            assert adapter.host in preloaded, f"缺少 {adapter.host} 的 robots 快照"
            for task in adapter.initial_tasks(None):
                assert await guard.allowed(
                    task.host, task.path, guard._ua
                ), f"{adapter.source_id} 的 path {task.path} 被 robots 拒绝"

    asyncio.run(_check())


# ===========================================================================
# 辅助：首个任务 / 真实样本视图
# ===========================================================================
def _first_task(adapter) -> FetchTask:
    tasks = adapter.initial_tasks(None)
    assert tasks, f"{adapter.source_id} 未产出初始任务（清单为空？）"
    return tasks[0]


def _real_view(adapter, task: FetchTask) -> ResponseView:
    """用 `adapters/fixtures/` 里录制的真实样本构造 ResponseView。"""
    if adapter.kind == "price":
        content = (ADAPTER_FIXTURES / "gw2_prices_page1.json").read_bytes()
        return ResponseView(
            200,
            {
                "X-Page-Total": "140",
                "X-Page-Size": "200",
                "X-Result-Total": "27997",
                "Cache-Control": "public, max-age=120",
                "X-Rate-Limit-Limit": "600",
            },
            content,
            "https://api.guildwars2.com/v2/commerce/prices?page=0",
            1.0,
        )
    if adapter.kind == "orderbook":
        content = (ADAPTER_FIXTURES / "gw2_listings_item24.json").read_bytes()
        return ResponseView(
            200,
            {"Cache-Control": "public, max-age=1200"},
            content,
            "https://api.guildwars2.com/v2/commerce/listings/24",
            1.0,
        )
    if adapter.kind == "item_meta":
        content = (ADAPTER_FIXTURES / "gw2_items_ids24_68.json").read_bytes()
        return ResponseView(
            200,
            {"Cache-Control": "public, max-age=3600"},
            content,
            "https://api.guildwars2.com/v2/items?ids=24,68",
            1.0,
        )
    content = (ADAPTER_FIXTURES / "gw2_news_feed.xml").read_bytes()
    return ResponseView(
        200,
        {"Content-Type": "text/xml; charset=utf-8"},
        content,
        "https://www.guildwars2.com/en/feed/",
        1.0,
    )


# ===========================================================================
# 附加：真实样本解析冒烟（确保解析契约与实测结构一致）
# ===========================================================================
def test_real_fixture_prices_parse_shape() -> None:
    from market_watch.infrastructure.adapters.gw2_prices import Gw2PricesAdapter

    adapter = Gw2PricesAdapter()
    task = _stamp(adapter.initial_tasks(None)[0])
    result = adapter.parse(task, _real_view(adapter, task))
    assert len(result.records) == 200
    tick = result.records[0]
    # 实测语义：sells.unit_price = 最低卖价，buys.unit_price = 最高买价
    assert tick.item_id == 24
    assert tick.sell_unit_price is not None and tick.buy_unit_price is not None
    assert tick.sell_unit_price > tick.buy_unit_price


def test_real_fixture_listings_side_ordering() -> None:
    from market_watch.infrastructure.adapters.gw2_listings import Gw2ListingsAdapter

    adapter = Gw2ListingsAdapter(watchlist=(24,))
    task = _stamp(adapter.initial_tasks(None)[0])
    result = adapter.parse(task, _real_view(adapter, task))
    buys = [r.unit_price for r in result.records if r.side == "buy"]
    sells = [r.unit_price for r in result.records if r.side == "sell"]
    assert buys, "应当解析出买盘"
    assert sells, "应当解析出卖盘"
    assert buys == sorted(buys, reverse=True), "买盘应为降序"
    assert sells == sorted(sells), "卖盘应为升序"
    assert min(sells) >= max(buys), "最低卖价应 >= 最高买价（否则负 spread）"
