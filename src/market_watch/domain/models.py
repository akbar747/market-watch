# src/market_watch/domain/models.py
"""跨层传输的全部数据结构。

架构依据：docs/01-architecture.md §7.1（传输契约）、§7.2（业务实体）。

约定：
- 全部 dataclass 为 `frozen=True, slots=True`（不可变、省内存、可哈希键友好）。
- 全文所有 `datetime` 一律 **timezone-aware UTC**；落库序列化为 ISO8601 `Z` 字符串。
- 本层禁止 import httpx / sqlite3 / fastapi / pandas（依赖方向铁律 §3）。
  `ResponseView` 是 httpx.Response 的**快照**，禁止把 httpx 对象传进 domain/application。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from typing import Any, Literal

from .errors import AttemptPolicy, ErrorClass, PayloadError

__all__ = [
    "AlertEvent",
    "Announcement",
    "AttemptPolicy",
    "ConsistencyViolation",
    "Cursor",
    "CycleContext",
    "CycleReport",
    "ErrorClass",
    "FanoutReport",
    "FetchResult",
    "FetchTask",
    "HealthReport",
    "ItemMeta",
    "ListingLevel",
    "ParseResult",
    "Politeness",
    "PriceAggregate",
    "PriceTick",
    "Quality",
    "RateState",
    "Record",
    "ResponseView",
    "WriteBatch",
    "iso_z",
    "utc_now",
]

type Cursor = dict[str, str]  # 业务游标：ETag / Last-Modified / 最大 published_at 等

type Quality = Literal["COMPLETE", "PARTIAL"]


def utc_now() -> datetime:
    """唯一允许的「取当前时间」入口；确保 tz-aware UTC（§14 P1 时区污染防护）。"""
    return datetime.now(tz=UTC)


def iso_z(dt: datetime) -> str:
    """datetime -> ISO8601 'Z' 字符串，秒级精度（落库格式统一）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ===========================================================================
