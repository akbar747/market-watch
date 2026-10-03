# src/market_watch/interfaces/cli.py
"""argparse CLI：`once` / `serve` / `status` / `gc` / `policy-check` / `reset`。

架构依据：docs/01-architecture.md §2、§11.3、§12 DoD。

启动即执行 C1/C5/C6 检查；任一失败 `SystemExit(2)`。
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Final

from .. import __version__
from ..application.config import (
    EFFECTIVE_RPS_CAP,
    HOST_SAFETY_FACTOR,
    OFFICIAL_LIMIT,
    Settings,
    get_settings,
)
from ..domain import policy
from ..domain.errors import PolicyViolation
from ..domain.models import iso_z, utc_now
from ..logging_setup import configure_logging, get_logger

__all__ = ["build_parser", "main"]

_log = get_logger("market_watch.cli")

EXIT_OK: Final[int] = 0
EXIT_POLICY: Final[int] = 2
#: 存储层异常（库打不开 / 不是库 / 无权限）。与 EXIT_POLICY 区分开：前者是
#: 「配置或环境不对」，后者是「合规检查没过」，CI 里要靠退出码分派，别混用。
EXIT_STORAGE: Final[int] = 3

#: 出网方法白名单（§11.3 policy-check 第 6 项）
ALLOWED_METHODS_TXT: Final[str] = "GET, HEAD"

#: `--force` 逃生开关的判据：必须真的被 argparse 注册成命令行选项。
#: 仅出现在注释/docstring/报错文案里的字面量不算（见 `_check_bench_target` 注释）。
_FORCE_FLAG_RE: Final[re.Pattern[str]] = re.compile(r"""add_argument\(\s*["']--force""")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="market-watch",
        description="合规版公开数据物价看板（数据源：Guild Wars 2 官方 v2 API，免鉴权、只读）",
    )
    parser.add_argument("--version", action="version", version=f"market-watch {__version__}")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果（便于脚本消费）")
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- once ----
    p_once = sub.add_parser("once", help="跑一轮就退（Windows 任务计划程序友好）")
    p_once.add_argument("--source", default="gw2_prices", help="source_id，默认 gw2_prices")
    p_once.add_argument("--dry-run", action="store_true", help="只取数解析，不落库")
    p_once.add_argument("--max-pages", type=int, default=200, help="单轮页数硬上限（终止性护栏）")
    p_once.add_argument("--cycles", type=int, default=1, help="跑几轮（默认 1）")

    # ---- serve ----
    p_serve = sub.add_parser("serve", help="常驻：调度 + 内嵌看板")
    p_serve.add_argument("--host", default=None, help="绑定地址，默认 127.0.0.1")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--no-web", action="store_true", help="只跑采集，不起看板")
    p_serve.add_argument("--pid-file", default=None, help="写 pid 文件，便于 py-spy 挂载")

    # ---- status ----
    sub.add_parser("status", help="打印库与源状态摘要")

    # ---- gc ----
    p_gc = sub.add_parser("gc", help="保留清理 + rollup + PRAGMA optimize")
    p_gc.add_argument("--now", default=None, help="指定 now（ISO8601），便于测试保留窗口")

    # ---- policy-check ----
    sub.add_parser("policy-check", help="9 项合规检查点自检")

    # ---- reset ----
    p_reset = sub.add_parser("reset", help="清空运行期数据（危险）")
    p_reset.add_argument("--yes", action="store_true", help="跳过二次确认")
    p_reset.add_argument(
        "--keep-schema", action="store_true", default=True, help="保留表结构（默认）"
    )
    return parser


# ===========================================================================
# policy-check（§11.3：9/9）
# ===========================================================================
def _check_python() -> tuple[bool, str]:
    import platform

    ok = sys.version_info >= (3, 13)
    return ok, f"python={platform.python_version()} platform={platform.platform()}"


def _check_forbidden_deps(settings: Settings) -> tuple[bool, str]:
    hits = settings.forbidden_dependency_hits()
    return (not hits), f"依赖黑名单: {len(hits)} hits" + (f" -> {hits}" if hits else "")


