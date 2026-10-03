# scripts/check_layers.py
"""依赖方向铁律的静态检查（架构 `docs/01-architecture.md` §3 铁律 1/2/3/4 的可执行版本）。

挂点：`.pre-commit-config.yaml` 的 `local` repo + CI（`pytest` 之外的第二道闸）。

**只做 AST 分析，不 import 任何业务代码**——因此 domain/ 里的模块即使存在运行时依赖
缺失，本脚本也能在 `uv sync` 之前跑通；反之，若把 `import httpx` 偷偷写进 domain/
，ruff 抓不到（它只看语法）、pytest 要等到 import 期才炸，而本脚本在提交时就会拦下。

被检查的四条铁律：

    L1  domain/         一行都不许 `import httpx / sqlite3 / requests / fastapi / pandas / ...`
    L2  infrastructure/ 可 import domain，但**禁止 import application**
        （例外：`application.config` 允许被 infra 构造函数作为参数接收，架构 §3 铁律 2）
    L3  domain/ 与 application/ 的签名里禁止出现 `httpx.Response` / `sqlite3.Row`，
        统一走 `domain.models.ResponseView`
    L4  interfaces/     只许被更高层 import；application/ 与 infrastructure/ 不得 import interfaces

用法::

    python scripts/check_layers.py                  # 人读报告，违规 exit 1
    python scripts/check_layers.py --json           # CI 消费结构化输出
    python scripts/check_layers.py --show-allowed   # 打印白名单，便于 PR 审查对照

Exit codes: 0 = 通过；1 = 有违规；2 = 仓库结构异常（找不到 src 目录）。
"""

from __future__ import annotations

import argparse
import ast
from ast import Attribute, Import, ImportFrom, Name, NodeVisitor, parse
from collections.abc import Iterable
import json
from pathlib import Path
import sys
from typing import Final

# ===========================================================================
# L1：domain/ 禁止出现的顶层模块（架构 §3 铁律 1）
# ===========================================================================
FORBIDDEN_IN_DOMAIN: Final[frozenset[str]] = frozenset(
    {
        "httpx",  # 出网客户端
        "sqlite3",  # 落库驱动
        "requests",  # 第二 HTTP 客户端
        "fastapi",  # Web 框架
        "pandas",  # 数据分析
        "sqlalchemy",  # ORM
        "asyncpg",  # 异步驱动
        "urllib.request",  # 绕过 fetcher 的直连出口
    }
)

# ===========================================================================
# L2：infrastructure/ 禁止 import 的应用层模块
# ===========================================================================
FORBIDDEN_IN_INFRA: Final[frozenset[str]] = frozenset(
    {
        "market_watch.application.config",
        "market_watch.application.alert_service",
        "market_watch.application.dedupe",
        "market_watch.application.fanout",
        "market_watch.application.news_ingest",
        "market_watch.application.orchestrator",
        "market_watch.application.price_ingest",
        "market_watch.application.rates",
        "market_watch.application.scheduler",
    }
)

#: L2 白名单：架构 §3 铁律 2 的显式例外（infra 构造函数接收 Settings 作为参数）。
INFRA_ALLOWED_APPLICATION: Final[frozenset[str]] = frozenset({"market_watch.application.config"})

# ===========================================================================
# L3：禁止出现在跨层注解里的第三方类型
# ===========================================================================
FORBIDDEN_ANNOTATION_MODULES: Final[frozenset[str]] = frozenset(
    {"httpx", "sqlite3", "sqlalchemy", "urllib3"}
)
FORBIDDEN_ANNOTATION_ATTRS: Final[frozenset[str]] = frozenset(
    {"Response", "Row", "Client", "AsyncClient", "Cursor"}
)

# ===========================================================================
# L4：application/ 与 infrastructure/ 禁止 import 的层
# ===========================================================================
FORBIDDEN_IMPORT_INTERFACES: Final[frozenset[str]] = frozenset({"market_watch.interfaces"})

#: 目录 → 层序遍历顺序（用于错误信息里的层级说明）
LAYERS: Final[tuple[str, ...]] = ("domain", "application", "infrastructure", "interfaces")


# ===========================================================================
# AST 工具
# ===========================================================================
class ImportCollector(NodeVisitor):
    """把文件里所有 `import` / `from ... import` 解析成**绝对**模块名。

    相对导入（`from ..domain.errors import X`）按 PEP 328 语义结合当前包解析
    （见 `resolve_relative`），`from . import httpx` 这种把第三方当相对包写的
    写法同样会被解析成顶层名，不会被绕过。
    """

    def __init__(self, *, pkg_parts: tuple[str, ...]) -> None:  # noqa: PLR0913 - 仅供本脚本内部使用
        self._pkg_parts = pkg_parts
        self.modules: set[str] = set()

    def visit_Import(self, node: Import) -> None:  # noqa: N802 - NodeVisitor 约定的方法名
        for alias in node.names:
            self.modules.add(alias.name)

    def visit_ImportFrom(self, node: ImportFrom) -> None:  # noqa: N802
        if not node.module:
            return
        if node.level == 0:
            self.modules.add(node.module)
            return
        resolved = resolve_relative(pkg_parts=self._pkg_parts, level=node.level, module=node.module)
        if resolved is not None:
            self.modules.add(resolved)


def _iter_nodes(node: ast.AST) -> Iterable[ast.AST]:
    """深度优先遍历任意 AST 子树的所有节点。"""
    yield from ast.walk(node)


