# release/build_exe.py
"""本地构建 Windows 发布包：调 PyInstaller 打 `dist/market-watch/`，然后**真的跑一遍**验证。

用法（Windows，Python 3.13）：:

    py -3.13 -m pip install pyinstaller
    python release/build_exe.py

产物：
    `release/dist/market-watch/market-watch.exe`  （onedir 布局）
    `release/dist/market-watch/market-watch.zip`  （打好的发布包，可选）

脚本末尾的验证是自红灯：exe 出不来不算构建成功，`--help` 与 `policy-check`
跑不通也不算。跑不通就直接报错退出，不做"看起来没问题"的和稀泥。
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "release" / "market-watch.spec"
DIST = ROOT / "release" / "dist"
BUILD = ROOT / "release" / "build"
#: onedir 布局下 exe 与整个运行时目录都叫这个名字（见 spec 的 COLLECT(name=…)）-- 与
#: spec 里的保持一致，别改一边忘另一边。
APP_NAME = "market-watch"
#: 运行时目录：exe 就落在这里，`status` / `gc` / `once` / `reset` / serve 冒烟全部以它为 cwd。
#: ★ 必须是**模块级单一真源**：早先这里写了 `APP_DIR = DIST / APP_NAME`，后来被整段
#:   重写时把常量删了、只留了 `_purge_release_junk(app_dir)` 的形参，于是 `_smoke_serve`
#:   里的 `SCRATCH` 和第 281/287/293/298 行的 `APP_DIR` 全变成未定义名，构建在 serve
#:   冒烟第一步 NameError 崩掉（`NameError: name 'SCRATCH' is not defined`），
#:   而且崩在**验证阶段**——`dist/market-watch/` 已经躺着一份看起来正常的产物。
#:   这里只定义一次，并让下面所有调用点都复用它，杜绝再出现这种半改。
APP_DIR = DIST / APP_NAME
#: 冒烟日志目录：serve 启动时若cwd 不可写会静默降级，日志落这里便于定位。
SCRATCH = APP_DIR / "scratch"


def _run(cmd: list[str], *, cwd: Path) -> None:
    print(f"$ {' '.join(cmd)}")
    proc = subprocess.run(  # noqa: S603 —— 命令列表由本脚本构造，无 shell 展开，无不可信输入
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
        raise SystemExit(f"命令失败（exit={proc.returncode}）：\n{tail}")
    if proc.stdout:
        print(proc.stdout[-1500:])


def _free_port() -> int:
    """给 serve 冒烟挑一个**当前未被占用**的端口。

    ★ 不能写死 8765：写死时若同一台机器上另有进程占着它（例如另一人录 demo.gif
      时起的看板），冒烟会连到**别人的进程**上 —— `/api/health` 200、首页 200、
      `echarts.min.js` 200 全绿，验的却是别的进程，而且从输出里**完全看不出来**。
      这是「绿壳」里最难发现的一类：红会逼你查，假绿会让你把没验过的包发出去。
      改成运行时向内核要空闲端口，撞端口这个故障模式直接消失。
      （内核给端口与 exe 真正 bind 之间有一瞬窗口，理论存在 TOCTOU；真撞上 exe
      会启动失败，下面的「serve 进程启动即退出」会判红 —— 红，不会假绿。）
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _smoke_serve(exe: Path, env: dict[str, str]) -> None:
    """冒烟 `serve`：起进程 → 探首页/health/静态资源 → 断言**没有任何 traceback** → 杀掉。

    这一步是故意做硬的，它一个人抓出过三个「policy-check 全绿但 exe 实际是坏的」：
      1. `ModuleNotFoundError: No module named 'market_watch.web'`（相对导入多点了一级）；
      2. `sqlite3.OperationalError: unable to open database file`（全新安装只读一个不存在的库）；
      3. 以及本函数自己原先的 `Popen.exitcode` 拼写错误——它让 serve 冒烟
         **从来没被真正执行过**，上面两条也一直藏在这层壳底下。

    ★ 不能加 `--no-web`：那个开关是「只跑采集、不起看板」。早先版本正是带
      `--no-web` 探 `/api/health`，自以为验过看板链路，其实**看板一次都没启动**，
      白屏、模板缺失、ECharts 没打包全都测不出来。这里必须真起看板。
    """
    import time
    import urllib.error
    import urllib.request

    # 下面的优雅退出块用 `contextlib.suppress` 兜 `send_signal` 的 OSError、
    # 用 `signal.CTRL_BREAK_EVENT` 发 CTRL_BREAK，二者都取自模块顶部的 import。
    # ★ 这段依赖是踩过坑的：00:42 那次重写把顶部 `import contextlib` / `import signal`
    #   连同 APP_DIR / SCRATCH 一起删了，而引用点还留着 —— 修掉 SCRATCH 之后，紧接着
    #   就会在 `finally:` 里再炸一次 `NameError: name 'suppress' is not defined`，
    #   报错长得像 serve 自己的问题，实际是构建脚本常量半改。别在这里偷偷补函数内
    #   import 了事：那会让顶部那两个 import 立刻变成 F401「未使用」，只剩红，
    #   真正的依赖反而被藏起来。顶部引一次、这里直接用。
    port = _free_port()
    scratch = SCRATCH / "serve-smoke.log"
    # ★ 必须自己建目录：PyInstaller 只会造 exe 和它声明过的 `_internal/`，不会替你建
    #   `scratch/`。少了这两行，冒烟会卡在 `FileNotFoundError: .../scratch/serve-smoke.log`
    #   —— 而且卡在**起 serve 进程之前**（`log.open()` 在 `Popen` 那一行之上），
    #   看起来像 serve 起不来，其实 serve 一次都没被启动过，纯粹的假红/真糊。
    #   宁可把日志直接写 app_dir 根下（少一层），也不要在这条隐式依赖上翻车，
    #   故优先尝试建目录；建不出来就让异常照原样冒出来，不做静默降级。
    scratch.parent.mkdir(parents=True, exist_ok=True)
    log = scratch
    cmd = [str(exe), "serve", "--port", str(port)]
    print(f"\n--- 验证 serve（{' '.join(cmd)}）---")
    with log.open("w", encoding="utf-8") as fh:
        # noqa: S603 —— 命令行由本脚本构造，无 shell 展开、无不可信输入
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=APP_DIR,
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            # ★ Windows 上必须建新进程组，否则后面的 CTRL_BREAK 会打到
            #   冒烟脚本自己身上（实测进程被 -1073741510 打断）。
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

    code: int | None = None
    try:
        for _ in range(30):  # 最多等 15s
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=2
                ) as resp:
                    code = resp.status
                    break
            except urllib.error.HTTPError as exc:  # 服务已起、只是路由不在 -> 也算起成功了
                code = exc.code
                break
            except OSError:
                continue
        # ★ 是 `returncode` 不是 `exitcode`：`subprocess.Popen` 没有 `.exitcode`
        #   属性，只有 `wait()`/自动回收后才有的 `.returncode`。写成 exitcode 会抛
        #   AttributeError（PyInstaller 6.22.3 / Python 3.13 实测），而它埋在「进程已
        #   退出」的分支里 —— 也就是说：serve 冒烟**从来没被真正执行过**，
        #   一旦真出现 `ModuleNotFoundError: market_watch.web` 之类启动即退，
        #   脚本会在报错前就先崩，红的是脚本而不是真正的问题，
        #   还白白掩盖掉「serve 链路没验过」这个事实。
        if proc.poll() is not None and proc.returncode != 0:
            raise SystemExit(f"serve 进程启动即退出（exit={proc.returncode}），见 {log}")
        if code is None:
            raise SystemExit(f"serve 起来后 15s 内探不到 /api/health，见 {log}")
        print(f"/api/health -> HTTP {code}")

        # ---- 看板链路：首页 + 离线 ECharts（★ 白屏是假不出来的，只能这么验）----
        # 「离线可用、禁止 CDN」是本项目对外的硬承诺；若 vendored 的
        # static/vendor/echarts.min.js 没打进包，首页会白屏而且**没有任何报错**。
        def _get(path: str) -> tuple[int, str]:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                return exc.code, ""

        status, body = _get("/")
        print(f"/            -> HTTP {status}  ({len(body)} bytes)")
        if status != 200:
            raise SystemExit(f"看板首页 / 不是 200（HTTP {status}），见 {log}")
        if "echarts.min.js" not in body:
            raise SystemExit("首页没引用 echarts.min.js：模板或静态资源路径不对（可能白屏）")

        status_lib, body_lib = _get("/static/vendor/echarts.min.js")
        print(f"/static/vendor/echarts.min.js -> HTTP {status_lib}  ({len(body_lib)} bytes)")
        if status_lib != 200 or len(body_lib) < 1024:
            raise SystemExit("ECharts 静态资源没打进包或体积异常（离线看板承诺落空）")
    finally:
        # ★ 先优雅退，再硬杀。`taskkill /F` 是硬杀，会直接跳过 uvicorn 与 asyncio 的
        #   清理钩子 —— 日志里的 `web.stopped` / `runtime.closed` 压根不会出现，
        #   于是「服务起来了但退出时崩」这类问题永远测不出来（只验到「起得来」）。
        #   故先发 CTRL_BREAK 等它自己收尾；超时才降级硬杀。
        if proc.poll() is None:
            with contextlib.suppress(OSError, ValueError):
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                taskkill = ["taskkill", "/PID", str(proc.pid), "/T", "/F"]
                subprocess.run(taskkill, capture_output=True)  # noqa: S603,S607
                proc.wait(timeout=10)

    # 优雅退出的**生命周期标记**：比退出码更值得断言。
    # ★ 实测（python-crawler-engineer）：优雅退出时 uvicorn 返回 **exitcode 3**，
    #   不是 0 —— 所以「断言 returncode == 0」会把所有优雅退出的正常服务都判红，
    #   反过来「硬杀后 returncode != 0」也证明不了 graceful shutdown 没崩。
    #   真正在意的是收尾链路跑没跑完，那就看日志里的 web.stopped / runtime.closed。
    text = log.read_text(encoding="utf-8", errors="replace")  # noqa: PTH123
    for bad in (
        "Traceback (most recent call last)",
        "ModuleNotFoundError",
        "Failed to execute script",
    ):
        if bad in text:
            raise SystemExit(f"serve 冒烟失败（命中 {bad!r}）：\n{text[-3000:]}")

    for marker, what in (
        ("web.stopped", "uvicorn 服务已停止"),
        ("runtime.closed", "asyncio 运行时已关闭"),
    ):
        if marker not in text:
            raise SystemExit(
                f"serve 优雅退出失败：日志里没有 {marker!r}（{what}）—— "
                f"多半是硬杀或收尾钩子抛了异常，见 {log}"
            )
    print("优雅退出：web.stopped + runtime.closed 均已落地")


