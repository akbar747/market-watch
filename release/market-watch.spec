# release/market-watch.spec
# PyInstaller 打包规格（Windows x64 / 控制台子系统）。
#
# 产物：onedir 布局的 `dist/market-watch/market-watch.exe`
#       —— 一个 exe + `_internal/` 运行时目录 + 同目录下的 `config/`、`docs/`、`data/`。
#       发布时把整个 `dist/market-watch/` 目录打 zip 即可（比 onefile 启动快、
#       且 DB/配置与 exe 同目录，符合便携工具的使用习惯）。
#
# 为什么必须 collect-all / 显式 add-data：
#   - fastapi / uvicorn / jinja2 把模板、静态资源、路由表放在包目录里，默认只收 .pyc，
#     不 `--collect-all` 会在 `market-watch serve` 时 404/FileNotFound；
#   - ECharts 是 **vendored 进 `interfaces/web/static/vendor/echarts.min.js`** 的（离线可用、
#     禁止 CDN），不显式 add-data 看板图表直接白屏；
#   - `infrastructure/storage/migrations/0001_init.sql` 是建表 DDL，同样需要显式收进来；
#   - `docs/_robots_snapshot.json` 是 C2 合规证据，`policy-check` 第 4 项会读它，缺了就 8/9。
#
# 打包前置条件（否则 exe 能启动但命令全挂）：
#   `src/market_watch/interfaces/cli.py` 的**相对导入级数不是统一的两层**，而是
#   取决于目标模块相对 `market_watch.interfaces`（cli.py 所在包）的位置：
#     - `from ..domain import ...`        —— 两层：回退到 `market_watch` 再往下走，对；
#     - `from .web.app import create_app` —— 一层：目标就在 `market_watch.interfaces` 里，对；
#     - `from .wiring import build_runtime`—— 同上，一层，对。
#   ★ 唯一判据只有一条：Python 按级数还原出的**绝对模块名必须真实存在**。
#     写成 `from ..web.app import ...` 会还原成并不存在的 `market_watch.web.app`，
#     运行时 `ModuleNotFoundError: No module named 'market_watch.web'`，
#     exe 表现为「policy-check 全绿、serve 一启动就崩」——这就是那个 P0 的原样。
#     **别把上面一层点的两条「改成两层」，那是把已修的 bug 原样装回去。**
#   回归测试见 `tests/unit/test_cli_relative_imports.py`（它按「还原绝对名再
#   find_spec」判定，不按 `level` 断言，所以不会误杀模块作用域那些正常的 level-2 导入）；
#   贡献者侧说明见 `CONTRIBUTING.md`。

from contextlib import suppress
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


# 推导仓库根。踩过的坑（不要再踩）：
#   1. SPECPATH 在 pyinstaller 6 里记的是「spec 所在目录」，所以 `Path(SPECPATH).parents[0]`
#      才是仓库根；写成 parents[1] 会指到仓库的父目录，Analysis 入口脚本会报
#      "script <父目录>/release/market_watch_console.py not found"。
#   2. SPECPATH 在个别版本里记的是当前工作目录，且 spec 内没有 `__file__`，
#      两者都不保险 —— 所以逐个候选校验「release/market_watch_console.py 是否存在」。
def _detect_root() -> Path:
    candidates = [Path.cwd().resolve()]
    with suppress(NameError):  # SPECPATH 只在 PyInstaller 注入时存在；不在就直接按 cwd 找
        candidates.append(Path(SPECPATH).resolve())  # noqa: F821
    for candidate in candidates:
        if (candidate / "release" / "market_watch_console.py").exists():
            return candidate
    raise SystemExit("market-watch.spec：无法定位仓库根（没找到 release/market_watch_console.py）")


ROOT = _detect_root()
SRC = ROOT / "src"


# ---- 延迟导入兜底（★ 漏了这条会「policy-check 全绿但 serve 一启动就崩」）----
# `cli.py` 刻意把绝大多数 import 写在**函数体内**（懒加载：启动更快、源码模式无
# 导入副作用）。PyInstaller 的静态分析只能顺着模块顶层 import 扫，函数体内的
# import 它看不见，于是不打包 —— 结果就是：
#     exe --help        OK      （只用到顶层 import）
#     exe policy-check  9/9     （robots/bench 走的是仓库文件，不依赖 web）
#     exe serve         ModuleNotFoundError: No module named 'market_watch.web'
# 2026-10-03 那次 `serve` 崩溃的直接原因其实是另一回事（cli.py 里 `from ..web.app`
# 多写了一级点，运行时去解析并不存在的 `market_watch.web`，详见 cli.cmd_serve 注释），
# 但**要不是有这条冒烟，那个错到今天还躺着**。这条兜底仍值得留：它把
# `src/market_watch` 下**每一个** .py 模块登记成 hidden import，以后新增子模块
# 不用回来改 spec —— 手写清单迟早会漏，漏了就是「本地能跑、出去不能跑」。
def _lazily_imported_modules() -> list[str]:
    out: list[str] = []
    for path in (SRC / "market_watch").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        dotted = path.relative_to(SRC).with_suffix("").as_posix().replace("/", ".")
        out.append(dotted)
    return sorted(out)