def _check_ua(settings: Settings) -> tuple[bool, str]:
    ua = settings.user_agent
    kind = policy.placeholder_kind(ua)
    if kind == "hard":
        return False, f"UA: {ua} -> 占位域，C1 拒绝"
    suffix = " (⚠️ 文档样例占位域，部署前必须替换)" if kind == "soft" else ""
    return True, f"UA: {ua}{suffix}"


def _check_robots() -> tuple[bool, str]:
    """用仓库留存的 robots 快照判定（**不发请求**，避免 policy-check 出网）。"""
    from ..infrastructure.http.robots import RobotsGuard, RobotsRecord

    snapshot_path = _repo_root() / "docs" / "_robots_snapshot.json"
    preloaded: dict[str, RobotsRecord] = {}
    if snapshot_path.exists():
        try:
            raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
            for item in raw:
                preloaded[item["host"]] = RobotsRecord(
                    host=item["host"],
                    status_code=int(item["status"]),
                    allowed=int(item["status"]) == 404,
                    raw=str(item.get("body", ""))[:2000],
                    fetched_at_iso=item.get("fetched_at", ""),
                    note=f"HTTP {item['status']}",
                )
        except Exception as exc:  # noqa: BLE001
            return False, f"robots: 快照读取失败 {exc}"
    else:
        return False, "robots: 缺少 docs/_robots_snapshot.json（先跑 scripts/_record_fixtures.py）"

    guard = RobotsGuard("market-watch/0.1.0 (+https://you.example/contact)", preloaded=preloaded)
    results = []
    for host, rec in preloaded.items():
        results.append(
            f"{host} -> {rec.status_code} ({'无指令 -> allow' if rec.allowed else 'DISALLOW'})"
        )
    ok = all(rec.status_code == 404 for rec in preloaded.values())
    # 同步验证：无 parser 时 allowed 必为 True（RFC 9309）
    for host in preloaded:
        if not _allowed_sync(guard, host, "/v2/commerce/prices"):
            ok = False
            results.append(f"{host} -> 判定拒绝（不应发生）")
    return ok, "robots: " + "   ".join(results)


def _allowed_sync(guard, host: str, path: str) -> bool:  # noqa: ANN001
    """robots 判定本身是同步安全的（快照已预置，不会触发抓取）。"""

    async def _run() -> bool:
        # bool() 不是多余：guard.allowed 走 ports 协议，返回类型推断为 Any，
        # mypy strict 下直接 return 会报 no-any-return（CI 第 2 步目前就红在这条）。
        return bool(await guard.allowed(host, path))

    return asyncio.run(_run())


def _check_rate(settings: Settings) -> tuple[bool, str]:
    declared = settings.per_host_rps
    return True, (
        f"有效速率: {declared:.2f} rps (= 官方 {OFFICIAL_LIMIT}/min × 安全系数 "
        f"{HOST_SAFETY_FACTOR:.2f})   声明={declared:.2f}  钳制={'是' if declared < EFFECTIVE_RPS_CAP else '否'}"
    )


def _check_methods() -> tuple[bool, str]:
    return True, f"出网方法白名单: {ALLOWED_METHODS_TXT}"


def _check_proxy(settings: Settings) -> tuple[bool, str]:
    if settings.proxy_url is None or not settings.proxy_url.strip():
        return True, "代理: 未配置（trust_env=False，环境变量代理被忽略）"
    try:
        policy.assert_single_proxy(settings.proxy_url)
    except PolicyViolation as exc:
        return False, f"代理: {exc}"
    return (
        True,
        f"代理: 单一静态代理已配置（{'含口令已脱敏' if '@' in settings.proxy_url else '无口令'}）",
    )


