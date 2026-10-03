"""`interfaces/cli.py` 延迟导入的层级回归测试（堵 P0 复发）。

背景 / 为什么是 `find_spec` 而不是数 `level`：

2026-10-03 出过一次 P0：`cli.py` 里 `cmd_serve` / `cmd_once` 两个函数体内部的延迟导入
写成了**两级相对**（`from ..web.app import create_app`、`from ..wiring import build_runtime`）。
`cli.py` 位于 `market_watch/interfaces/`，函数内 `.` 代表 `market_watch.interfaces`、
`..` 代表 `market_watch`，所以两级写法会被解析成 `market_watch.web.app` /
`market_watch.wiring`——**两个都不存在的模块**。后果是源码模式下 `market-watch once`
和 `market-watch serve` 直接 `ModuleNotFoundError`，而 `--help` / `--version` 因为
不进那条分支，看起来一切正常。

当时是靠人工跑一次性 ast 脚本发现的，`tests/` 里没有任何覆盖、CI 里也没有对应门禁。
发布链路对 exe 跑的那套冒烟能兜住，但 **CI 全绿的时候 cli 坏了照样能合并**。

为什么不直接断言「所有相对导入 level 必须 == 1」——那会误杀 12 条对的：
`cli.py` 模块作用域里的 `from ..domain.policy import ...`、`from ..infrastructure.http.robots
import ...` 等本来就是 level 2，而且**是对的**（从 `market_watch.interfaces` 退回
`market_watch` 再往下走）。

所以这里按 Python 自己的解析规则把每个相对导入还原成绝对模块名，再去问
`importlib.util.find_spec` 它是否存在。这个断言不关心写了几级点——写 `..` 只要
目标模块真的存在就放行，写 `.` 也一样；而 P0 那种「看着能解析、其实指向不存在的包」
会被精确抓住，不管将来谁把它改成几级点。

将来如果发现某条延迟导入的目标是**故意不存在**的（比如按需分发的可选后端），
不要给这条测试开口子，改成在 `_EXPECTED_DELAYED` 里显式登记并附理由。
"""

from __future__ import annotations

import ast
import importlib.util
import inspect

from market_watch.interfaces import cli


def _relative_imports() -> list[tuple[int, int, str]]:
    """返回 cli.py 里所有相对导入的 `(lineno, level, module)`。"""
    tree = ast.parse(inspect.getsource(cli))
    rows: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            rows.append((node.lineno, node.level, node.module or ""))
    return sorted(rows)


def _absolute_name(level: int, module: str) -> str:
    """按 Python 的解析规则把相对导入还原成绝对模块名。

    在 `market_watch.interfaces.cli` 里：level 1 = `market_watch.interfaces`、
    level 2 = `market_watch`、level 3 = 顶层包（本项目只有一层，不会到那儿）。
    """
    package = cli.__package__.split(".")  # ['market_watch', 'interfaces']
    base = package[: len(package) - (level - 1)]
    target = list(base)
    if module:
        target.extend(module.split("."))
    return ".".join(target)


def test_cli_has_relative_imports() -> None:
    """前置断言：cli.py 确实有相对导入，防止上面的扫描逻辑空转变成永远绿。"""
    rows = _relative_imports()
    assert rows, "cli.py 里找不到任何相对导入，_relative_imports() 的访问方式已经失效"
    # level 2 是模块作用域主力的正常形态（见模块 docstring），不是 lint 债。
    assert any(level == 2 for _, level, _ in rows)
    assert any(level == 1 for _, level, _ in rows)


def test_every_relative_import_resolves_to_existing_module() -> None:
    """核心回归：每个相对导入按 Python 规则还原后，目标模块必须真的存在。"""
    missing = [
        (lineno, level, module, _absolute_name(level, module))
        for lineno, level, module in _relative_imports()
        if importlib.util.find_spec(_absolute_name(level, module)) is None
    ]
    assert not missing, (
        "cli.py 存在指向不存在模块的相对导入（源码模式下 ImportError）；"
        f"出问题的是 {missing}。"
        "函数体内部的延迟导入要数着父包走：cli.py 在 market_watch/interfaces/ 下，"
        "函数里 `.` 是 market_watch.interfaces，`..` 是 market_watch。"
    )


def _function_scoped_relative_imports() -> dict[str, list[tuple[int, int, str]]]:
    """按函数收集相对导入，返回 `{函数名: [(lineno, level, module), ...]}`。"""
    tree = ast.parse(inspect.getsource(cli))
    collected: dict[str, list[tuple[int, int, str]]] = {}

    def walk(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_owner = owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_owner = f"{owner}.{child.name}" if owner else child.name
            if isinstance(child, ast.ImportFrom) and child.level:
                collected.setdefault(child_owner, []).append(
                    (child.lineno, child.level, child.module or "")
                )
            walk(child, child_owner)

    walk(tree, "")
    return collected


def test_the_two_known_p0_sites_are_still_lazy() -> None:
    """锁定 P0 那两处仍是**函数内**延迟导入，别被提到模块作用域变成无条件 import。

    模块作用域 import 会让 `--help` / `--version` 也把 web 运行时、storage 分层全部拉起来，
    源码模式下的启动时间和 import 面都会变差。这里只防「延迟」被去掉，不校验点级数
    （点级数由上面那条 `find_spec` 测试兜）。
    """
    scoped = _function_scoped_relative_imports()

    assert (
        "cmd_serve" in scoped
    ), f"cmd_serve 内不再有相对导入，P0 现场变了；当前函数内相对导入在：{sorted(scoped)}"
    assert (
        "cmd_once" in scoped
    ), f"cmd_once 内不再有相对导入，P0 现场变了；当前函数内相对导入在：{sorted(scoped)}"
    # cmd_once 里还嵌了一层 `_run`，那里面的导入同样得是延迟的。
    assert any(name.startswith("cmd_once.") for name in scoped), "cmd_once._run 不在延迟导入之列"