# §7.1 传输契约
# ===========================================================================
def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    raw = headers.get(name)
    if raw is None:
        # httpx 的 Headers 大小写不敏感，但 dict 快照未必；做一次降级查找
        lowered = name.lower()
        for k, v in headers.items():
            if k.lower() == lowered:
                raw = v
                break
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class ResponseView:
    """httpx.Response 的快照。禁止把 httpx 对象传进 domain/application。"""

    status_code: int
    headers: Mapping[str, str]
    content: bytes
    url: str
    elapsed_ms: float

    def json(self) -> Any:
        """解析失败抛 `PayloadError`（→ ErrorClass.JSON_DECODE），不静默吞掉。"""
        try:
            return json.loads(self.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PayloadError(f"JSON 解析失败: {exc}") from exc

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def page_total(self) -> int | None:  # X-Page-Total
        return _header_int(self.headers, "X-Page-Total")

    @property
    def page_size(self) -> int | None:  # X-Page-Size
        return _header_int(self.headers, "X-Page-Size")

    @property
    def result_total(self) -> int | None:  # X-Result-Total
        return _header_int(self.headers, "X-Result-Total")

    @property
    def result_count(self) -> int | None:  # X-Result-Count
        return _header_int(self.headers, "X-Result-Count")

    @property
    def rate_limit(self) -> int | None:  # X-Rate-Limit-Limit
        return _header_int(self.headers, "X-Rate-Limit-Limit")

    @property
    def rate_remaining(self) -> int | None:  # 多数源不返回 -> None
        for key in ("X-Rate-Limit-Remaining", "X-RateLimit-Remaining", "X-Ratelimit-Remaining"):
            value = _header_int(self.headers, key)
            if value is not None:
                return value
        return None

    @property
    def max_age_seconds(self) -> int | None:
        """解析 Cache-Control: public, max-age=120。"""
        raw = self.headers.get("Cache-Control")
        if not raw:
            for k, v in self.headers.items():
                if k.lower() == "cache-control":
                    raw = v
                    break
        if not raw:
            return None
        for part in str(raw).split(","):
            token = part.strip().lower()
            if token.startswith("max-age="):
                try:
                    return int(token.split("=", 1)[1].strip())
                except ValueError:
                    return None
        return None

    def _header_str(self, name: str) -> str | None:
        raw = self.headers.get(name)
        if raw is None:
            lowered = name.lower()
            for k, v in self.headers.items():
                if k.lower() == lowered:
                    raw = v
                    break
        return None if raw is None else str(raw)

    @property
    def etag(self) -> str | None:
        return self._header_str("ETag")

    @property
    def last_modified(self) -> str | None:
        return self._header_str("Last-Modified")

    @property
    def retry_after_seconds(self) -> float | None:
        raw = self._header_str("Retry-After")
        if raw is None:
            return None
        try:
            return float(raw.strip())
        except ValueError:
            from email.utils import parsedate_to_datetime  # 局部导入：仅头部为 HTTP-date 时需要

            try:
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return None
            if dt is None:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return max(0.0, (dt - utc_now()).total_seconds())


@dataclass(frozen=True, slots=True)
class FetchTask:
    task_id: str
    source_id: str
    host: str
    method: Literal["GET", "HEAD"]
    path: str  # 不含 host，便于 robots 判定
    params: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    cost: float = 1.0  # 令牌成本
    strategy: Literal["paged", "single", "watchlist"] = "single"
    cursor: str | None = None  # 业务游标，透传给 adapter

    # §7.3 规则 1 要求 adapter 的 parse 是同步纯函数且「不得读时钟」，
    # 并指明「时间由调用方通过 FetchTask/CycleContext 注入」——这个字段就是注入点。
    # 由 orchestrator 在每轮开始时统一赋值（同一轮所有 task 共享同一个 observed_at）。
    observed_at: datetime | None = None
    # 同上：cycle_id 也是调用方注入的轮次上下文（adapter 不得自己生成/读时钟）。
    cycle_id: str | None = None

    @property
    def url(self) -> str:
        """重建完整 URL（仅用于日志/审计；出网由 fetcher 用 base_url + path 拼）。"""
        if self.params:
            from urllib.parse import urlencode

            return f"https://{self.host}{self.path}?{urlencode(dict(self.params))}"
        return f"https://{self.host}{self.path}"


@dataclass(frozen=True, slots=True)
class FetchResult:
    """一次 fetch 的完整结果，供 retry/breaker/metrics/fetch_log 消费。"""

    task_id: str
    view: ResponseView | None
    error_class: ErrorClass
    attempts: int
    total_ms: float
    slept_ms: float
    retry_after_used_s: float | None = None


@dataclass(frozen=True, slots=True)
class ParseResult:
    task_id: str
    cycle_id: str
    source_id: str
    records: Sequence[Record]
    next_tasks: Sequence[FetchTask] = ()
    cursor: Cursor | None = None
    quality: Quality = "COMPLETE"
    consistency_failures: Sequence[ConsistencyViolation] = ()


# ===========================================================================
# §7.2 业务实体
# ===========================================================================
@dataclass(frozen=True, slots=True)
class PriceTick:
    """来源于 /v2/commerce/prices 的聚合报价快照。

    端点语义（§1.3，不要反推）：
        buys.unit_price  = 最高买价 best bid
        sells.unit_price = 最低卖价 best ask
    """

    source_id: str
    item_id: int
    observed_at: datetime
    cycle_id: str
    buy_unit_price: int | None  # best bid（铜币整数）
    buy_quantity: int | None  # 买盘总量
    sell_unit_price: int | None  # best ask
    sell_quantity: int | None  # 卖盘总量
    whitelisted: bool
    quality: Quality = "COMPLETE"

    @property
    def key(self) -> tuple[int, int, int, int]:
        """change gate 的比较键：(buy_px, buy_qty, sell_px, sell_qty)。"""
        return (
            self.buy_unit_price or 0,
            self.buy_quantity or 0,
            self.sell_unit_price or 0,
            self.sell_quantity or 0,
        )


@dataclass(frozen=True, slots=True)
class ListingLevel:
    """来源于 /v2/commerce/listings 的原始挂单档位。"""

    source_id: str
    item_id: int
    observed_at: datetime
    cycle_id: str
    side: Literal["buy", "sell"]
    unit_price: int  # > 0
    quantity: int  # >= 0
    listings: int  # 该价位的挂单笔数 >= 1


@dataclass(frozen=True, slots=True)
class PriceAggregate:
    """domain.pricing 由 ListingLevel 序列算出的每分钟/每小时派生。"""

    item_id: int
    bucket_ts: datetime
    best_bid: int | None
    best_ask: int | None
    spread: int | None  # best_ask - best_bid；必须 >= 0，由一致性规则校验
    mid: int | None
    vwap_buy: float | None
    vwap_sell: float | None
    depth_buy: int
    depth_sell: int
    samples: int


@dataclass(frozen=True, slots=True)
class ItemMeta:
    source_id: str
    item_id: int
    name: str
    item_type: str | None
    rarity: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Announcement:
    """来源：官方 RSS/Atom。"""

    source_id: str
    guid: str  # 自然主键（优先 <guid>；缺失用 sha1(link|title)）
    title: str
    link: str
    published_at: datetime | None
    summary: str | None
    content_hash: str  # sha256(title|summary|link) 前 32 hex
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class AlertEvent:
    alert_id: str  # uuid4
    dedupe_key: str  # sha1(source_id|rule|item_id|cooldown_bucket)
    rule: str  # 见 config/alerts.yaml 的 rule id
    source_id: str
    item_id: int | None
    severity: Literal["info", "warn", "critical"]
    metric: str  # 'sell_unit_price' | 'spread' | 'depth_sell' | ...
    prev_value: float | None
    curr_value: float | None
    delta_pct: float | None
    message: str
    fired_at: datetime
    cooldown_until: datetime
    acknowledged: bool = False


@dataclass(frozen=True, slots=True)
class ConsistencyViolation:
    rule: str
    item_id: int | None
    field: str
    observed: str
    expected: str
    severity: Literal["warn", "critical"]


@dataclass(frozen=True, slots=True)
class Politeness:
    """每主机的礼貌预算；adapter 声明，config 钳制。"""

    rps: float
    burst: int
    concurrency: int
    min_interval_seconds: int
    respect_max_age: bool = True
    daily_request_budget: int | None = None  # None = 不限制

    def clamped(self, cap_rps: float) -> Politeness:
        """按官方额度硬钳制（C3）。只允许调低，不允许调高。"""
        return Politeness(
            rps=min(self.rps, cap_rps),
            burst=self.burst,
            concurrency=self.concurrency,
            min_interval_seconds=self.min_interval_seconds,
            respect_max_age=self.respect_max_age,
            daily_request_budget=self.daily_request_budget,
        )


@dataclass(frozen=True, slots=True)
class RateState:
    host: str
    current_rps: float
    capacity: int
    tokens: float
    consumed_total: int
    throttled_total: int
    last_penalty_at: datetime | None


# ===========================================================================
# 编排层报告结构（§13）
# ===========================================================================
@dataclass(frozen=True, slots=True)
class CycleContext:
    cycle_id: str
    source_id: str
    started_at: datetime
    cursor: Cursor | None = None
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class CycleReport:
    cycle_id: str
    source_id: str
    pages: int = 0
    records: int = 0
    written: int = 0
    skipped_unchanged: int = 0
    rejects: int = 0
    alerts: int = 0
    quality: Quality = "COMPLETE"
    error_class: ErrorClass = ErrorClass.OK
    duration_ms: float = 0.0
    drift_ratio: float = 0.0


@dataclass(frozen=True, slots=True)
class FanoutReport:
    pages: int = 0
    records: int = 0
    rejects: int = 0
    drift_ratio: float = 0.0
    duplicates: int = 0
    page_totals: tuple[int, ...] = ()
    quality: Quality = "COMPLETE"


@dataclass(frozen=True, slots=True)
class HealthReport:
    db_size_mb: float = 0.0
    last_cycle: str | None = None
    items_total: int = 0
    alerts_24h: int = 0
    rejects_24h: int = 0
    limiter: tuple[RateState, ...] = ()
    breaker: Mapping[str, str] = field(default_factory=dict)


# ===== 必须在四个实体类之后声明（§7.2 末） =====
type Record = PriceTick | ListingLevel | ItemMeta | Announcement
#: `(table_name, rows)`，交给 `SnapshotSink.submit`
type WriteBatch = tuple[str, Sequence[tuple]]
