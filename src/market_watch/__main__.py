# src/market_watch/__main__.py
"""`python -m market_watch` 入口，同时作为 PyInstaller 的打包入口。

★ 为什么这里不能只写 `from .interfaces.cli import main`：

  同一份 `__main__.py` 有两种运行方式，**父包状态不一样**：

  1. `python -m market_watch` → 作为 `market_watch.__main__` 被导入，
     `__package__ == "market_watch"`，相对导入合法。
  2. PyInstaller 打包 `src/market_watch/__main__.py` → 被当成**顶层脚本**执行，
     `__package__` 是空串，相对导入直接抛
     `ImportError: attempted relative import with no known parent package`，
     exe 一启动就死（2026-10-03 实测复现）。

  因此这里按 `__package__` 分支：frozen/顶层脚本路径下把仓库 `src` 补进
  `sys.path`，再用绝对导入拿 `main`。两种入口都能启动，且 `pyproject.toml`
  的 `market-watch = "market_watch.interfaces.cli:main"` 与
  `[project.scripts]` 生成的控制台脚本都走 cli:main，不受影响。
"""

from __future__ import annotations

from pathlib import Path
import sys

if __package__ in (None, ""):
    # 顶层脚本 / frozen：没有父包，手动把 <repo>/src 挂到 sys.path
    # resolve() 而非 abspath()：frozen 场景下 __file__ 可能带符号链接或非规范路径，
    # resolve() 才是「真实目录」，parent.parent 才稳定指向 <repo>/src。
    _src = str(Path(__file__).resolve().parent.parent)  # <repo>/src
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from market_watch.interfaces.cli import main
else:
    from .interfaces.cli import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
