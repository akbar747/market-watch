# tests/unit/test_gw2_int_coercion.py
"""钉住 `adapters._coerce` 的两层语义：数值 coercion，以及「脏值」的可观测性。

为什么要有这个文件：2026-10-03 B1 那次 mypy 修复把 coercion 从「把 `object` 直接喂
`int()`、靠抛 `TypeError` 隐式兜底」改成「`isinstance` 显式收窄到 JSON 真正会出现的
`int` / `float` / `str`」。改的是**兜底机制本身**，属于高风险低可见度改动；而 docstring
白纸黑字承诺的「无挂单侧空对象 `{}` → None」此前**从未被任何测试验证过**——官方哪天
把 `{}` 改成 `null` 或 `[]`，这个契约会静默失效且没人发现。这里把契约钉成可执行的。

2026-10-04 合并（Task #10）：原先 `gw2_prices` / `gw2_listings` 各有一份 `_as_int`，
两者语义在 `bool` 上分叉（`True` → `1` vs `None`）。已抽到 `adapters/_coerce.py` 并对齐
为 **`bool` → `None`**——理由写在该模块 docstring 里，一句话版：`True` 静默转成单价 `1`
会让脏数据以合法身份混进统计口径，比丢掉一条记录危险得多。

2026-10-04 补可观测性：判成 `None` 之后，「这里是脏数据」和「该侧本来就没有挂单」在下游
看起来一模一样（`validate_tick` 认为无买盘是合法态、不报警），脏输入会被彻底静默吸收。
故加了 `as_int_field`，把「键不存在 / 值为 null」与「键存在但不可转」分开，让 adapter
给脏值留一条 `schema.field_not_integer`。

两个方向的坑都要钉住：
- 脏值**必须**报警（否则官方改了响应结构我们不会知道）——见 `test_*_flags_*` / `test_*_dirty*`；
- 合法的空对象 / null **必须不**报警（否则真实 GW2 数据里成千上万个无买盘物品会刷出一屏
  `warn`，把真问题彻底淹掉）——见 `test_*_not_dirty`。**这条比前一条更容易写错。**

未覆盖：`float("inf")` 这类会抛 `OverflowError` 的输入。前后都不被 `try` 兜住，
写进来等于把「没兜住」这个现状固化成契约，将来补兜底反而要改测试——故不写。
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

#: `(原始映射, 键, 期望值, 期望脏标记)`。脏 = 键存在、值非 null、但不可转。
_FIELD_CASES: list[tuple[dict[str, object], str, int | None, bool]] = [
    ({}, "unit_price", None, False),  # 键不存在：无挂单侧，合法
    ({"unit_price": None}, "unit_price", None, False),  # 显式 null 与缺省同义，不算脏
    ({"unit_price": 128}, "unit_price", 128, False),
    ({"unit_price": "128"}, "unit_price", 128, False),  # 数字字符串能转
    ({"unit_price": True}, "unit_price", None, True),  # bool 是脏数据
    ({"unit_price": "abc"}, "unit_price", None, True),
    ({"unit_price": {}}, "unit_price", None, True),  # 整侧缺省给 `{}` 是合法的；单字段给 `{}` 是脏
]

_OBSERVED_AT = datetime(2026, 10, 4, 6, 0, 0, tzinfo=UTC)
_PRICES_URL = "https://api.guildwars2.com/v2/commerce/prices"
_LISTINGS_URL = "https://api.guildwars2.com/v2/commerce/listings/24"


@pytest.mark.parametrize(("value", "expected"), _CASES, ids=repr)
def test_as_int_contract(value: Any, expected: int | None) -> None:
    """可转 → 整数；不可转 → None（**不是 0**，None 是合法态）。"""
    assert _coerce.as_int(value) == expected


@pytest.mark.parametrize(("raw", "key", "value", "dirty"), _FIELD_CASES, ids=repr)
def test_as_int_field_splits_absent_from_dirty(
    raw: dict[str, object], key: str, value: int | None, dirty: bool
) -> None:
    """`as_int_field` 多给的那个标记，必须只标记「键在但不可转」。"""
    assert _coerce.as_int_field(raw, key) == (value, dirty)


def test_both_adapters_share_one_implementation() -> None:
    """两侧 adapter 必须共用同一份实现——这是 Task #10 合并的防回归锁。

    只测行为测不出「两份代码重新漂回来」：两边同时改成 `True → None` 也能绿，
    但下一次改其中一份又会分叉。这里直接判函数对象同一性。
    """
    assert gw2_prices.as_int_field is _coerce.as_int_field
    assert gw2_listings.as_int_field is _coerce.as_int_field


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
        task_id="t-coerce",
        source_id=source_id,
        host="api.guildwars2.com",
        method="GET",
        path=path,
        observed_at=_OBSERVED_AT,
        cycle_id="c-coerce",
    )


def _parse_prices(payload: object) -> Any:
    return Gw2PricesAdapter().parse(
        _task("gw2_prices", "/v2/commerce/prices"), _view(json.dumps(payload), _PRICES_URL)
    )


def _dirty_rules(result: Any) -> list[str]:
    return [f.field for f in result.consistency_failures if f.rule == "schema.field_not_integer"]


def test_prices_parse_flags_dirty_field() -> None:
    """`unit_price: true` 不得伪装成单价 `1`，且必须留下可排查的失败记录。"""
    result = _parse_prices(
        [
            {
                "id": 24,
                "whitelisted": False,
                "buys": {"quantity": 10, "unit_price": True},
                "sells": {"quantity": 20, "unit_price": False},
            }
        ]
    )

    assert len(result.records) == 1, "剩余字段仍然有效，整条不该消失"
    tick = result.records[0]
    assert tick.buy_unit_price is None, "合并前这里会是 1（True 被静默强转）"
    assert tick.sell_unit_price is None, "合并前这里会是 0（False 被静默强转）"
    assert tick.buy_quantity == 10
    assert tick.sell_quantity == 20

    assert sorted(_dirty_rules(result)) == [
        "buys.unit_price",
        "sells.unit_price",
    ], "脏字段必须逐个点名"


def test_prices_parse_empty_side_is_not_dirty() -> None:
    """**防噪音的关键一条**：无挂单侧返回 `{}` 是官方常态，绝不能报警。

    这条比「脏值要报警」更容易写错——一旦判错，真实数据里成千上万个无买盘物品
    会刷出一屏 `warn`，把真正的问题彻底淹掉。
    """
    result = _parse_prices(
        [{"id": 24, "whitelisted": False, "buys": {}, "sells": {"quantity": 20, "unit_price": 200}}]
    )

    assert len(result.records) == 1
    tick = result.records[0]
    assert tick.buy_unit_price is None and tick.buy_quantity is None
    assert tick.sell_unit_price == 200 and tick.sell_quantity == 20
    assert _dirty_rules(result) == [], "合法的空挂单侧不得产生脏值告警"


def test_prices_parse_explicit_null_is_not_dirty() -> None:
    """显式 `null` 与「键不存在」同义，同样不算脏。"""
    result = _parse_prices(
        [{"id": 24, "buys": {"quantity": None, "unit_price": None}, "sells": {}}]
    )
    assert _dirty_rules(result) == []


def test_listings_parse_drops_bool_price_level() -> None:
    """挂单价是 bool → 该档位整条丢弃，并同时留下「丢了」和「为什么」两条线索。"""
    result = Gw2ListingsAdapter(watchlist=(24,)).parse(
        _task("gw2_listings", "/v2/commerce/listings/24"),
        _view(
            json.dumps(
                {
                    "id": 24,
                    "buys": [{"listings": 1, "unit_price": True, "quantity": 5}],
                    "sells": [],
                }
            ),
            _LISTINGS_URL,
        ),
    )

    assert result.records == (), "单价非整数的档位不得产出 ListingLevel"
    rules = [f.rule for f in result.consistency_failures]
    assert "schema.level_missing_field" in rules, "丢档位这件事本身要记录"
    assert "schema.field_not_integer" in rules, "脏值这个**原因**也要记录，不能只说缺字段"
