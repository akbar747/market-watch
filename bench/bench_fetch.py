# bench/bench_fetch.py
"""吞吐/延迟压测入口：打**本地 loopback mock server**（C7 硬性约束）。

合规约束：只允许打 loopback，且**不得**提供 force 逃生开关（policy-check 用子串匹配识别，文案里也不能出现该字面量）
（`market-watch policy-check` 第 9 项会检查这两点）。

用法::

    uv run python bench/bench_fetch.py --pages 8 --rps 8
    uv run python bench/bench_fetch.py --url http://127.0.0.1:8731/v2/test

实现要点：
- mock 服务端用标准库 `http.server.ThreadingHTTPServer` 起在随机回环端口，不引第三方；
- 客户端用 `httpx.AsyncClient` 串行/限流地打，观测实测 rps 与 P50/P99；
- 数值均为**本机实测**（无官方网络参与），不要当 benchmark 跟线上源比较。
"""

from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import random
import statistics
import threading
import time
from urllib.parse import parse_qs, urlparse

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: 压测请求的伪 UA；不得伪装成官方客户端
BENCH_UA: str = "market-watch-bench/0.1.0 (+local-benchmark, loopback only)"

_PAGE_TOTAL: int = 8
_PAGE_SIZE: int = 200


class _SyntheticHandler(BaseHTTPRequestHandler):
    """按 ?page=N 生成合成分页 JSON，带固定 X-Page-Total / X-Result-Total 头。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # 压测期间静音访问日志
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            page = int(query.get("page", ["0"])[0])
        except ValueError:
            page = 0
        page = max(0, min(page, _PAGE_TOTAL - 1))
        body = json.dumps(
            [{"id": page * _PAGE_SIZE + i, "whitelisted": False} for i in range(_PAGE_SIZE)]
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Page-Total", str(_PAGE_TOTAL))
        self.send_header("X-Page-Size", str(_PAGE_SIZE))
        self.send_header("X-Result-Total", str(_PAGE_TOTAL * _PAGE_SIZE))
        self.send_header("Cache-Control", "public, max-age=120")
        self.end_headers()
        self.wfile.write(body)


def assert_loopback(base_url: str) -> str:
    """C7：压测目标必须是 loopback。命中即 `SystemExit`，**没有任何逃生开关**。"""
    host = urlparse(base_url).hostname or ""
    if host.lower() not in LOOPBACK_HOSTS:
        raise SystemExit(
            f"[C7 违规] 压测目标 {base_url!r} 不是 loopback 地址。\n"
            "压测只允许打本地 mock server（127.0.0.1 / localhost / ::1），"
            "不得对任何官方/第三方端点压测。禁止使用 force 开关绕过。"
        )
    return host


def _start_mock_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SyntheticHandler)
    thread = threading.Thread(target=server.serve_forever, name="mock-server", daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}/v2/commerce/prices"


class _Pacer:
    """全局发放节拍器：第 n 个请求（n 从 0 起）的**启动**时刻不早于 `t0 + n/rate`。

    ★ 必须做成全局节拍而不是「每个 task 各自 `sleep(1/rate)`」——后者在并发 C 下
    实测会跑到约 C×rate（本脚本第一版就踩了这个坑：`--rps 8 --pages 16` 实测
    出 13.6 rps，是请求值的 1.7 倍，属于假数据）。
    """

    __slots__ = ("_n", "_rate", "_t0")

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._n = 0
        self._t0 = time.monotonic()

    async def wait(self) -> None:
        target = self._t0 + (self._n / self._rate)
        self._n += 1
        delta = target - time.monotonic()
        if delta > 0:
            await asyncio.sleep(delta)


async def _run(base_url: str, pages: int, rps: float, concurrency: int) -> dict[str, float]:
    import httpx

    latencies: list[float] = []
    pacer = _Pacer(rps) if rps > 0 else None
    started = time.monotonic()
    sem = asyncio.Semaphore(max(1, concurrency))

    # trust_env=False 是硬性要求，不能省：
    #   本机（含 CI 与开发机）常挂着 HTTP_PROXY/http_proxy，httpx 默认 trust_env=True
    #   会把**回环压测流量也送进代理**——代理拿到绝对形式 URI 后编码成
    #   `http%3A//127.0.0.1:PORT/api/...` 当 path 转发，落地必然 404；连不上则 502。
    #   实测踩坑：`--url http://127.0.0.1:8765/api/latest` 同一 client 里
    #   部分请求 200、部分 404/502，非确定性，很容易误判成服务端 bug。
    #   生产端 fetcher 也是 trust_env=False（见 policy-check「代理」项），这里保持一致。
    # 复用单个 client（生产端也是这么做的）：每次请求新建 AsyncClient 等于
    # 每趟都重新握手，测出来的延迟没有可比性。
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), trust_env=False) as client:

        async def one(page: int) -> None:
            async with sem:
                if pacer is not None:
                    await pacer.wait()
                t0 = time.perf_counter()
                resp = await client.get(base_url, params={"page": page})
                resp.raise_for_status()
                latencies.append(time.perf_counter() - t0)

        await asyncio.gather(*(one(p) for p in range(pages)))
    elapsed = time.monotonic() - started
    latencies.sort()
    return {
        "target": base_url,
        "pages": float(pages),
        "observed_rps": round(pages / elapsed, 3) if elapsed else 0.0,
        "elapsed_s": round(elapsed, 3),
        "p50_ms": round(latencies[int(len(latencies) * 0.5)] * 1000, 2) if latencies else 0.0,
        "p99_ms": (
            round(latencies[min(len(latencies) - 1, int(len(latencies) * 0.99))] * 1000, 2)
            if latencies
            else 0.0
        ),
        "latency_median_ms": round(statistics.median(latencies) * 1000, 2) if latencies else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地 loopback 压测（C7）")
    parser.add_argument("--url", default=None, help="压测 URL，默认起本地 mock server")
    parser.add_argument("--pages", type=int, default=_PAGE_TOTAL)
    parser.add_argument(
        "--rps", type=float, default=0.0, help=">0 时按该速率节流（本地自限，非官方限制）"
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20261003)
    args = parser.parse_args(argv)

    random.seed(args.seed)

    if args.url:
        assert_loopback(args.url)  # 自定义 URL 也必须过 loopback 校验
        base_url, server = args.url, None
    else:
        server, base_url = _start_mock_server()

    try:
        report = asyncio.run(_run(base_url, args.pages, args.rps, args.concurrency))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
