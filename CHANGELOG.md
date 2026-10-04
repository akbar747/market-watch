# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

暂无。

## [0.1.0] — 2026-10-04

首个可用版本：只读对接 Guild Wars 2 官方公开端点，本地 SQLite 落库，离线看板 + Windows 发布包。

### Added — 数据来源

- **只读对接 GW2 官方 v2 API**（免鉴权公开端点）与**官方公告 RSS**：`gw2_prices` / `gw2_listings` / `gw2_items` / `gw2_news_rss` 四个数据源，全部在 `policy-check` 的数据源注册表里登记为 `official_public`（非官方来源一律不得登记）。

### Added — 合规内建（不是开关，是代码常量）

- **零客户端交互依赖**：不读游戏内存、不做窗口句柄、不做图像识别；打包时 `cv2` / `pyautogui` / `pymem` / `frida` 等被显式 `excludes`，产物里不会出现。
- **不使用代理池**：`trust_env=False`，环境变量里的代理被忽略，请求路径可被审计。
- **只允许 `GET` / `HEAD` 且不带 body**：出网方法白名单在 `domain/policy.py` 里写死。
- **`robots.txt` 强制校验、无开关**：快照落在 `docs/_robots_snapshot.json`，`policy-check` 逐条比对。
- **速率硬钳制 8.0 rps**：由官方 600/min × 安全系数 0.80 得出，是 `domain/policy.py` 的**代码常量**，**故意不进 `.env`**——调不上去，也不需要调。
- **压测仅 loopback**：`bench/bench_fetch.py` 的 `assert_loopback` 保证压测永远打在本机，且不存在 `--force` 逃生开关。
- **不采集任何个人信息**：看板页脚即为对外声明原文。

### Added — 存储

- **SQLite WAL + 13 张 `STRICT` 表**，迁移脚本 `infrastructure/storage/migrations/0001_init.sql` 随包分发。
- **分层归档与保留策略**：`gc` 做 rollup 与到期清理，运行期缓冲目录 `data/pending/`、`data/rejects/` 与 exe 同目录。

### Added — 看板

- **FastAPI + Jinja2 + vendored ECharts**：`static/vendor/echarts.min.js` 随包分发，**离线可用、禁止 CDN**，断网也不白屏。
- 支持按 `item_id` 查询价格曲线，物品搜索下拉联动；页面底部为「最新告警」「官方公告」与合规声明。

### Added — 并发内核

- **httpx + asyncio**：三层并发——全局在飞 16 / 每主机并发 4 / 每主机令牌桶 8 rps（burst 12）。
- **AIMD 自适应**与**熔断**：`closed` / `open` / `half_open` 三态 + 半开探针，配合 **18 类错误矩阵**分类重试与降级。
- **分页 `Queue(maxsize=8)` + 4 worker**，背压可控。

### Added — 发布链路

- **Windows onedir 发布包**（`market-watch.zip`，约 27 MB）：exe + `_internal/` + 同目录 `config/` `docs/` `bench/` `data/`，解压即用，无需安装 Python。
- `python release/build_exe.py` **自验证**：`--help` / `policy-check` / `status` / `once` / `gc` / `reset` / `serve` 冒烟逐条跑，任一非 0 即失败；冒烟端口运行时向内核申请，不写死。
- **发布包卫生**：打 zip 前清理运行期数据库与构建日志，发布包内不含任何 `*.db` / `*.db-wal` / `*.db-shm` / `*.log`。

### Changed / Fixed

- `cli.py` 的函数体延迟导入级数修正（`from .wiring` / `from .web.app` 为一级、`from ..domain` 为两级），修前 `once` / `serve` 一启动就 `ModuleNotFoundError`；回归测试 `tests/unit/test_cli_relative_imports.py` 按「还原绝对模块名 + `find_spec`」判定。
- `gc` / `status` 在空库上不再抛 `OperationalError` / `no such table`：零字节占位 + 只读打开 + `_ready` 探测。
- GBK 控制台打印 `⚠️` 崩溃：运行时钩子 `SetConsoleOutputCP(65001)` + 入口 `sys.stdout.reconfigure(encoding="utf-8")` 双重兜底。
- 格式化器只保留 black（`line-length=100`），移除 `ruff format` 门禁；`ruff` / `black` / `mypy` 的版本上限与 `.pre-commit-config.yaml` 的 rev 对齐。
- `gw2_prices` / `gw2_listings` 里各有一份的 `_as_int` **抽成公共 helper** `infrastructure/adapters/_coerce.py` 并对齐语义：原先两侧对 `bool` 的处理分叉（`True` → `1` vs `None`），现统一为 **`bool` → `None`**——`True` 被静默强转成单价 `1` 会让脏数据以合法身份混入均价与告警基线，代价远大于丢掉一个观测点。`tests/unit/test_gw2_int_coercion.py` 有一条判函数对象同一性的用例锁住「不会再漂回两份」。
- 上一条的**后续项也一并收口**：`_coerce.as_int_field` 把「键不存在 / 值为 `null`」与「键存在但不可转」分开，脏值（如 `unit_price: true`）现在会留下一条 `schema.field_not_integer`（`warn`），不再被静默吸收。反向约束同样钉住：无挂单侧的 `{}` 与显式 `null` **不算脏**，否则真实数据里成千上万个无买盘物品会刷出一屏告警把真问题淹掉——已在真实录制的 fixture 上验证新规则命中数为 **0**。

### 质量门（打 tag 时的实测值）

| 项 | 实测 |
|---|---|
| `mypy` | `Success: no issues found in 59 source files` |
| `ruff check` | `All checks passed` |
| `black --check` | `85 files unchanged` |
| `pytest` | `269 passed / 0 failed`（269 collected） |
| 覆盖率 | `50%`（CI 闸门 `--cov-fail-under=35`） |
| CI | 13 个 step，含 source / exe 双份 `policy-check`、domain purity AST 静态检查、gitleaks 密钥扫描 |

[Unreleased]: https://github.com/market-watch/market-watch/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/market-watch/market-watch/releases/tag/v0.1.0
