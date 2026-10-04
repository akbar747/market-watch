# 贡献指南

## 0. 环境

需要 Python ≥ 3.13 与 [uv](https://docs.astral.sh/uv/)。所有元数据、依赖、lint 配置都集中在 `pyproject.toml`，没有 `setup.py`、没有 `requirements.txt`，不用两份维护。

```bash
uv sync --extra dev          # 装项目本体（可编辑）+ 开发/测试/静态检查全套
uv run pre-commit install    # 挂提交期闸门
```

## 1. 跑门禁

```bash
uv run ruff check $(git ls-files '*.py')   # lint
uv run black --check $(git ls-files '*.py')# 格式化（本仓库只认 black）
uv run mypy                                # 类型
uv run market-watch policy-check           # 合规自检，必须 9/9
uv run pre-commit run --all-files          # 提交期闸门全量
uv run pytest --cov=market_watch --cov-report=term-missing --cov-fail-under=35
```

`$(git ls-files '*.py')` 不是可选项：`release/build*/`、`release/dist*/` 里解压着 PyInstaller 生成的上千个 `.py`，全量跑 lint 会红一片，而红的全是构建垃圾。同理，**不要**对 `release/` 目录整体跑 lint/format。

## 2. 跑单文件脚本：请带 `PYTHONPATH=src`

本仓是 src-layout —— 真源码在 `src/market_watch/`，它没被装进那个解释器里。因此：

- **`uv run python <脚本>` 能直接跑**：`uv sync` 会把项目本体以可编辑模式装进该虚拟环境。
- **直接用系统解释器 / 另一个 venv 敲 `python <脚本>` 会 `ModuleNotFoundError: No module named 'market_watch'`**，必须显式补路径：

```bash
PYTHONPATH=src python examples/fetch_one_item.py
PYTHONPATH=src python examples/alert_webhook.py
PYTHONPATH=src python examples/smoke_policy_check.py
PYTHONPATH=src python scripts/check_layers.py
PYTHONPATH=src python scripts/_record_fixtures.py
PYTHONPATH=src python bench/bench_fetch.py
```

需要这么写的入口（截至 2026-10-03）：`examples/fetch_one_item.py`、`examples/alert_webhook.py`、`examples/smoke_policy_check.py`、`scripts/check_layers.py`、`scripts/_record_fixtures.py`、`bench/bench_fetch.py`；以及 `PYTHONPATH=src python mw_ast_check.py` 这类一次性自查脚本（不进仓库，跑相对导入校验时必须带它，否则父包都 import 不进去，会凭空多出一堆假报错）。

**`pytest` 不用管**：`pyproject.toml` 的 `[tool.pytest.ini_options]` 已经写了 `pythonpath = ["src"]`，裸跑 `uv run pytest` 收得到全部用例（实测 264 个、0 collection error）。

业务入口不要手敲模块路径，用控制台脚本：

```bash
uv run market-watch --help
uv run market-watch policy-check
uv run market-watch once --dry-run --source gw2_items
```

（`[project.scripts]` 目前只有 `market-watch = "market_watch.interfaces.cli:main"`，没有 `python -m market_watch.policy` 这种用法。）

## 3. 格式化只认 black

`line-length = 100`，见 `pyproject.toml` 的 `[tool.black]`。ruff 只跑 lint（含 `I` 导入排序与自动修复），**不参与格式化，也不存在 ruff-format 门禁**——同一批文件 black 判 0 红、ruff-format 仍判若干要改，两个门禁同时挂只会互相拉扯。别把 `ruff format --check` 加回 CI 或 pre-commit。

版本口径是钉死的：`pyproject.toml` 的 dev extras 与 `.pre-commit-config.yaml` 的 rev 必须对齐（当前 `ruff>=0.16,<0.17` 对 v0.16.10、`black>=26.5,<27` 对 26.5.1、`mypy>=1.15,<1.16` 对 v1.15.0），否则本地 pre-commit 绿、CI 红这类事会常态化。

**改完文件请顺手自检那一个文件**（本机 Windows 上尤其重要）：

```bash
black --check src/market_watch/application/alert_service.py
```

期望输出是 `1 file unchanged`；不是这句话就先别提交。Windows 编辑器按 CRLF 存盘时，`[tool.ruff]` 的 `line-ending` 只认 LF，black 会把整个文件判成要改，diff 是 `-1,294 +1,294` 这种整篇重写，还会把 lint 结果一起搅浑。逐文件自检成本极低，比事后统一 `git add --renormalize .` 收一遍便宜。全仓 EOL 归一由那一条命令一次提交，不要求每个人手改。

## 4. 测试与覆盖率

- 当前 `pytest` 实测 **264 passed / 0 failed**（2026-10-04）。
- 覆盖率实测 **50%**（`TOTAL 3992 1900 924 61`），CI 闸门 `--cov-fail-under=35`。**闸门就是 35，不设更高档位，也不是发布阻塞项**；缺口集中在编排层（`orchestrator` / `fanout` / `dedupe` / `scheduler` / `config` / `rates`），采集与落库主路径已覆盖。要补就从编排层补，补完再自行上调闸门，本文件不规定上调路线。
- **不要为了让 CI 变绿而删测试、加 `--deselect`、加 `-p no:cacheprovider`。**

### 本机跑 pytest 会遇到的两个环境坑（不是测试失败）

1. **汇总行被吞**：收尾清理 tmp 时，环境的批量删除守卫会 `SystemExit`，把 `N passed in Xs` 那行冲掉、退出码变 1。看起来像测试挂了，实际是清理阶段。**加 `CODEBUDDY_SAFE_DELETE_ENABLED=0`**，或给 `--basetemp` 指到项目外。
2. **`OSError: [WinError 448] 不受信任的装入点`**：`pytest` 的 `tmp_path` 会在 basetemp 下建 `...current` 符号链接，Windows 策略不允许 `resolve()` 它，`pytest_sessionfinish` 的 `cleanup_dead_symlinks` 因此在**所有用例跑完之后**抛异常，同样吞掉输出。

两者都发生在「测试已经跑完」之后。取数别靠数点阵行数（`addopts` 里已有 `-q`，再叠一个会把汇总行压掉，且点阵按终端宽度折行），用：

```bash
PYTHONPATH=src CODEBUDDY_SAFE_DELETE_ENABLED=0 uv run pytest --collect-only --no-header | tail -1
```

覆盖率同理——`--cov-report=term` 的输出可能一起被吞，改成静默跑完再从数据文件读：

```bash
PYTHONPATH=src CODEBUDDY_SAFE_DELETE_ENABLED=0 uv run pytest --cov --cov-report= -q
uv run coverage report | tail -1
```

**要通过/失败数，别去数点阵**（点阵只是进度进度条，不稳定也不权威）。`--junitxml` 在 `pytest_sessionfinish` **之前**写盘，正是这个坑发生时仍然可靠的唯一出口：

```bash
PYTHONPATH=src CODEBUDDY_SAFE_DELETE_ENABLED=0 uv run pytest --junitxml=/tmp/junit.xml -q >/dev/null 2>&1
uv run python -c "import xml.etree.ElementTree as ET;s=ET.parse('/tmp/junit.xml').getroot();s=s if s.tag=='testsuite' else s.find('testsuite');print({k:s.attrib[k] for k in ('tests','failures','errors','skipped')})"
# 实测输出：{'tests': '264', 'failures': '0', 'errors': '0', 'skipped': '0'}
```

所以本机的退出码 / 汇总行**不可作为门禁判据**；`--junitxml` 的 `failures`+`errors` 才是。CI 在 Linux 上跑不受这两个坑影响，正常看汇总行即可。

## 5. 合规（这是本项目的硬约束，不是建议）

- 采集只走 `GET` / `HEAD` 白名单，`robots.txt` 快照在 `docs/_robots_snapshot.json` 里被强制比对。
- `MW_CONTACT_URL`：本仓库**有意保留文档占位域** `https://you.example/contact`——CI 自己就用这个值跑 `policy-check`（9/9 通过），README 里有对应说明。但**你自己部署前必须换成真实可联系地址**：这个字段是留给被采集方（数据源方）联系你的渠道，占位符发出去等于没留。
- 出网代理、Token、口令一律走环境变量，禁止进代码、进 CI 明文、进日志。密钥扫描由 gitleaks 在 CI 里拦（见 `.github/workflows/ci.yml`）。
- 运行期数据（SQLite、WAL、`data/rejects/`、`data/pending/`）不进发布包、不随包分发；发布包内只保留运行期可创建的空目录。

## 6. 别做这几件事

- 别动 `src/market_watch/interfaces/web/{app,routes,deps}.py` 里的 `from ...application` 三点导入——它们在 `market_watch.interfaces.web` 包里，三点是对的，改成两点会变成 `No module named 'market_watch.web'`。
- 别给打包链路加 `--specpath`：PyInstaller ≥ 6 在同时给出 `.spec` 时会直接报 `makespec options not valid when a .spec file is given`。
- 别在 CI 里写 `|| true` 把红步骤抹平；也别为了绕过失败去弱化闸门。
- 别格式归一时只跑一半目录（`src` 归了 `tests` 没归），一次原子过，否则两次格式规则会互相覆盖。
