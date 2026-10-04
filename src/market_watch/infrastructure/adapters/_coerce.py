# src/market_watch/infrastructure/adapters/_coerce.py
"""JSON 原始标量 → `int | None` 的**唯一**实现（两侧 adapter 共用）。

为什么要有这个文件
------------------
此前 `gw2_prices` 与 `gw2_listings` **各有一份** `_as_int`，逐字近似但语义不同：

- `gw2_prices._as_int` 没有 bool 前置判断，而 `bool` 是 `int` 的真子类 →
  `True` 转出 `1`；
- `gw2_listings._as_int` 显式前置 `isinstance(value, bool)` → `True` 得到 `None`。

同一个字段在两条链路上落两种结果，排查时极易误判。故抽公共 helper 并对齐语义。

统一后的语义：**`bool` 一律 → `None`**
------------------------------------
JSON 的 `true` / `false` 出现在 `id` / `unit_price` / `quantity` / `listings`
这些位置上只可能是脏数据（官方从未这样返回）。把它静默转成 `1`，等于让脏数据
**伪装成合法单价**混进统计口径——后续的 `validate_tick` 通常看不出 `1` 和真实
报价的区别。丢掉这条记录（判 `None`，由上层进 `consistency_failures`）虽然少了
一个点，但远比污染均价/告警基线安全。这与本项目一贯的取舍一致：**宁可少收一条，
也不让不可信的值以合法身份落库**。

为什么不用 `int(value)` 直接收
-----------------------------
`view.json()` 声明返回 `Any`，参数只能标 `object`；而 typeshed 的 `int()` 重载
只接受 `str | bytes | SupportsInt | SupportsIndex | SupportsTrunc`，`int(object)`
没有匹配重载 → 返回值退化成 `Any` 并触发 `no-any-return`。故先用 `isinstance`
把类型收窄到 JSON 里真正可能出现的三种标量，再交给 `int()`。

未覆盖
------
`float("inf")` / `float("-inf")` 走的是 `OverflowError`，不在 `try` 的捕获范围内，
统一前后行为一致。此处刻意不把它写进测试，免得把「没兜住」这个现状固化成契约。

字段级脏值判定（`as_int_field`）
------------------------------
「该侧无挂单」和「这个字段是脏数据」在 `as_int` 的输出里都是 `None`，而下游
`validate_tick` 又因为「无买盘」是合法态而不报警，于是脏输入会被彻底静默吸收。
`as_int_field` 就是用来把这两种情形分开的：见下方它的 docstring。
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["as_int", "as_int_field"]


def as_int(value: object) -> int | None:
    """把 JSON 原始标量转成整数；不可转（`None` / `bool` / `{}` / `[]` / 嵌套）→ `None`。

    `None` 是**合法态**，不是 `0`：官方对「无挂单的那一侧」返回空对象 `{}`，
    与「价格为 0」是两回事。

    Args:
        value: `view.json()` 取出的原始字段。JSON 语法把它限定为 `int` / `float` /
            `str` / `None` / `bool`，其余标量（`{}` 空对象、`[]`、嵌套结构）一律 → None。

    Returns:
        可转时的整数值（含截断：`5.7` → `5`；数字字符串：`"7"` → `7`），否则 `None`。
    """
    if value is None or isinstance(value, bool):
        # bool 是真 int 子类：JSON 里 `true` 不可能当 id / 单价 / 数量，
        # 判 None 而非 int(True) == 1（理由见模块 docstring）
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        # 不可转整数（如 `int("abc")`）→ None
        return None


def as_int_field(raw: Mapping[str, object], key: str) -> tuple[int | None, bool]:
    """取一个整数字段，并把「字段不存在」与「字段存在但不可转」分开。

    为什么要有这个二元组：`as_int` 把「无挂单」和「脏数据」都压成 `None`，而下游
    `validate_tick` 又认定「无买盘」是合法态、不报警——于是 `unit_price: true`
    这类脏输入落到 None 之后就被彻底静默吸收，官方哪天改了响应结构我们不会知道。
    这里把两种情形分开，让 adapter 能给脏值留一条可排查的 `ConsistencyViolation`。

    **显式 `null` 与「键不存在」同义，都不算脏**：官方对无挂单的一侧返回的是空对象
    `{}`，不用 `null` 表示价格；假如把 `null` 也算脏，`{"unit_price": null}`
    这种无害输入会刷出一堆噪音，反而淹没真正的问题。

    Args:
        raw: 已确认是 dict 的单条原始记录（挂单侧的对象或挂单档位对象）。
        key: 字段名。

    Returns:
        `(值, 是否脏)`。脏 = 键在、值不是 `null`、但 `as_int` 判为不可转。
        值本身仍然是「不可转 → None」，与 `as_int` 完全一致，本函数只多给一个标记。
    """
    if key not in raw:
        return None, False
    value = raw[key]
    if value is None:
        return None, False
    coerced = as_int(value)
    return coerced, coerced is None
