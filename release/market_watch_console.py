# release/market_watch_console.py
"""冻结（PyInstaller）入口：**唯一**负责在打包态下重定位『应用根目录』。

为什么需要它
------------
`application/config.py` 用 `Path(__file__).resolve().parents[3]` 推导仓库根（``_REPO_ROOT``），
进而在源码模式下把 DB 放到 ``<repo>/src/market_watch/data/``、把 ``config/`` 当配置目录、
把 ``docs/_robots_snapshot.json`` 当 robots 证据。`interfaces/cli.py::_repo_root`` 同理。

冻结后 `__file__` 会指向打包目录，这个深度推导会指到完全无关的目录：

- onefile：``<_MEIPASS>/market_watch/interfaces/cli.py`` → parents[3] = 系统临时目录
- onedir：``<dist>/_internal/market_watch/interfaces/cli.py`` → parents[3] = exe 所在目录

本脚本复刻同一套推导（``sys._MEIPASS.parent``），并把打包体里的 ``config/`` 与
``docs/_robots_snapshot.json`` 播种到 exe 所在目录，然后用 ``MW_*`` 环境变量把
DB / 配置目录指过去，使冻结态与源码态行为一致。

约束：
- 本文件**不修改任何 src 代码**；它只在 `market_watch` 被导入之前设环境变量。
- 只在 frozen 时生效；源码模式直接透传给 CLI，行为零变化。
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import sys

#: 从打包体播种到应用根目录的条目（缺失才复制；用户改过的不会被覆盖）
_SEED_ITEMS: tuple[tuple[str, str], ...] = (
    ("config", "config"),
    # 只取这一个文件（dest 是 `docs` 目录，解压后为 docs/_robots_snapshot.json）
    ("docs/_robots_snapshot.json", "docs/_robots_snapshot.json"),
    # C7 证据：policy-check 第 9 项读 <root>/bench/bench_fetch.py
    ("bench/bench_fetch.py", "bench/bench_fetch.py"),
)


def _meipass() -> Path:
    return Path(getattr(sys, "_MEIPASS", ".")).resolve()


def _seed(dest: Path, src_root: Path) -> None:
    """把 config/ 与 robots 快照复制到 dest（只补缺失项），并建好 data 子目录。

    onedir 布局下 `Analysis.datas` 会被直接收进 exe 所在目录（不等 _MEIPASS），
    onefile 布局下才会解压进 _MEIPASS；两处的源都要找一遍。
    """
    for rel_src, rel_dst in _SEED_ITEMS:
        s = src_root / rel_src
        if not s.exists():
            s = dest / rel_src
        if not s.exists():
            continue
        d = dest / rel_dst
        if d.exists() and d.resolve() == s.resolve():
            continue  # 源与目标同路径（onedir 下数据本就在 exe 目录里），无需复制
        if s.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            for child in s.iterdir():
                target = d / child.name
                if target.exists():
                    continue
                if child.is_dir():
                    shutil.copytree(child, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(child, target)
        elif not d.exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)

    (dest / "data" / "rejects").mkdir(parents=True, exist_ok=True)
    (dest / "data" / "pending").mkdir(parents=True, exist_ok=True)


def _resolve_app_root(exe_dir: Path, meipass: Path) -> Path:
    """优先 exe 所在目录（便携模式），不可写则回落到临时解压目录的父目录。"""
    candidates = [exe_dir, meipass.parent]
    for candidate in candidates:
        try:
            _seed(candidate, meipass)
            return candidate
        except OSError:
            continue
    return meipass.parent


def _apply_env(app_root: Path) -> None:
    """把运行期路径写进环境变量；用户已显式设置的 MW_* 不覆盖。"""
    data = app_root / "data"
    os.environ.setdefault("MW_DB_PATH", str(data / "market_watch.db"))
    os.environ.setdefault("MW_CONFIG_DIR", str(app_root / "config"))
    os.environ.setdefault("MW_REJECTS_DIR", str(data / "rejects"))
    os.environ.setdefault("MW_PENDING_DIR", str(data / "pending"))


def _force_utf8_console() -> None:
    """Windows 控制台默认 GBK（cp936），一旦程序往 stdout 打 `⚠️` / 中文就抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u26a0'`。

    `policy-check` 第 3 项恰好会打 `⚠️ 文档样例占位域，部署前必须替换`，
    所以不加这一句，exe 在默认 cmd.exe / PowerShell(GBK) 下会在第 3 行直接崩。
    冻结态重配 stdout/stderr 为 UTF-8；源码模式由调用方终端负责，不动。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):  # 流已关闭 / 非 TextIO 时跳过
            reconfigure(encoding="utf-8", errors="replace")


def bootstrap() -> None:
    """在 `market_watch` 被导入之前调用；源码模式下是 no-op。"""
    if not getattr(sys, "frozen", False):
        return
    exe_dir = Path(sys.executable).resolve().parent
    _apply_env(_resolve_app_root(exe_dir, _meipass()))
    _force_utf8_console()


bootstrap()

from market_watch.interfaces.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
