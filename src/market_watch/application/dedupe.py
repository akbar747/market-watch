# src/market_watch/application/dedupe.py
"""进程内变更门（change gate）+ content_hash 计算。

架构依据：docs/01-architecture.md §8.3（Change gate 实现，热路径）。

容量设计：
    `LastSeenTracker` 用 `collections.OrderedDict` **手工维护 LRU**，容量 = 最近一轮
    item 数 + 20% ≈ 34k 条。~34k × ~120B ≈ 4MB RSS，可接受。
    进程重启后第一轮全写（天然补写），这不算故障。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
import hashlib
from typing import Final

from ..domain.models import Announcement, PriceTick

__all__ = [
    "DEFAULT_CAPACITY",
    "LastSeenTracker",
    "announcement_hash",
    "content_hash",
    "guid_for",
]

#: 默认容量：实测 universe ≈ 27997，+20% ≈ 33600，取整 34000
DEFAULT_CAPACITY: Final[int] = 34_000


class LastSeenTracker:
    """`dict[item_id] -> (buy_px, buy_qty, sell_px, sell_qty, observed_at_iso)`

    - `changed(tick)` 返回 `(是否变化, prev_sell_price)`；命中即跳过写入。
    - 手工 LRU：命中时 `move_to_end`，超容量时 `popitem(last=False)`。
      用 OrderedDict 而不是 functools.lru_cache——后者无法表达「按 item_id 精确失效」。
    """

    __slots__ = ("_capacity", "_data", "_evictions", "_hits", "_misses")

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._data: OrderedDict[int, tuple[int, int, int, int, str]] = OrderedDict()
        self._capacity = max(1, int(capacity))
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, item_id: int) -> bool:
        return item_id in self._data

    def __iter__(self) -> Iterator[int]:
        return iter(self._data)

    def changed(self, tick: PriceTick) -> tuple[bool, int | None]:
        """热路径：返回 `(changed, prev_sell_price)`。

        四项数值任一变化即为「变化」。`observed_at` 不参与比较——周期时间戳必然变。
        """
        current = tick.key
        seen = self._data.get(tick.item_id)
        if seen is None:
            self._misses += 1
            self._put(tick.item_id, (*current, tick.observed_at.isoformat()))
            return True, None
        self._data.move_to_end(tick.item_id)
        prev = seen[2] or None  # prev_sell_price
        if seen[:4] == current:
            self._hits += 1
            return False, prev
        self._hits += 1
        self._put(tick.item_id, (*current, tick.observed_at.isoformat()))
        return True, prev

    def peek(self, item_id: int) -> tuple[int, int, int, int] | None:
        seen = self._data.get(item_id)
        return None if seen is None else seen[:4]

    def _put(self, item_id: int, value: tuple[int, int, int, int, str]) -> None:
        self._data[item_id] = value
        self._data.move_to_end(item_id)
        while len(self._data) > self._capacity:
            self._data.popitem(last=False)
            self._evictions += 1

    def stats(self) -> dict[str, int]:
        return {
            "size": len(self._data),
            "capacity": self._capacity,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
        }

    def clear(self) -> None:
        self._data.clear()


def content_hash(title: str, summary: str | None, link: str) -> str:
    """§7.2：`sha256(title|summary|link)` 前 32 hex。"""
    raw = f"{title}|{summary or ''}|{link}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def guid_for(guid: str | None, link: str, title: str) -> str:
    """§7.2：优先 `<guid>`；缺失用 `sha1(link|title)`。"""
    if guid:
        return guid
    # S324：sha1 只用于公告 guid 兜底（非安全场景），sha256 无必要
    return hashlib.sha1(f"{link}|{title}".encode()).hexdigest()  # noqa: S324


def announcement_hash(ann: Announcement) -> str:
    return content_hash(ann.title, ann.summary, ann.link)
