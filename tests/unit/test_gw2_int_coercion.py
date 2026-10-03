# tests/unit/test_gw2_int_coercion.py
"""钉住两个 adapter 里各自那份 `_as_int` 的兜底语义：JSON 原始值 → `int | None`。

为什么要有这个文件：2026-10-03 B1 那次 mypy 修复把 `_as_int` 从「把 `object` 直接喂
`int()`、靠抛 `TypeError` 隐式兜底」改成「`isinstance` 显式收窄到 JSON 真正会出现的
`int` / `float` / `str`」。改的是**兜底机制本身**，属于高风险低可见度改动；而 docstring
白纸黑字承诺的「无挂单侧空对象 `{}` → None」此前**从未被任何测试验证过**——官方哪天
把 `{}` 改成 `null` 或 `[]`，这个契约会静默失效且没人发现。这里把契约钉成可执行的。

两侧语义差异（**只记录现状，不改**）：
- `gw2_prices._as_int` 没有 bool 前置判断，`bool` 是 `int` 的真子类 → `True` → `1`；
- `gw2_listings._as_int` 显式前置 `isinstance(value, bool)` → `True` → `None`。

两份 helper 是各自独立的两份代码（本轮刻意不抽公共 helper），所以两份都测。

未覆盖：`float("inf")` 这类会抛 `OverflowError` 的输入。改前改后都不被 try 兜住，
写进来等于把「没兜住」这个现状固化成契约，将来补兜底反而要改测试——故不写。
"""

from __future__ import annotations

from typing import Any

import pytest

from market_watch.infrastructure.adapters.gw2_listings import _as_int as listings_as_int
from market_watch.infrastructure.adapters.gw2_prices import _as_int as prices_as_int

#: `(入参, 期望)`。`bool` 那行的期望两侧不同，理由见模块 docstring。
_PRICES_CASES: list[tuple[object, int | None]] = [
    (5, 5),
    (5.7, 5),
    ("7", 7),
    (None, None),
    ("abc", None),
    ({}, None),  # docstring 明确承诺的那条：无挂单侧空对象
    ([], None),
    (True, 1),  # 无 bool 挡 → int(True) == 1（现状，非笔误）
]

#: 与上面逐项同义，只有 `True` 的期望不同（显式前置 bool 判断 → None）。
_LISTINGS_CASES: list[tuple[object, int | None]] = [
    (5, 5),
    (5.7, 5),
    ("7", 7),
    (None, None),
    ("abc", None),
    ({}, None),
    ([], None),
    (True, None),
]


@pytest.mark.parametrize(("value", "expected"), _PRICES_CASES, ids=repr)
def test_gw2_prices_as_int(value: Any, expected: int | None) -> None:
    """`gw2_prices._as_int`：可转 → 整数；不可转 → None（**不是 0**，None 是合法态）。"""
    assert prices_as_int(value) == expected


@pytest.mark.parametrize(("value", "expected"), _LISTINGS_CASES, ids=repr)
def test_gw2_listings_as_int(value: Any, expected: int | None) -> None:
    """`gw2_listings._as_int`：同上，额外把 `bool` 判为不可转。"""
    assert listings_as_int(value) == expected
