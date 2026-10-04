# tests/unit/test_gw2_int_coercion.py
"""钉住合并后的 `adapters._coerce.as_int` 语义：JSON 原始标量 → `int | None`。

为什么要有这个文件：2026-10-03 B1 那次 mypy 修复把 coercion 从「把 `object` 直接喂
`int()`、靠抛 `TypeError` 隐式兜底」改成「`isinstance` 显式收窄到 JSON 真正会出现的
`int` / `float` / `str`」。改的是**兜底机制本身**，属于高风险低可见度改动；而 docstring
白纸黑字承诺的「无挂单侧空对象 `{}` → None」此前**从未被任何测试验证过**——官方哪天
把 `{}` 改成 `null` 或 `[]`，这个契约会静默失效且没人发现。这里把契约钉成可执行的。

2026-10-04 合并（Task #10）：原先 `gw2_prices` / `gw2_listings` 各有一份 `_as_int`，
两者语义在 `bool` 上分叉（`True` → `1` vs `None`）。已抽到 `adapters/_coerce.py` 并对齐
为 **`bool` → `None`**——理由写在该模块 docstring 里，一句话版：`True` 静默转成单价 `1`
会让脏数据以合法身份混进统计口径，比丢掉一条记录危险得多。

本文件因此**反向锁住合并本身**：`test_both_adapters_share_one_implementation` 断言两侧
拿到的是同一个函数对象。谁再把 coercion 复制回两个 adapter 内部各留一份，这条立刻红。

未覆盖：`float("inf")` 这类会抛 `OverflowError` 的输入。合并前后都不被 `try` 兜住，
写进来等于把「没兜住」这个现状固化成契约，将来补兜底反而要改测试——故不写。

已知缺口（**未修，不属于本文件职责**）
--------------------------------------
判成 `None` 之后，「这个字段是脏数据」和「该侧本来就没挂单」在下游看起来一模一样：
`validate_tick` / `validate_levels` 都把 None 价格当合法态（有些物品确实没有买盘），
因此 `unit_price: true` 这种脏输入会被静默吸收，不留 failure。要堵住得在 adapter 的
`_one` 里区分「键不存在」与「键存在但不可转」，再产出一条 `ConsistencyViolation`。
那是一次 parse 层改动，本轮的 Task #10 只统一 coercion 语义，故记账不动代码。
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

import pytest

from market_watch.domain.models import FetchTask, ResponseView
from market_watch.infrastructure.adapters import _coerce, gw2_listings, gw2_prices
from market_watch.infrastructure.adapters.gw2_listings import Gw2ListingsAdapter
from market_watch.infrastructure.adapters.gw2_prices import Gw2PricesAdapter

#: `(入参, 期望)`。`True` / `False` 均判 None：**bool 从不参与数值口径**。
_CASES: list[tuple[object, int | None]] = [
    (5, 5),
    (5.7, 5),  # 截断而非四舍五入
    ("7", 7),  # 数字字符串（官方偶发把数字编成字符串）
    (None, None),  # 合法态：该侧无挂单
    ("abc", None),  # 非数字字符串
    ({}, None),  # docstring 明确承诺的那条：无挂单侧空对象
    ([], None),
    (True, None),  # 合并后的统一语义：bool 是脏数据，不是 1
    (False, None),
]

_OBSERVED_AT = datetime(2026, 10, 4, 6, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(("value", "expected"), _CASES, ids=repr)
def test_as_int_contract(value: Any, expected: int | None) -> None:
    """可转 → 整数；不可转 → None（**不是 0**，None 是合法态）。"""
    assert _coerce.as_int(value) == expected


def test_both_adapters_share_one_implementation() -> None:
    """两侧 adapter 必须共用同一份实现——这是 Task #10 合并的防回归锁。

    只测行为测不出「两份代码重新漂回来」：两边同时改成 `True → None` 也能绿，
    但下一次改其中一份又会分叉。这里直接判函数对象同一性。
    """
    assert gw2_prices.as_int is _coerce.as_int
    assert gw2_listings.as_int is _coerce.as_int


def _view(content: str, url: str) -> ResponseView:
    return ResponseView(
        status_code=200,
        headers={"Cache-Control": "public, max-age=120"},
        content=content.encode(),
        url=url,
        elapsed_ms=1.0,
    )


def _task(source_id: str, path: str) -> FetchTask:
    return FetchTask(
        task_id="t-bool",
        source_id=source_id,
        host="api.guildwars2.com",
        method="GET",
        path=path,
        observed_at=_OBSERVED_AT,
        cycle_id="c-bool",
    )


def test_prices_parse_refuses_bool_price() -> None:
    """`unit_price: true` 不得伪装成单价 `1` 落进 PriceTick（合并前正是 → 1）。"""
    body = json.dumps(
        [
            {
                "id": 24,
                "whitelisted": False,
                "buys": {"quantity": 10, "unit_price": True},
                "sells": {"quantity": 20, "unit_price": False},
            }
        ]
    )
    result = Gw2PricesAdapter().parse(
        _task("gw2_prices", "/v2/commerce/prices"),
        _view(body, "https://api.guildwars2.com/v2/commerce/prices"),
    )

    assert len(result.records) == 1, "脏价位应当仍产出记录（剩余字段有效），而不是整条消失"
    tick = result.records[0]
    assert tick.buy_unit_price is None, "合并前这里会是 1（True 被静默强转）"
    assert tick.sell_unit_price is None, "合并前这里会是 0（False 被静默强转）"
    # 同一条里的 quantity 是正常整数，证明被判 None 的只有 bool 那两个字段，
    # 不是整条记录被一起丢掉了——`bool → None` 的代价仅限脏字段本身。
    assert tick.buy_quantity == 10
    assert tick.sell_quantity == 20

    # 已知缺口（不在本测试范围内，见模块 docstring 末尾）：
    # `validate_tick` **不会**因为两侧价格都是 None 而报警（这是合法态：无买盘 / 无卖盘），
    # 所以「`unit_price` 是 true」这种脏数据在这里变成 None 后，与「该侧无挂单」完全无法区分，
    # 是被静默吸收的。想堵住要在 `_one` 里区分「键不存在」与「键存在但不可转」，
    # 那属于 parse 层的另一项改动，本轮 Task #10 只负责统一 coercion 语义。


def test_listings_parse_drops_bool_price_level() -> None:
    """挂单价是 bool → 该档位整条丢弃并记 failure，不会带着假价格进 records。"""
    body = json.dumps(
        {"id": 24, "buys": [{"listings": 1, "unit_price": True, "quantity": 5}], "sells": []}
    )
    result = Gw2ListingsAdapter(watchlist=(24,)).parse(
        _task("gw2_listings", "/v2/commerce/listings/24"),
        _view(body, "https://api.guildwars2.com/v2/commerce/listings/24"),
    )

    assert result.records == (), "单价非整数的档位不得产出 ListingLevel"
    assert any(f.rule == "schema.level_missing_field" for f in result.consistency_failures)
