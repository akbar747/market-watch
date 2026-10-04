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

已知缺口（本次合并未修，记账）
----------------------------
判成 `None` 之后，「这里是脏数据」和「该侧本来就没有挂单」在下游无法区分：GW2 确实
会对无买盘的物品返回空对象，`validate_tick` 把 None 价格当合法态，所以 `unit_price:
true` 落到 None 后就被静默吸收了，不留 `ConsistencyViolation`。要让它可观测，得在
adapter 的 `_one` 里区分「键不存在」与「键存在但不可转」——那是一次 parse 层改动，
本次只统一 coercion 语义，故只做记录，不改行为。
"""

from __future__ import annotations

__all__ = ["as_int"]


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