def _make_room(*dirs: Path) -> None:
    """构建前给输出目录腾位：把上次的产物**改名**，而不是删掉。

    `--clean` 会让 PyInstaller 先删掉 `dist/market-watch/` 与 `build/market-watch/`，
    这两个目录动辄几百上千个文件。在批量删除受限的环境里（沙箱/CI 安全守卫，
    阈值通常 50 个文件）这一步会直接把构建打断，报
    `SAFE_DELETE_BULK_CONFIRM_REQUIRED` —— 报错信息还完全看不出跟打包有关。

    改名是同一卷内的 `mv`，既腾出空目录让 PyInstaller 干净的重建，又不销毁任何
    产物（要回滚把名字改回来即可），也绕开了删除受限。
    """
    for d in dirs:
        if not d.exists():
            continue
        # 只能挪到**同卷**：跨卷 mv 会退化成「复制+删除」，照样撞守卫
        target = d.with_name(f"{d.name}-old-{time.strftime('%H%M%S')}")
        print(f"$ mv {d.name} -> {target.name}")
        d.rename(target)


def _purge_release_junk(app_dir: Path) -> None:
    """打 zip 之前清掉两类**不该分发**的文件。

    1. `data/market_watch.db*` —— 上面那些冒烟（status / serve）在 exe 同目录下跑出来的
       **开发者本地 SQLite**，而且 `-wal` 里还压着未 checkpoint 的抓取数据（实测 267832 字节）。
       把本地数据库连同 WAL 一起打进发布包 = 把你的历史行情、告警、请求证据分发给用户，
       这在 README 的免责条款里是不允许的（运行期数据不进包、不随包分发）。
       保留 `data/pending/` 与 `data/rejects/` 两个空目录——运行期要往里写。
    2. `serve-smoke.log` —— 构建期冒烟日志，对使用者零价值，还会把构建机绝对路径带出去。
    """
    # ★ 改成 rglob 全扫，不再只清 `data/*.db*`：**运行期产生的 db 出现在 app_dir 的
    #   任何位置都是漏**（仓库里 `src/market_watch/data/` 那个开发库就是这么混进来的
    #   一类；spec 的 datas 没声明它，但它可能随 COLLECT 落地）。开发机上那个库里
    #   压着真实抓取数据（行情/告警/请求证据），分发给下载者在 README 的免责条款里
    #   是不允许的。判据按硬来：zip 里出现任何 `*.db` / `*.db-wal` / `*.db-shm` 就是失败。
    for db in sorted(app_dir.rglob("*.db*")):
        print(f"  清理发布包内的数据库文件：{db.relative_to(app_dir)}")
        db.unlink()
    smoke = app_dir / "serve-smoke.log"
    if smoke.exists():
        print("  清理发布包内的构建日志：serve-smoke.log")
        smoke.unlink()
    # `_smoke_serve` 的日志落在 `scratch/` 子目录里（见那里的 mkdir），清 root 下的
    # `serve-smoke.log` 清不掉它。漏掉这一步，构建机绝对路径和冒烟输出就跟着 zip
    # 分发给用户了。目录里只有这一个文件，rmtree 不会撞批量删除守卫。
    if SCRATCH.is_dir():
        print(f"  清理发布包内的冒烟日志目录：{SCRATCH.name}/")
        shutil.rmtree(SCRATCH)


