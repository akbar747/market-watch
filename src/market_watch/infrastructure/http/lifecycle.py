# src/market_watch/infrastructure/http/lifecycle.py
"""优雅退出：signal / 事件、TaskGroup 取消、队列 drain。

架构依据：docs/01-architecture.md §4.6。

Windows 注意（Phase 3a 要打 exe）：
**Windows 没有可靠的 `SIGTERM`**，只有 `SIGINT`（Ctrl+C / `WM_CLOSE`）。因此
signal handler 里**禁止** sync await，只通过 `loop.call_soon_threadsafe` 置 Event。

退出序列固定为：
    Event.set() → 停止 scheduler 接受新周期 → TaskGroup 发送 cancel（不新起任务）
    → writer.drain(timeout=30s) → PRAGMA optimize → pool.aclose_all() → aconn.close() → 退出码 0
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
import json
from pathlib import Path
import signal
import sys
from typing import Final

from ...domain.models import iso_z, utc_now
from ...logging_setup import get_logger

__all__ = [
    "DEFAULT_SHUTDOWN_GRACE_S",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "ShutdownSignal",
    "install_signal_handlers",
    "run_until_stopped",
]

_log = get_logger("market_watch.lifecycle")

DEFAULT_SHUTDOWN_GRACE_S: Final[float] = 30.0
EXIT_OK: Final[int] = 0
EXIT_INTERRUPTED: Final[int] = 130  # 双 Ctrl+C / SIGINT


class ShutdownSignal:
    """退出信号。Windows 下只有 SIGINT 可靠，SIGTERM 尽力而为。"""

    __slots__ = ("count", "reason", "requested")

    def __init__(self) -> None:
        self.requested = asyncio.Event()
        self.reason: str | None = None
        self.count: int = 0

    def request(self, reason: str) -> None:
        """触发一次退出请求（可重复调用：第二次会升级为强制退出）。

        Args:
            reason:  Human-readable 原因（`'signal:SIGINT'` 之类），直接进日志。
        """
        self.count += 1
        self.reason = reason
        self.requested.set()
        _log.warning("shutdown.requested reason=%s count=%d", reason, self.count)

    @property
    def is_set(self) -> bool:
        """是否已收到过退出请求。"""
        return self.requested.is_set()


def install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: ShutdownSignal) -> None:
    """注册 SIGINT（全平台）与 SIGTERM（Unix）。handler 内**只置 Event**（禁止 sync await）。

    Args:
        loop: 当前事件循环。
        stop: 退出信号对象。

    注意：Windows 的 `loop.add_signal_handler` 会抛 `NotImplementedError`，
    这里自动回落到 `signal.signal + loop.call_soon_threadsafe`；非主线程安装失败
    只告警、不抛（崩溃前不该因为信号处理器的注册细节挂掉进程）。
    """
    for sig in (signal.SIGINT,):
        try:
            loop.add_signal_handler(sig, stop.request, f"signal:{sig.name}")
        except (NotImplementedError, RuntimeError):  # noqa: SIM105  # pragma: no cover
            # Windows：add_signal_handler 不支持 → 回落到 signal.signal + call_soon_threadsafe
            def _handler(signum: int, _frame: object) -> None:
                loop.call_soon_threadsafe(stop.request, f"signal:{signum}")

            try:
                signal.signal(sig, _handler)
            except ValueError:  # 非主线程
                _log.warning("signal.install_failed sig=%s（非主线程）", sig)  # noqa: SIM105
    if sys.platform != "win32":
        with suppress(NotImplementedError, RuntimeError):  # pragma: no cover
            loop.add_signal_handler(signal.SIGTERM, stop.request, "signal:SIGTERM")


async def run_until_stopped(
    start: Callable[[ShutdownSignal], Awaitable[None]],
    *,
    shutdown_grace_s: float = DEFAULT_SHUTDOWN_GRACE_S,
    drain: Callable[[float], Awaitable[bool]] | None = None,
    finalize: Sequence[Callable[[], Awaitable[None]]] = (),
    pending_dir: Path | None = None,
    cycle_id: str = "",
) -> int:
    """跑主循环直到收到退出信号，然后执行固定退出序列。

    - `drain(timeout_s)`：把队列里已解析但未落库的数据写完；返回 True 表示写完。
    - 若在 `shutdown_grace_s` 内 drain 不完：**记 ERROR 并保留未落库批次到
      `data/pending/<cycle_id>.jsonl`**（重启后 `--resume` 重放），拒绝静默丢数据。
    - `finalize`：PRAGMA optimize / pool.aclose_all() / aconn.close() 等。
    """
    stop = ShutdownSignal()
    loop = asyncio.get_running_loop()
    install_signal_handlers(loop, stop)

    task = asyncio.create_task(start(stop))

    # 双 Ctrl+C → 立刻全部取消并退 130（§4.6-4）
    watcher = asyncio.create_task(_interrupt_watcher(stop, task))

    # 等退出信号；本协程若被上层取消，静默继续走退出序列（不吞掉 CancelledError
    # 之外的任何语义，取消本身就是「退出」的一种形式）
    with suppress(asyncio.CancelledError):
        await stop.requested.wait()

    # 1) 停止调度（由 start 内部的 stop 检查负责）
    _log.info("shutdown.sequence begin grace=%.0fs", shutdown_grace_s)

    # 2) 取消主循环
    if not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=shutdown_grace_s)
        except (TimeoutError, asyncio.CancelledError):
            _log.error("shutdown.main_task_timeout grace=%.0fs", shutdown_grace_s)

    # 3) drain
    if drain is not None:
        try:
            ok = await asyncio.wait_for(drain(shutdown_grace_s), timeout=shutdown_grace_s + 5.0)
        except TimeoutError:
            ok = False
        if not ok:
            _log.error(
                "shutdown.drain_incomplete grace=%.0fs -> 保留未落库批次，拒绝静默丢数据",
                shutdown_grace_s,
            )
            if pending_dir is not None:
                _write_pending_marker(pending_dir, cycle_id)

    # 4) finalize（PRAGMA optimize → pool.aclose_all() → aconn.close()）
    for hook in finalize:
        try:
            await hook()
        except Exception as exc:  # noqa: BLE001 - 收尾失败不得改变退出码语义
            _log.error("shutdown.finalize_failed err=%s", exc)

    watcher.cancel()
    _log.info("shutdown.sequence done exit=%d", EXIT_OK)
    return EXIT_OK


async def _interrupt_watcher(stop: ShutdownSignal, task: asyncio.Task) -> None:
    """第二次 Ctrl+C：立即取消所有任务并退出码 130。"""
    while True:
        await stop.requested.wait()
        if stop.count >= 2:
            _log.error(
                "shutdown.forced count=%d -> 取消全部任务，退出码 %d", stop.count, EXIT_INTERRUPTED
            )
            for t in asyncio.all_tasks():
                if t is not asyncio.current_task():
                    t.cancel()
            raise SystemExit(EXIT_INTERRUPTED)
        stop.requested.clear()
        await asyncio.sleep(0.5)


def _write_pending_marker(pending_dir: Path, cycle_id: str) -> None:
    pending_dir.mkdir(parents=True, exist_ok=True)
    path = pending_dir / f"{cycle_id or 'unknown'}.jsonl"
    payload = {
        "cycle_id": cycle_id,
        "written_at": iso_z(utc_now()),
        "note": "优雅退出 drain 未完成；重启后用 --resume 重放本文件",
    }
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        _log.error("shutdown.pending_written path=%s", path)
    except OSError as exc:
        _log.error("shutdown.pending_write_failed path=%s err=%s", path, exc)