block_cipher = None

# ---- collect-all：FastAPI/uvicorn/jinja2/structlog 会把包内数据（模板、路由、模板过滤器、
# ---- logging 配置、可选依赖）藏在包目录里；不收全会在 serve 时找不到资源。
_COLLECT_PACKAGES = ("fastapi", "starlette", "uvicorn", "jinja2", "structlog", "feedparser")
_collected_datas: list[tuple[str, str]] = []
_collected_binaries: list[tuple[str, str]] = []
_collected_hidden: list[str] = []
for _pkg in _COLLECT_PACKAGES:
    # collect_all 的返回结构随 pyinstaller 版本变动（新版为 (datas, binaries, hiddenimports)），
    # 这里按下标取并做长度兜底，避免换版本就崩。
    _result = collect_all(_pkg)
    _collected_datas.extend(_result[0] if len(_result) > 0 else [])
    _collected_binaries.extend(_result[1] if len(_result) > 1 else [])
    _collected_hidden.extend(_result[2] if len(_result) > 2 else [])

a = Analysis(  # noqa: F821  (PyInstaller 注入的全局量)
    [str(ROOT / "release" / "market_watch_console.py")],
    pathex=[str(SRC)],
    binaries=_collected_binaries,
    datas=[
        *_collected_datas,
        # 看板：vendored 静态资源 + Jinja2 模板
        (
            str(SRC / "market_watch" / "interfaces" / "web" / "static"),
            "market_watch/interfaces/web/static",
        ),
        (
            str(SRC / "market_watch" / "interfaces" / "web" / "templates"),
            "market_watch/interfaces/web/templates",
        ),
        # 建表 DDL
        (
            str(SRC / "market_watch" / "infrastructure" / "storage" / "migrations"),
            "market_watch/infrastructure/storage/migrations",
        ),
        # 数据源与告警规则；冻结入口会播种到 exe 目录
        (str(ROOT / "config"), "config"),
        # C2 合规证据：robots 快照（policy-check 第 4 项读它）
        # 注意 dest 只能是目录 `docs`：写成 `docs/_robots_snapshot.json` 会被解压成
        # `docs/_robots_snapshot.json/docs/_robots_snapshot.json`（嵌套目录），policy-check 读不到。
        (str(ROOT / "docs" / "_robots_snapshot.json"), "docs"),
        # C7 合规证据：`policy-check` 第 9 项要 bench/bench_fetch.py，冻结态也要在。
        # 这里按**单文件**收（整目录收进 COLLECT 时没落地到 _internal/bench/），
        # dest 写 `bench` 等价于 _internal/bench/bench_fetch.py。
        (str(ROOT / "bench" / "bench_fetch.py"), "bench"),
    ],
    hiddenimports=list(
        dict.fromkeys(
            [
                # 第三方：这些在函数体内 import（如 cmd_serve 的 `import uvicorn`）
                "feedparser",
                "feedparser.sgmllib",
                "jinja2",
                "structlog",
                "uvicorn",
                *_collected_hidden,
                # 自家包：枚举全模块，覆盖 cli.py 里所有函数体延迟导入
                # （market_watch.web / .wiring / .infrastructure.* 等）
                *_lazily_imported_modules(),
            ]
        )
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[
        # 控制台代码页 -> UTF-8，否则 GBK 下 print('⚠️ …') 直接 UnicodeEncodeError
        str(ROOT / "release" / "rt_utf8_console.py"),
    ],
    excludes=[
        # 游戏客户端自动化/内存读写/图像识别的黑名单依赖（C6）：
        # 明确排除，让打包产物里绝不会出现这些模块。
        "cv2",
        "skimage",
        "pyautogui",
        "pydirectinput",
        "pymem",
        "frida",
        "proxy_pool",
        "rotating_proxies",
        "PyQt5",
        "PySide6",
        "matplotlib",
        # 测试/开发期依赖不需要进包
        "pytest",
        "respx",
        "black",
        "mypy",
        "ruff",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="market-watch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="market-watch",
)