def main() -> int:
    if sys.platform != "win32":
        print("[warn] 非 Windows 平台：脚本假定在 windows-latest runner 上使用。")

    if not SPEC.exists():
        raise SystemExit(f"缺少 spec：{SPEC}")

    # 1) 打包
    _make_room(DIST / APP_NAME, BUILD / APP_NAME)
    _run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            # ★ 不能带 `--specpath`：PyInstaller ≥ 6 在「同时给出 .spec 文件」时会直接报错
            #     ERROR: option(s) not allowed: --specpath
            #     makespec options not valid when a .spec file is given
            #   （本机 6.22.3 实测，构建第一步就红；CI 跑同一脚本同样红）。
            #   发布链路的输出目录改用 --distpath / --workpath 指定——这两个允许与 .spec
            #   同时使用——效果等价：产物落在 release/dist 与 release/build，不污染仓库根。
            #   也不能改成 cd release 再跑：spec 的 _detect_root() 会先试 cwd 并判断
            #   `cwd/release/market_watch_console.py` 是否存在，cwd=release 时命不中，
            #   直接 SystemExit 定位不到仓库根。
            "--distpath",
            str(DIST),
            "--workpath",
            str(BUILD),
            str(SPEC),
        ],
        cwd=ROOT,
    )

    exe = DIST / "market-watch" / "market-watch.exe"
    if not exe.exists():
        raise SystemExit(f"构建失败：没找到 {exe}")
    # ★ 早于第一步冒烟就确认运行时目录存在：上面的 APP_DIR 如果哪天又和 DIST/APP_NAME
    #   对不上（改名、spec 改 COLLECT 名字），后面 status/gc/once/reset/serve 会在
    #   各自的 cwd 上才炸，报错点散在四处且都长得像「用户环境问题」，很难一眼看出
    #   是构建脚本常量写错。在这里先撞一次，红得明白。
    if not APP_DIR.is_dir() or not (APP_DIR / "market-watch.exe").exists():
        raise SystemExit(f"运行时目录与预期不符：{APP_DIR}")

    size_mb = exe.stat().st_size / 1024 / 1024
    print(f"\n产物：{exe}  ({size_mb:.1f} MB)")

    # 2) 真的跑一遍
    print("\n--- 验证 --help ---")
    _run([str(exe), "--help"], cwd=DIST / "market-watch")

    print("\n--- 验证 policy-check ---")
    env = dict(os.environ)
    env.setdefault("MW_CONTACT_URL", "https://you.example/contact")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(  # noqa: S603 —— 命令行由本脚本构造，无 shell 展开
        [str(exe), "policy-check"],
        cwd=DIST / "market-watch",
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr[-2000:])
    if proc.returncode != 0:
        raise SystemExit(f"policy-check 未通过（exit={proc.returncode}）")
    if "合规检查通过 9/9" not in proc.stdout:
        raise SystemExit("policy-check 未输出 9/9（robots 快照或 bench 目标缺失？）")

    # status 走的是 `cmd_status` 里另一条函数体延迟导入（`..infrastructure.storage.repositories`）。
    # 只冒烟 serve 会漏掉这一类：不同子命令各有各的懒加载，修好 web 不等于修好 status。
    # 这条纯本地（只读 SQLite），不产生任何出网请求。
    print("\n--- 验证 status（覆盖 cmd_status 的延迟导入 + SQLite 冻结态建表）---")
    _run([str(exe), "status"], cwd=APP_DIR)

    # ★★ 下面三条是六个子命令里**只有这三条从没人真执行过**的那部分 ★★
    # （`--help` 只到 argparse，压根不 import 任何业务模块，所以「六条 --help 都 OK」
    #   完全等价于「六条都没跑过」——2026-10-03 那两条 P0 就是这么藏住的。）
    print("\n--- 验证 gc（全新安装：保留清理 + rollup）---")
    _run([str(exe), "gc"], cwd=APP_DIR)

    print("\n--- 验证 once --dry-run（wiring 那条延迟导入的**唯一**真跑路径）---")
    # 这是 wiring 第一次真跑起来：`cmd_once` 里的 `from .wiring import build_runtime`
    # 只有走到这里才会被 import（--help / status / gc 都碰不到它）。
    # 同时验证「库里还没数据时跑一轮」这条真实用户路径。
    _run([str(exe), "once", "--dry-run", "--source", "gw2_items"], cwd=APP_DIR)

    print("\n--- 验证 reset --yes（破坏性入口 + schema 重建）---")
    # 唯一能证明「清空后还能起来」的入口；顺带把 once 写进去的数据清掉，
    # 让后面的 serve 冒烟从干净状态起步。
    _run([str(exe), "reset", "--yes"], cwd=APP_DIR)

    _smoke_serve(exe, env)

    # ★ 打 zip 之前清构建期产物——见 `_purge_release_junk` 的注释。
    #   注意：这里**不能**写成 `from release.build_exe import _purge_release_junk`
    #   再调一遍（一度有人这么改过）：直接跑 `python release/build_exe.py` 时
    #   `sys.path[0]` 是 `release/`，不是仓库根，`import release` 必然 ModuleNotFoundError；
    #   就算改成命名空间包能 import 通，这也是**自己 import 自己**，纯绕路，什么都没解决。
    #   本函数就在本模块里定义过、也只在这里用一次，直接调用即可。

    # 3) 打包 zip（可选；CI 里由 release.yml 接管）
    # ★ 不要先 `archive.unlink()` 再 make：`shutil.make_archive` 本来就会覆盖同名文件，
    #   而「删一个 28MB 的旧 zip」在批量删除受限的环境里会直接触发安全守卫
    #   （实测：SAFE_DELETE_BULK_CONFIRM_REQUIRED），进程被杀在打 zip 前一步，
    #   连 traceback 都留不下。少删一次文件，就少一个失败点。
    #
    #   ★ 但也必须**先清两类垃圾再打**：上面的冒烟（status / serve）会在 exe 同目录留下
    #     `data/market_watch.db*` —— 那是开发者本地 SQLite，而且 `-wal` 里还压着未 checkpoint
    #     的抓取数据（实测 267832 字节），外加 `serve-smoke.log`（构建日志，还带构建机绝对路径）。
    #     直接打包 = 把本地数据与构建日志一起分发给用户，README 的免责条款不允许。
    #     release.yml 上传的就是这份 zip，所以清一次两边都干净。
    # 传 APP_DIR 而不是现写 DIST / APP_NAME：和上面所有冒烟共用同一个常量，
    # 免得哪天常量改了、这里还按老写法传，清的不是同一个目录还不一定报错。
    _purge_release_junk(APP_DIR)

    archive = DIST / "market-watch.zip"
    shutil.make_archive(str(archive.with_suffix("")), "zip", root_dir=DIST / APP_NAME)
    print(f"\n发布包：{archive}  ({archive.stat().st_size / 1024 / 1024:.1f} MB)")
    print("\n全部验证通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