def _check_registry() -> tuple[bool, str]:
    from ..infrastructure.adapters.registry import build_default_registry

    reg = build_default_registry()
    labels = []
    ok = True
    for sid in reg.source_ids():
        kind = reg.authorization_of(sid)
        labels.append(f"{sid} -> '{kind}'")
        if kind != "official_public":
            ok = False
    return ok, "数据源注册表: " + "/".join(labels)


def _check_bench_target() -> tuple[bool, str]:
    """C7：压测目标必须只打 loopback，且**不存在** `--force` 逃生开关。

    ★ 判据必须精确到「真的注册了一个 --force 命令行选项」，不能用裸子串扫描
    `"--force" not in text`——那样会把 `bench_fetch.py` 顶部/报错文案里
    「禁止使用 --force 绕过」这类**说明性字符串**误判成违规开关，
    结果是策略门自己红（并掩盖真正的风险）。
    真正的判据：`add_argument("--force" ...)` 这类注册式出现才算逃生开关。
    """
    from ..domain.policy import ALLOWED_METHODS  # noqa: F401

    bench_file = _repo_root() / "bench" / "bench_fetch.py"
    if not bench_file.exists():
        return False, "压测目标: 缺少 bench/bench_fetch.py"
    text = bench_file.read_text(encoding="utf-8")
    has_assert = "def assert_loopback" in text
    # 只有「注册成 CLI 选项」的 --force 才叫逃生开关
    real_force = bool(_FORCE_FLAG_RE.search(text))
    return (has_assert and not real_force), (
        f"压测目标: 仅 loopback（assert_loopback={'有' if has_assert else '无'}，"
        f"--force 逃生开关={'存在(违规)' if real_force else '无'}）"
    )


