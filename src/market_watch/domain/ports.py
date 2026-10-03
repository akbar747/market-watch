# src/market_watch/domain/ports.py
"""全部外部能力的 Protocol（端口）。application 只认这些协议，不认具体实现。

架构依据：docs/01-architecture.md §7.3（SourceAdapter 最小接口契约）、§13（签名清单）。

依赖方向：infrastructure 实现这些 Protocol；application 通过构造函数注入拿到实例，
因此 application 不需要 import infrastructure（避免循环，§3 铁律 2）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from .errors import ErrorClass
from .models import (
    AlertEvent,
    Cursor,
    FetchTask,
    ParseResult,
    Politeness,
    RateState,
    ResponseView,
    WriteBatch,
)

__all__ = [
    "AlertSink",
    "Breaker",
    "Fetcher",
    "RateLimiter",
    "SnapshotSink",
    "SourceAdapter",
]


@runtime_checkable
class SourceAdapter(Protocol):
    """新增一个官方公开源 = 实现这 6 个成员，采集内核一行不改。

    五条硬规则（§7.3，契约测试逐条跑）：
      1. `initial_tasks` / `next_tasks` / `parse` / `cursor_from` 必须是**同步纯函数**
         —— 不得 await、不得 IO、不得读时钟（时间由调用方注入）。
      2. `next_tasks` **必须可终止**：`page + 1 >= X-Page-Total` 或空数组时返回 `()`。
      3. `parse` 不得因单条坏数据中断整页：坏记录进 `consistency_failures`。
      4. `politeness` 是声明值，实际生效值被 Config 钳制，adapter 无权突破。
      5. 一个 adapter 只能用一个 `host`；**禁止**在 adapter 内 new httpx.Client。
    """

    # ---- 声明区 ----
    source_id: str  # 全局唯一，落 natural key
    kind: str  # 'price' | 'orderbook' | 'item_meta' | 'announcement'
    host: str  # 单一主机；跨主机请在仓库里建两个 adapter
    politeness: Politeness  # 声明式礼貌预算（会被 Config 钳制）

    # ---- 任务区（同步、纯函数、不发请求） ----
    def initial_tasks(self, cursor: Cursor | None) -> Sequence[FetchTask]:
        """给出本轮首批任务（通常是 page=0，或单次请求的 URL）。

        Args:
            cursor: 上轮落盘的游标（`ETag` / `Last-Modified` / 最大 `published_at`）；
                `None` 表示首次运行。

        Returns:
            任务序列（通常 1 个）。空序列会被编排层视为「本轮无任务」并直接结束轮次。
        """
        ...

    def next_tasks(self, task: FetchTask, view: ResponseView) -> Sequence[FetchTask]:
        """依据响应头推进分页；不推进时返回 ()。禁止在此发请求。

        Args:
            task: 刚完成的那个任务（用于取 `page` / `strategy`）。
            view: 响应快照（`ResponseView`，不是 `httpx.Response`）。

        Returns:
            下一批任务；终止时返回空序列 `()`。见契约测试第 5 条的终止性要求。
        """
        ...

    # ---- 解析区（同步、纯函数） ----
    def parse(self, task: FetchTask, view: ResponseView) -> ParseResult:
        """把 payload 转成 domain 模型 + 可选的下游任务。

        抛 `PayloadError` / `SchemaError` 由内核分类（§6）；单条坏记录不得抛出，
        必须进 `ParseResult.consistency_failures`，否则整页会被丢掉。

        Args:
            task: 触发本次解析的任务（携带 `observed_at` / `cycle_id`，不得自读时钟）。
            view: 响应快照。

        Returns:
            `ParseResult`，含 `records` 与 `next_tasks`。
        """
        ...

    # ---- 增量区 ----
    def cursor_from(self, view: ResponseView, result: ParseResult) -> Cursor | None:
        """从 ETag / Last-Modified / 最大 published_at 导出下轮游标。

        Args:
            view: 响应快照。
            result: 本轮解析结果（可能为空集，此时应返回 `None` 表示不推进游标）。

        Returns:
            下轮游标字典；`None` = 无游标可用。
        """
        ...

    # ---- 合规区 ----
    def user_agent_suffix(self) -> str:
        """本 adapter 的 UA 后缀声明。

        Returns:
            形如 `'market-watch/0.1.0 (+https://you.example/contact)'`；
            最终 UA 由内核拼接（本方法不得自己拼完整 UA）。
        """
        ...


class RateLimiter(Protocol):
    """每主机令牌桶 + AIMD。唯一能放缓出站速度的地方（§4.2 第 3 层）。"""

    async def acquire(self, host: str, cost: float = 1.0) -> float:
        """取令牌，必要时等待；返回实际等待秒数。

        Args:
            host: 目标主机。
            cost: 令牌成本（重试默认 1.0，与首次请求同价）。

        Returns:
            实际等待秒数（0.0 = 未等待）。

        契约：`asyncio.CancelledError` 时**必须退还已扣令牌**，否则优雅退出后
        重启的进程会看到被吃空的桶（§4.2）。
        """
        ...

    def penalize(self, host: str, factor: float) -> float:
        """乘性减（429/420 → 0.5；读超时批量 / 池重建 → 0.7）。

        Args:
            host: 目标主机。
            factor: 降速系数，实现侧会 `min(1.0, max(0.01, factor))` 夹紧。

        Returns:
            降速后的 rps（不高于合规上限 `EFFECTIVE_RPS_CAP`）。
        """
        ...

    def reward(self, host: str) -> float:
        """加性增：连续 `ADAPTIVE_HEALTHY_WINDOW_S`（300s）无错误后 `rps *= 1.10`。

        Args:
            host: 目标主机。

        Returns:
            恢复后的 rps，天花板为 `min(声明初始值, EFFECTIVE_RPS_CAP)`。
        """
        ...

    def configure(self, host: str, rps: float, capacity: int) -> None:
        """注册/更新某主机的桶参数（幂等）。

        ⚠️ 语义铁律：已注册主机的 `rps` **只降不升**。想提速的唯一合法路径是
        「用满 `page_size=200` 减少请求数」，不是提高 rps（C3，§11.2）。

        Args:
            host: 目标主机。
            rps: 期望速率（会被 `policy.effective_rps` 钳到 `EFFECTIVE_RPS_CAP`）。
            capacity: 桶容量（突发上限）。
        """
        ...

    def snapshot(self, host: str) -> RateState:
        """取某主机的限流快照（看板 `/api/health` 用）。"""
        ...

    def hosts(self) -> Sequence[str]:
        """返回已注册/已建桶的主机列表（升序）。"""
        ...


class Breaker(Protocol):
    """每 host 熔断状态机 closed/open/half-open + 探针（§5.3）。"""

    def before(self, host: str) -> bool:
        """是否允许发起请求。

        Args:
            host: 目标主机。

        Returns:
            `True` 表示放行。

        Raises:
            OpenCircuit: 状态为 `open`（或 half_open 探针已用尽）时抛出，
                调用方应把它转成「本轮短路、记录失败、不重试」。
        """
        ...

    def record(self, host: str, err: ErrorClass) -> None:
        """记录一次失败（含 304 之外的成功分类之外的任何非 OK 结果）。

        Args:
            host: 目标主机。
            err: 错误分类（`domain.errors.ErrorClass`）。
        """
        ...

    def record_success(self, host: str) -> None:
        """记录一次成功，供 half_open → closed 的闭合并复位 `open_duration_s`。

        ★ **实现不变式（别改坏，这是 Phase 2 修过的一个真 bug）**：
        实现体第一行**必须**先调 `self.state(host)`（触发时间驱动的
        `open → half_open` 迁移），之后才能读 `self._state(host).state` 再判断
        「是否处于半开」。否则一个 open 到期、却没人显式调过 `state()` 的 host，
        `record_success` 会一直读到 OPEN，探针成功分支永远走不到，
        电路只能靠进程重启复位。

        Args:
            host: 目标主机。
        """
        ...

    def state(self, host: str) -> str:
        """查询（并顺带推进时间驱动的迁移）某主机的状态。

        Args:
            host: 目标主机。

        Returns:
            `'closed'` / `'open'` / `'half_open'` 三者之一。

        Raises:
            OpenCircuit: 本方法**不抛**；抛出发生在其调用方 `before()`。
        """
        ...

    def snapshot(self) -> Mapping[str, str]:
        """全部 host 的 `{host: state}` 映射（看板健康页用）。"""
        ...


class Fetcher(Protocol):
    """全仓唯一出网口。顺序：robots → 熔断 → 限流 → 请求 → 分类（§13）。"""

    async def fetch(self, task: FetchTask) -> tuple[ResponseView | None, ErrorClass, int]:
        """执行一次（含内部重试的）出网。

        Args:
            task: 出网任务（`method` 必为 GET/HEAD，`content` 必为 None）。

        Returns:
            `(view, error_class, attempts)`。`view` 为 `None` 表示未拿到任何响应；
            `attempts` 含首次，≥1。
        """
        ...

    async def aclose(self) -> None: ...


class SnapshotSink(Protocol):
    """落库端口。实现见 infrastructure/storage/writer.py（单写线程 + Queue）。"""

    def submit(self, batch: WriteBatch) -> None: ...

    async def flush(self, timeout_s: float = 10.0) -> None: ...

    async def aclose(self) -> None: ...

    @property
    def depth(self) -> int:
        """当前队列积压深度，用于背压与看板显示。"""
        ...


class AlertSink(Protocol):
    """告警出口。实现见 `infrastructure/alerting/{stdout,sqlite}_sink.py`。

    实现约定：`emit` **不得因单个 sink 故障影响其它 sink**（`alert_service` 已做
    隔离），本接口本身保持同步、不抛、不重试——告警是旁路，不是采集主链路。
    """

    def emit(self, event: AlertEvent) -> None:
        """投递一条告警。

        Args:
            event: 已组装好的 `AlertEvent`（含 `dedupe_key` 与 `cooldown_until`）。
        """
        ...
