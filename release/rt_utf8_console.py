# release/rt_utf8_console.py
"""PyInstaller **runtime hook**：把 Windows 控制台代码页切到 UTF-8（65001）。

为什么需要
----------
默认 cmd.exe / PowerShell(中文环境) 的代码页是 cp936(GBK)。Python 把 stdout 的
encoding 绑到该代码页，`policy-check` 第 3 项恰好会打印 `⚠️`（U+26A0），
于是 `print()` 直接抛：

    UnicodeEncodeError: 'gbk' codec can't encode character '\\u26a0'

在冻结态尤其致命（CI 不给你改终端）。运行时钩子在解释器启动早期执行，
`SetConsoleOutputCP(65001)` 让 Python 默认就用 UTF-8；入口脚本
`release/market_watch_console.py` 里还有一道 `sys.stdout.reconfigure()` 兜底。
源码模式下此文件不参与，无需关心。
"""

from __future__ import annotations

import sys

if getattr(sys, "frozen", False) and sys.platform == "win32":
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # noqa: S110 —— Windows 专用运行时钩子
        kernel32.SetConsoleOutputCP(65001)  # 输出代码页 -> UTF-8
        kernel32.SetConsoleCP(65001)  # 输入代码页 -> UTF-8
    except Exception:  # noqa: S110,S112
        pass