class AnnotationLeakVisitor(NodeVisitor):
    """找出 `httpx.Response` / `sqlite3.Row` 这类跨层注解（架构 §3 铁律 4）。"""

    def __init__(self) -> None:
        self.leaks: list[str] = []

    def _scan(self, annotation: ast.AST | None, where: str) -> None:
        if annotation is None:
            return
        for child in _iter_nodes(annotation):
            if (
                isinstance(child, Attribute)
                and isinstance(child.value, Name)
                and child.value.id in FORBIDDEN_ANNOTATION_MODULES
                and child.attr in FORBIDDEN_ANNOTATION_ATTRS
            ):
                self.leaks.append(f"{where}注解引用了跨层类型 {child.value.id}.{child.attr}")

    def visit_arg(self, node: object) -> None:  # noqa: N802
        self._scan(getattr(node, "annotation", None), "入参 ")

    def visit_FunctionDef(self, node: object) -> None:  # noqa: N802
        self._scan(getattr(node, "returns", None), "返回类型 ")

    def visit_AnnAssign(self, node: object) -> None:  # noqa: N802
        self._scan(getattr(node, "annotation", None), "变量注解 ")


def resolve_relative(
    *,
    pkg_parts: tuple[str, ...],
    level: int,
    module: str | None,
) -> str | None:
    """把 `from ...domain.errors import X` 解析成绝对模块名。

    `pkg_parts` 是当前文件所属**包**（不含文件名）的 dotted 分段，
    例如 `market_watch/domain/ports.py` → `("market_watch", "domain")`。

    level=1 表示当前包，level=n 往上退 n-1 层（PEP 328 语义）。
    """
    if level <= 0:
        return module
    depth = len(pkg_parts) - (level - 1)
    if depth < 0:
        return None
    base = list(pkg_parts[:depth])
    if not base:
        return None
    if module:
        base.extend(module.split("."))
    return ".".join(base)


# ===========================================================================
# 检查主体
# ===========================================================================
def check_file(path: Path, src_root: Path) -> list[str]:
    """对单个文件跑四条铁律，返回违规描述列表（空列表 = 通过）。"""
    rel = path.relative_to(src_root.parent).as_posix()
    parts = path.relative_to(src_root).with_suffix("").parts
    layer = parts[0] if parts and parts[0] in LAYERS else None
    if layer is None:
        return []

    pkg_parts = parts[:-1]
    source = path.read_text(encoding="utf-8")
    tree = parse(source)
    violations: list[str] = []

    # ---- L1 / L2 / L4：导入目标 ----
    collector = ImportCollector(pkg_parts=pkg_parts)
    collector.visit(tree)
    for target in sorted(collector.modules):
        root = target.split(".")[0]
        if layer == "domain" and root in FORBIDDEN_IN_DOMAIN:
            violations.append(f"{rel}: [L1] domain/ 禁止 import {root}（依赖方向铁律 §3）")
            continue
        if layer == "infrastructure" and _is_forbidden_infra(target):
            msg = "[L2] infrastructure/ 禁止 import application（依赖方向铁律 §3）"
            violations.append(f"{target}: {msg}")
            continue
        if layer in {"application", "infrastructure"} and any(
            target == bad or target.startswith(bad + ".") for bad in FORBIDDEN_IMPORT_INTERFACES
        ):
            violations.append(f"{target}: [L4] {layer}/ 禁止 import interfaces（依赖方向铁律 §3）")

    # ---- L3：注解泄漏（只查 domain/ 与 application/；infra 本来就该碰 sqlite3/httpx） ----
    if layer in {"domain", "application"}:
        leak = AnnotationLeakVisitor()
        leak.visit(tree)
        violations.extend(f"{rel}: [L3] {msg.strip()}" for msg in leak.leaks)
    return violations


def _is_forbidden_infra(target: str) -> bool:
    if target in INFRA_ALLOWED_APPLICATION:
        return False
    return any(target == bad or target.startswith(bad + ".") for bad in FORBIDDEN_IN_INFRA)


def check_repo(src_root: Path) -> list[str]:
    """跑全仓检查，返回违规列表。"""
    if not src_root.is_dir():
        raise SystemExit(f"[check_layers] 找不到 {src_root}（须从仓库根运行）")
    violations: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        violations.extend(check_file(path.resolve(), src_root.resolve()))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="依赖方向铁律静态检查（架构 §3）")
    parser.add_argument(
        "--src-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "src" / "market_watch",
        help="源码根（market_watch 包目录），默认从脚本位置推导",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON 供 CI 消费")
    parser.add_argument("--show-allowed", action="store_true", help="打印白名单后退出")
    args = parser.parse_args(argv)

    if args.show_allowed:
        print(
            json.dumps(
                {
                    "FORBIDDEN_IN_DOMAIN": sorted(FORBIDDEN_IN_DOMAIN),
                    "FORBIDDEN_IN_INFRA": sorted(FORBIDDEN_IN_INFRA),
                    "INFRA_ALLOWED_APPLICATION": sorted(INFRA_ALLOWED_APPLICATION),
                    "FORBIDDEN_IMPORT_INTERFACES": sorted(FORBIDDEN_IMPORT_INTERFACES),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    violations = check_repo(args.src_root.resolve())
    if args.as_json:
        payload = {"ok": not violations, "violations": violations}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not violations else 1

    if not violations:
        ok_message = (
            "[OK] 依赖方向铁律 4 条全部通过："
            "L1 domain/ 纯净 / L2 infra 不反向依赖 application / "
            "L3 无第三方类型跨层 / L4 interfaces 单向"
        )
        print(ok_message)
        return 0

    print(f"[FAIL] 依赖方向铁律发现 {len(violations)} 处违规：", file=sys.stderr)
    for item in violations:
        print(f"  - {item}", file=sys.stderr)
    print(
        "修复提示：需要 infra 能力时在 domain/ports.py 加 Protocol，"
        "由 application 通过构造函数注入实现（架构 §3 铁律 2）。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