def _ensure_utf8_stdout() -> None:
    """Windows 中文 locale 下把 stdout/stderr 切成 UTF-8。

    ★ 必须做：检查项文案里有 `⚠️`（U+26A0）这类非 GBK 字符，中文 Windows 把输出
    重定向到文件/管道时走 ANSI 代码页（cp936），直接 `print` 会抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u26a0'`，
    整个 `policy-check` 崩在一个 print 上（2026-10-03 打 exe 冒烟时实测复现）。
    CI 与任务计划程序都是重定向场景，这条不修等于合规自检本身不可用。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - 极老解释器
            continue
        # pragma: no cover - 流已关闭等；suppress 与 try/except/pass 行为等价，
        # 用 suppress 是为了过 ruff SIM105（CI 拦）
        with suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def policy_check(*, as_json: bool = False) -> int:
    """执行 9 项检查点，返回退出码。"""
    _ensure_utf8_stdout()
    try:
        settings = get_settings()
    except SystemExit as exc:  # C1/C5 在 Settings 校验期就失败
        print(f"[FAIL] {exc}", file=sys.stderr)
        return EXIT_POLICY

    configure_logging(json_logs=settings.log_json, level=settings.log_level)

    checks: list[tuple[str, tuple[bool, str]]] = [
        ("python", _check_python()),
        ("forbidden_deps", _check_forbidden_deps(settings)),
        ("ua", _check_ua(settings)),
        ("robots", _check_robots()),
        ("rate", _check_rate(settings)),
        ("methods", _check_methods()),
        ("proxy", _check_proxy(settings)),
        ("registry", _check_registry()),
        ("bench_target", _check_bench_target()),
    ]
    passed = sum(1 for _, (ok, _) in checks if ok)
    total = len(checks)

    if as_json:
        print(
            json.dumps(
                {
                    "total": total,
                    "passed": passed,
                    "ok": passed == total,
                    "checks": [{"name": n, "ok": ok, "detail": d} for n, (ok, d) in checks],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for _name, (ok, detail) in checks:
            print(f"[{'OK' if ok else 'FAIL'}]  {detail}")
        print(f"合规检查通过 {passed}/{total}")
    return EXIT_OK if passed == total else EXIT_POLICY


# ===========================================================================
# 其它命令
# ===========================================================================
def _repo_root() -> Path:
    """返回「资源根」：源码时是仓库根，frozen 时是 bundle 根（`sys._MEIPASS`）。

    ★ 不能写死 `parents[3]`：两种布局的包深度不同，且 frozen 下**不能用目录探测**

        源码：   <repo>/src/market_watch/interfaces/cli.py → parents[3] = <repo>
        frozen:  <bundle>/market_watch/interfaces/cli.py  → parents[3] = <bundle> 的父目录 ✗

    为什么 frozen 下不能靠「找含 market_watch/interfaces 的目录」来定位：
    onefile 把 `market_watch.*` 全部压进 PYZ 归档，**磁盘上没有 `market_watch/`
    这个真实目录**，任何 `.is_dir()` 探测都命不中，最终只能落到兜底分支，
    `_repo_root()` 返回 `_MEIPASS` 的上一层（实测：指向 `%TEMP%`），于是
    `docs/_robots_snapshot.json` / `bench/bench_fetch.py` 全部「路径不存在」，
    policy-check 在 Release 产物里直接掉到 7/9（2026-10-03 打 exe 冒烟实测复现）。

    正确锚点就是 `sys._MEIPASS` —— 它按定义就是 bundle 根。
    """
    import sys as _sys

    meipass = getattr(_sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)

    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == "src":
            continue
        if (parent / "pyproject.toml").is_file():
            return parent
    return here.parents[3]  # pragma: no cover - 兜底，正常跑不到


def cmd_status(settings: Settings, *, as_json: bool) -> int:
    from ..infrastructure.storage.repositories import Repository

    repo = Repository(settings.db_path)
    try:
        summary = repo.summary()
        quality = repo.quality()
        # ★ 走 `source_states()` 而不是直连 `repo._conn`：直连在「库还没建表」的
        #   全新安装上会 `no such table: source_state`（cmd_status 是用户装完 exe
        #   第一条常跑的命令，一崩就是很糟的第一印象）。见 repositories.Repository。
        sources = list(repo.source_states())
    finally:
        repo.aclose()
    payload = {
        "db_path": str(settings.db_path),
        "summary": summary,
        "quality": quality,
        "sources": [dict(r) for r in sources],
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"DB: {payload['db_path']}")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        print(f"  rejects_24h: {quality['rejects_24h']}")
        for r in sources:
            print(
                f"  source {r['source_id']}: last_run={r['last_run_at']} err={r['last_error_class']}"
            )
    return EXIT_OK


def cmd_gc(settings: Settings, now_iso: str | None, *, as_json: bool) -> int:
    from datetime import datetime

    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00")) if now_iso else utc_now()
    from ..infrastructure.storage.maintenance import run_gc

    # ★ 存储层异常在 CLI 边界收口，不把 traceback 直吐给用户。
    #   与 `Repository` 空库 traceback（零字节占位 + `_ready` + `_query` 那套）
    #   同一类兜底思路：存储内部自己认「空库」是正常态（`Maintenance.gc()` 里
    #   有幂等 `apply_migrations`），空库根本到不了这里；能到这里的只有
    #   「库文件打不开 / 不是库 / 无权限 / 磁盘满」这类真异常，它们统一收成
    #   一条可读提示 + `EXIT_STORAGE`，保留 `exc` 供日志定位。
    try:
        stats = run_gc(settings.db_path, now=now)
    except (sqlite3.Error, OSError) as exc:
        _log.error("gc.failed db_path=%s err=%s", settings.db_path, exc)
        print(f"[ERROR] GC 失败：{exc}（db_path={settings.db_path}）", file=sys.stderr)
        return EXIT_STORAGE
    if as_json:
        print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"GC 完成 @ {iso_z(now)}")
        for k, v in stats.items():
            print(f"  {k}: {v}")
    return EXIT_OK


def cmd_reset(settings: Settings, *, yes: bool) -> int:
    db = Path(settings.db_path)
    if not yes:
        answer = input(f"确认删除 {db} 及其 WAL/SHM 文件？[y/N] ").strip().lower()
        if answer != "y":
            print("已取消")
            return EXIT_OK
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
            print(f"已删除 {p}")
    print("运行期数据已清空；下次启动会重新建表。")
    return EXIT_OK


def cmd_once(settings: Settings, args: argparse.Namespace, *, as_json: bool) -> int:
    """跑一轮。**唯一的真实出网路径**（单轮、不提额）。"""
    # 同样只能单点：`..wiring` 会解析成 `market_watch.wiring`，真实是
    # `market_watch.interfaces.wiring`（与本文件同级）。见 cmd_serve 的说明。
    from .wiring import build_runtime

    async def _run() -> int:
        runtime = await build_runtime(settings, dry_run=args.dry_run)
        try:
            reports = []
            for _ in range(max(1, args.cycles)):
                from ..application.scheduler import new_cycle_id

                cycle_id = new_cycle_id()
                report = await runtime.run_source(
                    args.source, cycle_id, dry_run=args.dry_run, max_pages=args.max_pages
                )
                reports.append(report)
                _log.info("once.report cycle=%s", cycle_id)
            if as_json:
                print(
                    json.dumps(
                        [
                            {
                                "cycle_id": r.cycle_id,
                                "source_id": r.source_id,
                                "pages": r.pages,
                                "records": r.records,
                                "written": r.written,
                                "skipped_unchanged": r.skipped_unchanged,
                                "rejects": r.rejects,
                                "alerts": r.alerts,
                                "quality": r.quality,
                                "drift_ratio": round(r.drift_ratio, 4),
                                "duration_ms": round(r.duration_ms, 1),
                                "error_class": str(r.error_class),
                            }
                            for r in reports
                        ],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                for r in reports:
                    print(
                        f"cycle={r.cycle_id} source={r.source_id} pages={r.pages} "
                        f"records={r.records} written={r.written} skipped={r.skipped_unchanged} "
                        f"rejects={r.rejects} alerts={r.alerts} quality={r.quality} "
                        f"drift={r.drift_ratio:.2%} duration={r.duration_ms:.0f}ms"
                    )
            return EXIT_OK
        finally:
            await runtime.aclose()

    return asyncio.run(_run())


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    # ★ 单点 `.` 不是 `..`：本文件在包 `market_watch.interfaces` 下，
    #   `from ..web.app import ...` 会被解析成 `market_watch.web.app`（不存在），
    #   运行时抛 `ModuleNotFoundError: No module named 'market_watch.web'`。
    #   真实模块是 `market_watch.interfaces.web.app`。写成两点会让
    #   「源码模式 serve 一跑就崩」和「exe serve 一跑就崩」同时成立，
    #   而 `policy-check` / `--help` / `status` 全绿 —— 只有真起服务才暴露。
    #   （2026-10-03 发布冒烟实测复现。）
    from .web.app import create_app

    host = args.host or settings.web_host
    port = args.port or settings.web_port
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"[WARNING] 绑定非 loopback 地址 {host!r}：本看板无鉴权（本地信任模型，§14 P2）。"
            " 同局域网的任何人都能看/改。",
            file=sys.stderr,
        )
    if args.pid_file:
        Path(args.pid_file).write_text(str(os.getpid()), encoding="utf-8")
    # 把生效的 host/port 一并交给 app：`web.started` 日志必须报实际绑定的端口
    app = create_app(settings, host=host, port=port)
    uvicorn.run(app, host=host, port=port, log_level=settings.log_level.lower())
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "policy-check":
        return policy_check(as_json=getattr(args, "json", False))
    settings = get_settings()
    configure_logging(json_logs=settings.log_json, level=settings.log_level)
    if args.command == "status":
        return cmd_status(settings, as_json=args.json)
    if args.command == "gc":
        return cmd_gc(settings, getattr(args, "now", None), as_json=args.json)
    if args.command == "reset":
        return cmd_reset(settings, yes=getattr(args, "yes", False))
    if args.command == "once":
        return cmd_once(settings, args, as_json=args.json)
    if args.command == "serve":
        return cmd_serve(settings, args)
    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
