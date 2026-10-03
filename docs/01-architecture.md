# market-watch 架构设计文档

| 项 | 值 |
|---|---|
| 文档编号 | `docs/01-architecture.md` |
| 版本 | v1.0 |
| 作者 | 梁高展（senior-python-architect） |
| 目标读者 | Phase 2 采集实现（`python-crawler-engineer`）、Phase 3a/3b |
| 目标 Python | **3.13**（`requires-python = ">=3.13"`，Windows x64 首发环境） |
| 依赖管理 | `uv`（备选 pdm）；依赖版本一律以 `>=` 下界声明，精确锁定交给 `uv.lock` |

> 全文版本号均为「常用稳定版，以官方发布为准」；所有性能数字除非标注「实测」，一律是**预算值（目标）**，标注「需实测确认」，禁止当作 benchmark 引用。
> 本仓库 Phase 2 落盘时，文档中每个代码块首行的文件路径即为**真实落盘路径**。

---

## 0. 摘要：三条结论 + 一条必须知道的坏消息

**结论一句话**：**httpx + asyncio 自建采集内核** + **SQLite（WAL + 分层归档）** + **FastAPI + 本地内置静态资源单页看板**，够了；不需要 Scrapy、不需要 Postgres、不需要 Node 构建链。

三句话说清选型：

1. **为什么不用 Scrapy**：我们需要的是「对一个免鉴权官方 REST 端点的**分页扇出 + 每主机令牌桶 + 分类重试 + 熔断 + 连接池重建**」的**精细可控**链路，而 Scrapy 的 Twisted 引擎把这一层封进了 Downloader 中间件，改造成「按错误分类决定退避曲线、按主机动态调整令牌生成速率」要逆向它的 `RETRY_TIMES / AUTOTHROTTLE` 交互。httpx 是除 Python 标准库之外唯一的 IO 基建，业务逻辑 100% 可见、100% 可单测。我们不抓 HTML 页面、**没有 DOM 解析需求**，Item/Pipeline 那一套收益为零。代价是：重试、限速、去重、幂等落库都要自己写——这四块本文全部给出接口与参数。
2. **为什么不用 Postgres**：这是**单机本地看板**，一个用户、一个进程、一个文件。SQLite 在 WAL + `synchronous=NORMAL` + 单写线程下能吞下我们的写入量（预算见 §10），零运维、零 Docker、`pdm/pyinstaller` 打包 exe 时零额外依赖；而 Postgres 的收益（多写者并发、行级锁、网络访问）在我们的场景里一个都用不上。真到需要做重分析的那天，用 DuckDB 直接读 SQLite（duckdb sqlite_scanner）或导出 Parquet，比迁 Postgres 便宜一个数量级。
3. **看板用什么**：**FastAPI + Jinja2 单模板 + 少量 JSON API + 内置（vendored）ECharts UMD**。不引 Node、不用 CDN（离线可用、exe 打包可用）、不做 SSR 框架之争。图表数据量用服务端降采样（`downsample ≤ 2000 点`）控制，浏览器端只负责画。

**坏消息（已成文的合规判定，直接影响 Phase 2 的输入）**：
主理人给定的参考源 **EVE Online ESI 不能用**。实测证据：

```
$ curl -s https://esi.evetech.net/robots.txt
User-agent: *
Disallow: /
```

`Disallow: /` 对所有 UA 禁止全部路径。我们自己在 §11 写死了「必须遵守 robots.txt」，若第一天就破例，这份文档就没有任何约束力。因此**默认数据源替换为 guildwars2.com 官方 v2 API**（§1.2 给出完整实测证据：无 robots.txt 指令 → RFC 9309 视为允许、免鉴权、官方宣布速率限制）。ESI 相关事实被保留在 §11.3 的「否决源登记表」中，禁止被静默加回。

---

## 1. 数据源基线与实测证据

### 1.1 合规四要素（引用 `references/crawler-playbook.md`，缺一不得开工）

| 要素 | 结论 |
|---|---|
| 目标站点 | `api.guildwars2.com`（ArenaNet 官方 REST v2）、`www.guildwars2.com`（官方新闻站） |
| 采集内容 | 交易行挂单价格/数量、物品元数据、官方新闻公告 |
| 是否公开可访问 | 是。**免鉴权**（无需 API Key / OAuth），实测 200 OK，无 `WWW-Authenticate` |
| robots.txt 状态 | `api.guildwars2.com/robots.txt` → **HTTP 404**（无指令 → RFC 9309 视为无限制）；`www.guildwars2.com/robots.txt` → **HTTP 404**（同）。**均未声明任何 Disallow** |
| 限速参数 | 官方响应头实测 `X-Rate-Limit-Limit: 600`（按官方 Wiki 为每 IP 每分钟 600 请求，需以官方文档最终核实）。本方案自限 **8 rps（≈480/min，即官方额度的 80%）**，详见 §5 |
| 授权来源 | **官方公开 API**（四选一的「官方 API」项）。只读 GET，不写任何服务端 |

### 1.2 实测记录（2026-10-03，全部由本机 curl 实测）

```
# 1) 连接 channel：无需鉴权
GET https://api.guildwars2.com/v2/commerce/prices/24          -> 200, 165 B
GET https://api.guildwars2.com/v2/commerce/listings/24        -> 200, 77.8 KB
GET https://api.guildwars2.com/v2/items?ids=24,68             -> 200, 1.4 KB
GET https://api.guildwars2.com/v2/commerce/prices?page=0&page_size=10 -> 200

# 2) 分页语义（v2/...prices?page=1&page_size=200）
X-Page-Total: 140        # 当次页数
X-Page-Size: 200         # 单页条数（最大 200，实测接受）
X-Result-Total: 27997    # 全量条数（随行情变动 -> 分页漂移必须处理，见 §6）
X-Result-Count: 200

# 3) 限流语义
X-Rate-Limit-Limit: 600           # 无 Remaining 头 -> 必须自限，不能依赖服务端回传

# 4) 缓存语义
Cache-Control: public, max-age=120   # -> 同一资源的轮询周期不得短于 120s，这是硬性礼貌约束
Access-Control-Expose-Headers: Link,X-Page-Total,X-Page-Size,X-Result-Total,X-Result-Count

# 5) 新闻 RSS
GET https://www.guildwars2.com/en/feed/ -> 200, text/xml; charset=utf-8, 78.8 KB, RSS 2.0
```

### 1.3 数据结构（实测响应，作为解析契约）

```jsonc
// GET /v2/commerce/prices/24
{ "id": 24, "whitelisted": false,
  "buys":  { "quantity": 108641, "unit_price": 128 },
  "sells": { "quantity": 282300, "unit_price": 198 } }

// GET /v2/commerce/listings/24   （原始挂单，按单位价格聚合成档）
{ "id": 24,
  "buys":  [ { "listings": 1, "unit_price": 128, "quantity": 56 }, ... ],   // 买盘降序
  "sells": [ { "listings": 1, "unit_price": 198, "quantity": 3 },  ... ] }  // 卖盘升序

// GET /v2/items?ids=24
[ { "name": "Sealed Package of Snowballs", "description": "...", "type": "Consumable",
    "level": 0, "rarity": "Basic", "vendor_value": 0, "game_types": ["Pve"], "flags": [...] } ]
```

> `buys.unit_price` 是**最高买价（best bid）**，`sells.unit_price` 是**最低卖价（best ask）**。这是端点的官方语义，Phase 2 解析时不要反推。

### 1.4 否决源登记表（禁止静默加回）

| 源 | 实测结果 | 判定 |
|---|---|---|
| `esi.evetech.net` (EVE ESI) | `robots.txt` = `User-agent: * / Disallow: /`；`X-Pages: 272`（The Forge 卖单，10 万级）；`X-Ratelimit-Limit: 12000/15m` | **否决**。命中我们自己的 robots 红线。若要启用，必须先取得运营方**书面授权**并在 `docs/04-compliance.md` 登记，且 `RobotsGuard` 显式对该主机放行（代码变更 + PR 评审，不允许环境变量开关） |
| 任何需登录/付费墙/验证码的端点 | — | **否决**（红线，见 §11.1） |
| 任何非官方镜像 / 第三方「黑盒聚合」API | — | **否决**。数据可追溯性不可证明，且存在转售风险 |

---

## 2. 目录结构（src-layout）

```text
market-watch/
├── pyproject.toml                                 # 唯一元数据源：依赖/构建/工具链配置，禁止 setup.py 与 requirements.txt 双份
├── README.md                                      # Phase 3a 负责
├── LICENSE                                        # Phase 3a 负责（建议 MIT）
├── .env.example                                   # 必须提交，列出全部环境变量且不含真实密钥
├── .gitignore                                     # __pycache__/ .env/ dist/ data/ !(data/.gitkeep) 等
├── .pre-commit-config.yaml                        # ruff + black + mypy + trailing-whitespace 钩子
├── config/
│   ├── sources.yaml                               # 数据源清单：source_id → 适配器/端点/礼貌参数/启用开关
│   └── alerts.yaml                                # 异动规则：阈值/窗口/冷却/严重级/订阅
├── src/
│   └── market_watch/
│       ├── __init__.py                            # 只放 __version__ 与显式再导出，禁止放逻辑
│       ├── __main__.py                            # python -m market_watch 入口
│       ├── py.typed                               # PEP 561 标记，空文件但必须有
│       ├── logging_setup.py                       # structlog 结构化 JSON 初始化 + 敏感字段脱敏处理器
│       ├── domain/                                # 纯业务层：禁止 import httpx / sqlite3 / 任何 IO
│       │   ├── __init__.py
│       │   ├── models.py                          # 全部 dataclass/TypedDict：快照、聚合、公告、告警、游标
│       │   ├── ports.py                           # Protocol：SourceAdapter/RateLimiter/Breaker/SnapshotSink/AlertSink
│       │   ├── errors.py                          # 错误分类枚举 + ExceptionMap（可逆的错误码 ↔ 异常映射）
│       │   ├── policy.py                          # 合规纯逻辑：UA 模板校验/额度钳制/只读判定/黑名单import判定
│       │   ├── pricing.py                         # best bid/ask、spread、mid、VWAP、盘口水深、OHLC 桶聚合
│       │   ├── consistency.py                     # 取代多尺度匹配：多源冗余校验 + 数据一致性校验规则
│       │   ├── anomaly.py                         # 异动判定：环比/z-score/绝对阈值 + 冷却去重
│       │   └── retention.py                       # 保留窗口计算（表 → cutoff 时间戳），纯函数便于单测
│       ├── application/                           # 用例编排层：只依赖 domain，靠 ports 拿 IO
│       │   ├── __init__.py
│       │   ├── config.py                          # pydantic-settings Settings；含官方额度硬钳制 validator
│       │   ├── rates.py                           # 从 config + sources.yaml 编译出每主机的 Politeness 预算
│       │   ├── scheduler.py                       # 周期调度：价格周期/挂单周期/公告周期/cycle_id 分配
│       │   ├── orchestrator.py                    # 一轮采集的完整编排：fanout→fetch→parse→validate→sink→detect
│       │   ├── fanout.py                          # 分页扇出与「下一批任务」推进，含漂移检测
│       │   ├── price_ingest.py                    # 价格用例：prices 全量快照 + watchlist 原始挂单
│       │   ├── news_ingest.py                     # 公告用例：RSS 拉取 + 条件请求 + 增量去重
│       │   ├── dedupe.py                          # 进程内变更门（change gate）+ content_hash 计算
│       │   └── alert_service.py                   # 异动 → AlertEvent → 多 sink 分发 + 冷却
│       ├── infrastructure/
│       │   ├── __init__.py
│       │   ├── http/
│       │   │   ├── __init__.py
│       │   │   ├── client_pool.py                 # 每 host 单 AsyncClient；健康检查后整池重建（断线重连）
│       │   │   ├── fetcher.py                     # 全仓唯一允许出网的地方；强制 GET/HEAD、强制经限流器
│       │   │   ├── rate_limiter.py                # 每 host 令牌桶 + AIMD 自适应（429/420 减半、平稳期恢复）
│       │   │   ├── breaker.py                     # 每 host 熔断状态机 closed/open/half-open + 探针
│       │   │   ├── retry.py                       # 决策纯函数 + 执行器；错误矩阵的唯一实现点
│       │   │   ├── classification.py              # Response/Exception → ErrorClass（供 retry/breaker/metrics 复用）
│       │   │   ├── robots.py                      # robots.txt 抓取/缓存/判定/强制；默认 enforce 无开关
│       │   │   ├── lifecycle.py                   # 优雅退出：signal/事件、TaskGroup 取消、队列 drain
│       │   │   └── metrics.py                     # 计数器/直方图（内存 + 落 fetch_log），给看板健康页
│       │   ├── adapters/
│       │   │   ├── __init__.py
│       │   │   ├── registry.py                    # adapter 注册表 + 契约自检入口
│       │   │   ├── gw2_prices.py                  # /v2/commerce/prices 分页全量行情快照适配器
│       │   │   ├── gw2_listings.py                # /v2/commerce/listings 观测清单原始挂单适配器
│       │   │   ├── gw2_items.py                   # /v2/items 物品元数据（名称/类型）补齐适配器
│       │   │   ├── rss_generic.py                 # 通用 RSS/Atom 适配器，供官方公告源复用
│       │   │   └── fixtures/                      # 每个适配器一份录制样本，契约测试的输入
│       │   │       ├── gw2_prices_page1.json
│       │   │       ├── gw2_listings_item24.json
│       │   │       └── gw2_news_feed.xml
│       │   ├── storage/
│       │   │   ├── __init__.py
│       │   │   ├── sqlite.py                      # 连接工厂 + PRAGMA + 迁移执行 + 只读克隆
│       │   │   ├── migrations/
│       │   │   │   ├── __init__.py
│       │   │   │   └── 0001_init.sql              # 全量 DDL，见 §8
│       │   │   ├── writer.py                      # 单写线程 + asyncio.Queue 批量 upsert（消解 SQLite 单写者瓶颈）
│       │   │   ├── repositories.py                # 读侧：看板用的聚合查询（降采样/分页/搜索）
│       │   │   └── maintenance.py                 # 保留清理、rollup、PRAGMA optimize/incremental_vacuum
│       │   └── alerting/
│       │       ├── __init__.py
│       │       ├── sqlite_sink.py                 # 落 alert_event，供看板展示与去重
│       │       └── stdout_sink.py                 # structlog 输出，本地运行时的即时反馈
│       ├── interfaces/
│       │   ├── __init__.py
│       │   ├── cli.py                             # argparse CLI：once/serve/status/gc/policy-check/reset
│       │   └── web/
│       │       ├── __init__.py
│       │       ├── app.py                         # FastAPI 工厂 + lifespan（启停共用一个 Orchestrator）
│       │       ├── routes.py                      # 页面 + JSON API
│       │       ├── deps.py                        # 依赖 wiring：把 Settings/只读连接/Orchestrator 注入路由
│       │       ├── static/
│       │       │   ├── app.css
│       │       │   ├── app.js
│       │       │   └── vendor/echarts.min.js      # vendored，禁止 CDN
│       │       └── templates/
│       │           ├── base.html
│       │           └── dashboard.html
│       └── data/                                  # 运行期数据目录（gitignore，仅保留 .gitkeep）
│           └── .gitkeep
├── tests/
│   ├── conftest.py                                # fixtures 分层：session 级 mock transport / function 级临时 DB
│   ├── fixtures/http/                             # 录制样本（与 adapters/fixtures 同源，供集成测试）
│   ├── unit/
│   │   ├── test_pricing.py
│   │   ├── test_consistency.py
│   │   ├── test_anomaly.py
│   │   ├── test_policy.py                         # 额度钳制/UA/只读/黑名单 的断言
│   │   ├── test_rate_limiter.py
│   │   ├── test_breaker.py
│   │   ├── test_retry.py
│   │   └── test_retention.py
│   ├── adapters/
│   │   ├── fixtures_routing.py                    # 把 JSON/XML 样本映射成 httpx.MockTransport 路由
│   │   └── test_adapter_contract.py               # 参数化跑全部注册适配器（契约测试，见 §9.2）
│   └── integration/
│       ├── test_pipeline_once.py                  # 用 mock transport 端到端跑一轮并断言落库
│       └── test_web_api.py                        # TestClient 打 JSON API
├── bench/
│   ├── mock_server.py                             # 本地被测服务端（生成合成分页数据 + 可注入故障）
│   ├── bench_fetch.py                             # 吞吐/延迟压测入口，硬校验 base_url 必须是 loopback
│   └── profile_cli.py                             # cProfile 包裹 once 模式
├── scripts/
│   └── dev.ps1                                    # Windows PowerShell 一键：sync/lint/type/test
└── docs/
    ├── 01-architecture.md                         # 本文档
    └── 04-compliance.md                           # 合规记录：源登记表、授权记录、robots 快照存档（Phase 2 补齐）
```

---

## 3. 模块职责与依赖方向

| 层 | 模块 | 职责（一句话） | 允许依赖 |
|---|---|---|---|
| domain | `models` | 定义跨层传输的全部数据结构 | 仅 stdlib + pydantic |
| domain | `ports` | 定义全部外部能力的 Protocol（端口） | stdlib |
| domain | `errors` | 错误分类枚举与「HTTP/异常 → ErrorClass」映射 | stdlib |
| domain | `policy` | 合规判定纯逻辑：UA 校验、额度钳制算术、只读判定、禁用库名单 | stdlib |
| domain | `pricing` | 盘口计算与 OHLC/VWAP 聚合 | domain.models |
| domain | `consistency` | **取代多尺度匹配**的多源冗余 + 一致性校验规则 | domain.models |
| domain | `anomaly` | 异动判定与冷却去重 | domain.models |
| domain | `retention` | 按表产出保留 cutoff | stdlib |
| application | `config` | 读 `.env` 生成强类型配置，**官方额度在此被硬钳制** | pydantic-settings, domain.policy |
| application | `rates` | 把 config + sources.yaml 编译成每主机的 Politeness | domain.ports, application.config |
| application | `scheduler` | 决定本轮该跑哪些源与周期，分配 `cycle_id` | domain.models |
| application | `orchestrator` | 一轮编排的总导演：扇出→取数→校验→落库→告警 | domain.ports, infrastructure.*（只认 Protocol） |
| application | `fanout` | 推进分页游标、检测 `X-Page-Total` 漂移 | domain.models, domain.ports |
| application | `price_ingest` / `news_ingest` | 价格/公告两条用例的具体编排 | domain.*, infrastructure.* |
| application | `dedupe` | 进程内变更门与内容哈希 | domain.models |
| application | `alert_service` | 把异动编排成 AlertEvent 并分发 | domain.ports |
| infrastructure | `http/rate_limiter` | 令牌桶 + AIMD；唯一能放缓出站速度的地方 | anyio/asyncio |
| infrastructure | `http/breaker` | 熔断状态机，按主机隔离故障 | asyncio, domain.errors |
| infrastructure | `http/retry` | 「是否重试 + 等多久」的唯一决策点 | domain.errors |
| infrastructure | `http/client_pool` | 每 host 的 AsyncClient 生命周期与故障重建 | httpx |
| infrastructure | `http/fetcher` | 唯一出网出口：限流→robots→熔断→请求→分类 | 上述全部 |
| infrastructure | `http/robots` | robots.txt 抓取、24h 缓存、强制判定 | urllib.robotparser, httpx |
| infrastructure | `http/lifecycle` | 优雅退出、TaskGroup、队列 drain | asyncio, signal |
| infrastructure | `adapters/*` | 把具体端点翻译成 `FetchTask` 与 `ParseResult` | httpx（仅用于类型）, domain.* |
| infrastructure | `storage/sqlite` | 连接/PRAGMA/迁移 | sqlite3 |
| infrastructure | `storage/writer` | 单写线程 + 队列批量 upsert | sqlite3, asyncio |
| infrastructure | `storage/repositories` | 看板读侧查询 | sqlite3 |
| infrastructure | `alerting/*` | AlertSink 的两个实现 | sqlite3/logging |
| interfaces | `cli` | 命令行入口 | application.* |
| interfaces | `web/*` | FastAPI 看板 | fastapi, application.*, storage.repositories |

**依赖方向铁律（PR 审查清单）**

```
interfaces  →  application  →  domain  ←  infrastructure
                    ↑                        ↑
                    └── orchestrator 通过 domain.ports 反向注入 infrastructure 实现
```

1. `domain/` **一行都不许** `import httpx / sqlite3 / requests / fastapi / pandas`。用 `import-linter` 或 CI 里一条 `grep -R "import httpx" src/market_watch/domain && exit 1` 强制（Phase 3a 落 CI）。
2. `infrastructure` 可以 import `domain`（实现 ports），**禁止 import `application`**（防循环：配-rate/orchestrator 反过来注入 infra）。例外：`application.config` 允许被 infra 构造函数接收（作为参数传入，而非 import 类）。
3. `interfaces` 可以 import `application` 与 `infrastructure`（wiring 的地方），禁止被任何下层 import。
4. 跨层传数据一律走 `domain.models` 的 dataclass，**禁止**把 `httpx.Response` 或 `sqlite3.Row` 传进 domain/application 函数体。为此定义 `ResponseView`（§7.1）作为响应快照。

---

## 4. 并发模型设计

### 4.1 选型判定

| 负载类型 | 判定 | 本项目的落点 |
|---|---|---|
| IO 密集、库原生支持异步 | `asyncio` | **全部网络请求**：httpx.AsyncClient |
| IO 密集、库是阻塞式 | `ThreadPoolExecutor` | **SQLite 写入**：`sqlite3` 是阻塞 C 扩展，用**单个**写线程 |
| CPU 密集 | `ProcessPoolExecutor` | 本方案**不用**。单轮 140 页 JSON 解析是纯 Python 反序列化，预算在百毫秒级；真超预算先换 `orjson` 而不是开进程 |

### 4.2 三层并发控制（层层收紧，缺一不可）

```
第 1 层  全局在飞任务闸    asyncio.Semaphore(GLOBAL_INFLIGHT = 16)
            └─ 限制 TaskGroup 同时存在的任务数，防止 listings 500 个 item 一次性建 500 个 Task 打爆内存

第 2 层  每主机并发闸      asyncio.Semaphore(host_concurrency = 4)      [per host, 惰性创建]
            └─ 礼貌核心：同一时刻对同一主机最多 4 条连接

第 3 层  每主机令牌桶      TokenBucket(rate = 8.0 rps, capacity = 12)   [per host, 惰性创建]
            └─ 速率核心：把 5 分钟一次的批量扇出抹平成均匀的 8 req/s，而不是瞬时脉冲
```

**为什么三层而不是一层**：`Semaphore` 只限制「同时在飞」，不限制「单位时间内的次数」——Lua 式突发会在 `max-age` 窗口内打出一串脉冲，对面 CDN（`Server: Quaggans`）会认为你在刷。令牌桶反之不限「在飞」，慢响应会让在飞堆积。二者叠加才是可控的速率 + 可控的资源占用。全局闸则是为了防止「任务对象」本身（每个 task 要装一份 JSON）把 RSS 吃穿。

令牌桶实现要点（不用依赖库，约 40 行）：单调时钟 `time.monotonic()`、每次 `acquire(cost=1.0)` 先按 `elapsed * rate` 补令牌（`min(capacity, ...)`），不足则 `await asyncio.sleep(deficit / rate)`；**必须支持取消**——`asyncio.CancelledError` 时把已扣的令牌退还 (`self._tokens += cost`)，否则优雅退出后重启的进程会看到被吃空的桶。

**AIMD 自适应**（写在 `rate_limiter` 内部，`penalize/reward` 两个方法）：

```
收到 429 / 420 / 5xx(连续)     → rate *= 0.5，下限 floor = 1.0 rps      # 乘性减
连续 HEALTHY_WINDOW = 300s 无错误 → rate += 10% * rate，上限 = 配置初始值  # 加性增
初始 rate 永远不大于 config 值，绝不自发超过官方限额 × HOST_SAFETY_FACTOR
```

### 4.3 连接池、DNS、keep-alive

```python
# src/market_watch/infrastructure/http/client_pool.py
import asyncio, httpx

class ClientPool:
    """每 host 一个 AsyncClient；故障后整池重建，这是「断线重连」的落点。"""
    def __init__(self, transport_factory, timeout: httpx.Timeout, headers: dict[str, str]) -> None: ...
    async def get(self, host: str) -> httpx.AsyncClient: ...
    async def invalidate(self, host: str, reason: str) -> None: ...
    async def aclose_all(self) -> None: ...
```

| 参数 | 值 | 理由 |
|---|---|---|
| `limits.max_connections` | `20` | ≥ 全局在飞闸 16，让「拿连接」永不成为第二重瓶颈（第二重瓶颈应由令牌桶承担） |
| `limits.max_keepalive_connections` | `20` | keep-alive 复用抬到等于并发上限，避免 TLS/握手反复开销 |
| `limits.keepalive_expiry` | `20.0` 秒 | 官方 CDN 常见空闲回收是 30s 内，留 10s 余量，避免拿到已被对面关掉的半开连接 |
| `http2` | `False` | HTTP/2 需要 `httpx[http2]`（h2 依赖），且多路复用会让「连接数=并发数」被打破，令牌桶与它的安全感错配。HTTP/1.1 + keep-alive 足够达到 8 rps |
| `proxy` | `settings.proxy_url or None` **且 `trust_env=False`** | `trust_env=False` → 忽略 `HTTP(S)_PROXY` 环境变量：**架构上禁止**通过环境变量引入代理池。单一静态代理只允许在合规的企业出口场景显式配置 |
| `follow_redirects` | `False` | 禁止跟随跳转：防止一个官方源把请求引到第三方域（越权访问）。需要 30x 时由适配器显式声明 `allow_redirects=True` 且走 robots 复核 |
| `verify` | `True`（默认） | **禁止**全局关闭证书校验，出现 `SSL: CERTIFICATE_VERIFY_FAILED` 修环境（证书/系统时间），不关校验 |

DNS：httpx 通过 anyio 在线程池里做 `getaddrinfo`，本方案**不引入自建 DNS 缓存**——单主机、keep-alive 常连，解析量极小。若要压 `DNS_TIME`，用 `max_keepalive_connections` 与 `keepalive_expiry` 解决，不要引 `aiodns`。

### 4.4 分页任务调度策略（如何不打爆同一主机）

**原则：页数未知时绝不"预测性全部并发"。** 分页任务用**两阶段渐进扇出 + 单管道回填**：

```
阶段 A（探路）  取 page=0，读 X-Page-Total / X-Result-Total / Cache-Control:max-age
                → 得到 total_pages；若缺失则回退「取到空页为止」模式
阶段 B（有限并发推进）  维护一个 asyncio.Queue(maxsize = host_concurrency * 2)
                → 起 N=host_concurrency 个 worker 协程，每个 worker 循环：
                     page = q.get_nowait()  →  acquire(令牌) → GET → parse → 入队 siblings
```

这样做的好处：
- **在飞页数 ≤ host_concurrency(4)**，天然不会踩 ConnectionReset 与 `max-age` 之外的突发。
- **内存有界**：最多 4 份 ~200 条的记录在解析缓冲里，不会 140 页同时反序列化。
- **可观测**：队列长度直接是「剩余页数」指标，看板能显示进度条。
- **可取消**：取消 worker + `q.shutdown()` 即可优雅停在一页边界。

无论如何第二轮循环不得在 `max-age`(120s) 内启动；`scheduler` 用 `next_run_at = last_run_at + max(min_interval, max_age)` 计算，**这是硬约束**，`interval_seconds` 配得更小也只会被抬到 `max_age`（并在日志打印 WARNING:clamped）。

### 4.5 会话与连接池重建（用户点名的「断线重连」）

判定为「需要重建连接池」的信号（任一命中即 `invalidate(host)`）：

| 信号 | 说明 |
|---|---|
| `httpx.RemoteProtocolError: Server disconnected` | 典型半开连接 |
| `httpx.ConnectError` / `httpx.ConnectTimeout` 连续 ≥ 2 次 | TLS/握手层失效 |
| `httpx.ReadTimeout` 在同一 host 60s 窗口内 ≥ 5 次 | 对面过载/链路劣化 |
| `httpx.PoolTimeout` | 池子被占满——多半是连接泄漏，重建最快 |
| 池使用时长 > `POOL_MAX_LIFETIME = 1800s` 或请求数 > `POOL_MAX_REQUESTS = 5000` | 主动轮换，避免长寿命连接的隐性劣化 |

重建语义：`invalidate` 内先用 `asyncio.Lock` 串行化，执行 `await client.aclose()` → 丢弃引用 → 下次 `get()` 惰性新建；**在飞的请求不受影响**（它们持有旧 client 的引用，自然结束后被 GC）。重建期间该 host 的请求排队等待，而不是报错。`invalidate` 后自动 `rate *= 0.7`（比 429 轻柔的自适应减速），并记录 `event="pool.rebuilt"`。

### 4.6 优雅退出与任务取消

```python
# src/market_watch/infrastructure/http/lifecycle.py
async def run_until_stopped(start, *, shutdown_grace_s: float = 30.0) -> int: ...
class ShutdownSignal:
    requested: asyncio.Event
    reason: str | None
```

Windows 注意（Phase 3a 要打 exe，这条必须落地）：**Windows 没有可靠的 `SIGTERM`**，只有 `SIGINT`（Ctrl+C / `WM_CLOSE`）。因此：

1. `signal.signal(signal.SIGINT, handler)` 在 worker thread 里通过 `loop.call_soon_threadsafe` 置 `Event`（**禁止**在 signal handler 里 sync await）。
2. 退出序列固定为：`Event.set()` → 停止 `scheduler` 接受新周期 → `TaskGroup` 发送 cancel（不新起任务）→ `writer.drain(timeout=30s)` 把队列里已解析但未落库的数据写完 → `PRAGMA optimize` → `pool.aclose_all()` → `aconn.close()` → 退出码 0。
3. 若在 `shutdown_grace_s` 内 drain 不完，**记 ERROR 日志并保留未落库批次到 `data/pending/<cycle_id>.jsonl`**（重启后 `--resume` 重放），拒绝静默丢数据。
4. 双 Ctrl+C → 立刻 `asyncio.all_tasks()` 全部取消并退出码 130。
5. 提供 `--once` 模式（跑一轮就退），便于 Windows 任务计划程序托管，**而不是**自己写守护。

---

## 5. 限流参数表（可直接抄）

> 全部数值：**常用稳定起点，需实测确认**。调优顺序：先看 `X-Rate-Limit-Limit` 是否变化 → 看 429/5xx 比例 → 看 P99 延迟 → 最后才动并发。

### 5.1 出站 HTTP

| 项 | 默认值 | 代码位置 | 说明 |
|---|---|---|---|
| 官方速率上限 `OFFICIAL_LIMIT` | `600` req/min (=10 rps) | `config.py` 常量 | 实测 `X-Rate-Limit-Limit`。**不得从 `.env` 覆盖**，只允许代码里改（随官方文档变动） |
| 安全系数 `HOST_SAFETY_FACTOR` | `0.8` | `config.py` 常量 | 留出其他客户端/重试占用的余量 |
| 有效上限 | `8.0` rps | 推导 `10 * 0.8` | **所有 rps 配置的上界** |
| `per_host_rps` | `8.0` | `sources.yaml` → validator 钳制到 ≤ 8.0 | 单主机令牌生成速率 |
| `per_host_burst` | `12` | `sources.yaml` | 突发容量，≈1.5s 的量，够吸收排头的突发与网络抖动 |
| `per_host_concurrency` | `4` | `sources.yaml` | 单主机在飞任务/连接上限 |
| `GLOBAL_INFLIGHT` | `16` | `config.py` | 全局在飞任务上限 |
| `POOL_MAX_CONNECTIONS` | `20` | `client_pool.py` | httpx Limits |
| `POOL_MAX_KEEPALIVE` | `20` | `client_pool.py` | httpx Limits |
| `KEEPALIVE_EXPIRY_S` | `20.0` | `client_pool.py` | 必须 < 常见 CDN 30s 回收 |
| `CONNECT_TIMEOUT_S` | `5.0` | `client_pool.py` | 连接超时单独收紧，DNS/TLS 卡死要在 5s 内暴露 |
| `READ_TIMEOUT_S` | `15.0` | `client_pool.py` | 整页 body 读完的时限；单页 ~1.2MB 在普通宽带下够用 |
| `WRITE_TIMEOUT_S` | `5.0` | `client_pool.py` | GET 场景无 body，形式化设置 |
| `POOL_TIMEOUT_S` | `10.0` | `client_pool.py` | 拿不到连接就是限流器失配的信号，不要让它静默等 |
| `ADAPTIVE_FLOOR_RPS` | `1.0` | `rate_limiter.py` | AIMD 减性下限 |
| `ADAPTIVE_HEALTHY_WINDOW_S` | `300` | `rate_limiter.py` | 无错误多久后开始加性恢复 |
| `ADAPTIVE_RECOVER_STEP` | `1.10`（+10%） | `rate_limiter.py` | 恢复步长 |
| `MIN_INTERVAL_FROM_MAX_AGE` | `True` | `rates.py` | 尊重 `Cache-Control: max-age`（实测 120s） |
| `RETRY_AFTER_MAX_S` | `120` | `retry.py` | `Retry-After` 可信上限，超过则回落到 `BACKOFF_MAX_S` 而不是傻等 |

### 5.2 重试与退避

| 项 | 默认值 | 说明 |
|---|---|---|
| `MAX_ATTEMPTS` | `4`（=1 次 + 3 次重试） | 分类见 §6，某些错误类不重试或重试上限更低 |
| `BACKOFF_BASE_S` | `1.0` | 指数基数 |
| `BACKOFF_FACTOR` | `2.0` | 第 n 次重试的上界计算 `base * factor**(n-1)` |
| `BACKOFF_MAX_S` | `30.0` | 单次等待上限 |
| `JITTER` | `full`（`sleep = U(0, min(max, raw))`） | 全抖动，`random.uniform`；不加抖动会让 140 个分页任务在重试时同步再撞一次 |
| `TOTAL_ATTEMPT_BUDGET_S` | `90.0` | 单个请求累计耗时（含等待）预算，超预算即判定软失败并停止重试；防止抖动后的尾部请求拖垮整轮 |
| `RETRY_COST` | `1.0` 令牌 | 重试同样要过令牌桶（重试不是免费的份额） |

### 5.3 熔断

| 项 | 默认值 | 说明 |
|---|---|---|
| `BREAKER_WINDOW_S` | `60` | 滚动窗口（用 deque 计数，不用分布式的） |
| `BREAKER_MIN_SAMPLES` | `20` | 样本不足不开闸，防一次性'flapping' |
| `BREAKER_FAILURE_RATIO` | `0.5` | 窗口内失败率阈值 |
| `BREAKER_CONSECUTIVE_5XX` | `8` | 「连续 5xx」这个硬信号可以直接开闸，不等比率 |
| `BREAKER_OPEN_DURATION_S` | `60` | 打开持续时间 |
| `BREAKER_HALF_OPEN_PROBES` | `3` | 半开状态允许的探针请求数 |
| `BREAKER_HALF_OPEN_SUCCESS_TO_CLOSE` | `3` | 探针全成功才闭合；任一失败回到 open 并 `OPEN *= 2`（≤ 600s） |

### 5.4 调度周期

| 周期 | 默认值 | 约束 |
|---|---|---|
| `PRICES_INTERVAL_S` | `300` | 必须 ≥ `max-age`(=120)，且全市场 140 页 @8rps ≈ 18s 理论下限（需实测确认），留 ≥5× 余量 |
| `LISTINGS_INTERVAL_S` | `300` | 观测清单模式；`WATCHLIST_MAX_ITEMS=500` → 500 请求 @8rps ≈ 62.5s 理论下限（需实测确认） |
| `ITEMS_INTERVAL_S` | `86400` | 物品元数据（名称）日级刷新即可 |
| `NEWS_INTERVAL_S` | `900` | RSS 用 `If-None-Match`/`If-Modified-Since` 做条件请求，304 不消耗配额 |
| `GC_INTERVAL_S` | `3600` | 保留清理 + rollup + `PRAGMA optimize` |

---

## 6. 错误分类与处理矩阵

统一入口：`classification.py` 把 `(exception | ResponseView)` → `ErrorClass`。**retry / breaker / metrics / 告警四处的判定全部读这一个出口**，禁止各处自己判 `status_code == 429`。

| # | 类别 | 判定依据 | 重试 | 退避策略 | 计熔断失败 | 告警 | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | DNS 解析失败 | `httpx.ConnectError` 且 `socket.gaierror` 链路 | 是（≤2 次） | 指数+抖动；第 3 次前先 `invalidate(host)` 重建池 | 是 | 是（连续 3 次 ≥ warn） | 常常是本地 resolver/断网，重建池无意义时应快速失败 |
| 2 | 连接超时 | `httpx.ConnectTimeout` | 是（≤3 次） | 指数+抖动 base=1.0；累计 ≥2 次 → `invalidate(host)` | 是 | 连续 ≥5 次 → warn | 不要把 connect timeout 调大到污染 IO 预算 |
| 3 | 读超时 | `httpx.ReadTimeout` | 是（≤3 次） | 同 #2；同 host 60s 窗口内 ≥5 次 → `rate *= 0.7` + 重建池 | 是 | 批量出现 → warn | 单页 1.2MB 慢是正常的，不要因此把 timeout 加到 60s |
| 4 | 429 Too Many Requests | status=429 | 是（≤3 次） | **优先读 `Retry-After`**（钳到 ≤120s），缺失则 `base*2^n` + full jitter；并立即 `rate *= 0.5` | 是 | 是（必告） | 收到 429 说明我们的速率模型错了，必须立刻调低 `rate`，而不只是退避重试 |
| 5 | 5xx Server Error | status ∈ [500,599] | 是（≤3 次） | 指数+抖动；连续 8 次 → 直接开闸 | 是 | 连续 ≥3 → warn | 503 常伴随 `Retry-After`，同样优先读 |
| 6 | 420 / vendor 自定义限流码 | status == 420 或 body 含官方限流关键字 | 是（≤2 次） | **强制** `max(Retry-After, 30s)` 起步，`rate *= 0.5` | 是 | 是（必告） | ESI 的 420 语义在本仓已无对应源；此分类为**适配器可插拔**保留（其它官方 API 用 420/430） |
| 7 | 限流头预警 | 存在 `X-RateLimit-Remaining` 且 ≤ 10% limit | **不重试**（请求是成功的） | **主动降速**：`rate *= 0.6`，并在下一个令牌周期之前 `await sleep(1/rate)` | 否 | ≤20% 时 info | 本实测源**不返回 Remaining 头**，故该探测器默认关闭（`RATELIMIT_HEADER_PROBE=false`），但协议保留 |
| 8 | JSON 解析失败 | `json.JSONDecodeError` / `ResponseView.json()` 抛错 | 是（≤2 次） | 固定 `2.0s * n`（不指数），第 2 次仍失败则**落样本** | 是 | 是（必告） | 落 `data/rejects/<ts>_<host>_<hash>.txt` 前 8KB + headers，**不静默丢弃**；重复出现通常意味着端点 Schema 变了，应停机排查而非重试 |
| 9 | 分页漂移 | 本轮各页 `X-Page-Total` 不一致，或 ids 出现跨页重复 | 否（数据层面处理） | — | 否 | 漂移 >10% → warn | 处理策略见 §6.1；**不得**因此重试已经成功取到的页 |
| 10 | 数据去重冲突 | `UNIQUE constraint failed` / `ON CONFLICT` 分支命中 | 否 | — | 否 | 否（计数指标） | 合法情况：同一 `observed_at` 重跑。用 `INSERT ... ON CONFLICT DO UPDATE` 幂等化；非预期的冲突才告警 |
| 11 | Schema 校验失败 | `pydantic.ValidationError` / 字段缺失 | 否 | — | 是 | 是（必告） | 隔离该 item，不阻断整轮；连续 ≥20 个 item 失败 → 判定端点改版，停机 |
| 12 | 一致性校验失败 | `consistency.py` 任一条规则不过 | 否 | — | 否 | 数据类告警 warn | 写入 `data_reject` 日志表并标记 `snapshot='QUARANTINED'`，**异动检测跳过被隔离的快照** |
| 13 | robots 拒绝 | `RobotsGuard` 返回 False | **否，立即终止该源** | — | 否 | 是（critical） | 不可配置绕过，见 §11.2 |
| 14 | 403 / 401 | status ∈ {401,403} | 否 | — | 是 | 是（critical） | 官方 API 对我们不需要鉴权，出现 401/403 = **上下文变了**（可能是限额/封禁/条款变更），停机等待人工，不得换 UA 重试、不得走代理 |
| 15 | SSL 校验失败 | `httpx.ssl` 相关异常 | 否 | — | 是 | 是（critical） | 修环境（证书/系统时间），**禁止** `verify=False` |
| 16 | 304 Not Modified | status=304 | 否 | — | 否（计成功） | 否 | 条件请求正常路径，直接复用上一份 `content_hash` |
| 17 | 任务被取消 | `asyncio.CancelledError` | 否 | — | 否 | 否 | 必须原样 `raise` 传播（禁止 `except Exception` 吞掉），否则优雅退出失效 |
| 18 | 未知异常 | 兜底 | 否 | — | 是 | 是（critical） | 记完整 traceback + 当前 task 上下文；未知异常的**默认是不重试**（宁可丢一轮也不要触发不可控行为） |

### 6.1 分页漂移的具体处理（这条 Phase 2 最容易踩）

`X-Result-Total` 实测 27997，一轮 140 页遍历期间它**一定会变**（下一秒就有新的上架/下架）。

```
1) 用 dict[item_id] = payload 累积，天然幂等：同一 item 跨页出现两次 → 后者覆盖前者（记 metric `pagination.duplicate_item`）
2) 记录 page={0..N-1} 各自的 X-Page-Total 到 set()；|set| > 1 → 记 `pagination.drifted`，但**不重试**
3) 漂移率 = |max-min| / min；> 10% → 本轮快照标记 quality='PARTIAL'
4) quality='PARTIAL' 的那一轮：
   - 数据照常落库（不会缺 numeric fields 的 items）
   - 但**跳过本轮未采到（missing）物品的异动检测**（避免把「本轮没抓到」误判成「价格归零/暴跌」）
   - 且回看一个周期（look-behind）：若下一轮该 item 重新出现且值回到旧区间，则不产生回归告警
5) 绝不用 page 当作幂等键；幂等键是 (cycle_id, item_id)
```

---

## 7. 核心数据结构

### 7.1 传输契约（Phase 2 必须按此签名实现）

```python
# src/market_watch/domain/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Mapping, Sequence, TypedDict

type Cursor = dict[str, str]        # 业务游标：ETag / Last-Modified / 最大 published_at 等

# 全文所有 datetime 一律 timezone-aware UTC；落库序列化为 ISO8601 'Z' 字符串


class ErrorClass(StrEnum):
    OK = "ok"
    DNS_FAILURE = "dns_failure"
    CONNECT_TIMEOUT = "connect_timeout"
    READ_TIMEOUT = "read_timeout"
    POOL_TIMEOUT = "pool_timeout"
    REMOTE_PROTOCOL = "remote_protocol"
    RATE_LIMITED_429 = "rate_limited_429"
    RATE_LIMITED_VENDOR = "rate_limited_vendor"
    RATE_LIMIT_HEADER_LOW = "rate_limit_header_low"
    SERVER_5XX = "server_5xx"
    AUTH_403 = "auth_403"
    ROBOTS_DENIED = "robots_denied"
    JSON_DECODE = "json_decode"
    SCHEMA_INVALID = "schema_invalid"
    PAGINATION_DRIFT = "pagination_drift"
    DEDUPE_CONFLICT = "dedupe_conflict"
    CONSISTENCY_FAILED = "consistency_failed"
    SSL_INVALID = "ssl_invalid"
    NOT_MODIFIED = "not_modified"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class AttemptPolicy(TypedDict):
    retryable: bool
    max_attempts: int
    count_as_failure: bool
    cool_host: bool          # 是否立即按 cool_factor 对该主机降速
    cool_factor: float       # 降速系数，如 0.5 / 0.6 / 0.7
    open_breaker_now: bool
    alert_level: Literal["none", "info", "warn", "critical"]
    respect_retry_after: bool


@dataclass(frozen=True, slots=True)
class ResponseView:
    """httpx.Response 的快照。禁止把 httpx 对象传进 domain/application。"""
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    url: str
    elapsed_ms: float

    def json(self) -> Any: ...                      # 解析失败抛 PayloadError -> JSON_DECODE
    @property
    def page_total(self) -> int | None: ...         # X-Page-Total
    @property
    def page_size(self) -> int | None: ...          # X-Page-Size
    @property
    def result_total(self) -> int | None: ...       # X-Result-Total
    @property
    def rate_limit(self) -> int | None: ...         # X-Rate-Limit-Limit
    @property
    def rate_remaining(self) -> int | None: ...     # 多数源不返回 -> None
    @property
    def max_age_seconds(self) -> int | None: ...    # Cache-Control: max-age
    @property
    def etag(self) -> str | None: ...
    @property
    def last_modified(self) -> str | None: ...
    @property
    def retry_after_seconds(self) -> float | None: ...


@dataclass(frozen=True, slots=True)
class FetchTask:
    task_id: str
    source_id: str
    host: str
    method: Literal["GET", "HEAD"]
    path: str                       # 不含 host，便于 robots 判定
    params: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    cost: float = 1.0               # 令牌成本
    strategy: Literal["paged", "single", "watchlist"] = "single"
    cursor: str | None = None       # 业务游标，透传给 adapter


@dataclass(frozen=True, slots=True)
class FetchResult:
    """一次 fetch 的完整结果，供 retry/breaker/metrics/fetch_log 消费。"""
    task_id: str
    view: ResponseView | None
    error_class: ErrorClass
    attempts: int
    total_ms: float
    slept_ms: float
    retry_after_used_s: float | None = None


@dataclass(frozen=True, slots=True)
class ParseResult:
    task_id: str
    cycle_id: str
    source_id: str
    records: Sequence["Record"]
    next_tasks: Sequence[FetchTask] = ()
    cursor: Cursor | None = None
    quality: Literal["COMPLETE", "PARTIAL"] = "COMPLETE"
    consistency_failures: Sequence["ConsistencyViolation"] = ()
```

> `Record` 类型别名定义在 `models.py` 末尾（须在四个实体类之后）：
> `Record = PriceTick | ListingLevel | ItemMeta | Announcement`
```

### 7.2 业务实体

```python
# src/market_watch/domain/models.py（续）

@dataclass(frozen=True, slots=True)
class PriceTick:
    """来源于 /v2/commerce/prices 的聚合报价快照。"""
    source_id: str
    item_id: int
    observed_at: datetime
    cycle_id: str
    buy_unit_price: int | None          # best bid（铜币整数）
    buy_quantity: int | None            # 买盘总量
    sell_unit_price: int | None         # best ask
    sell_quantity: int | None           # 卖盘总量
    whitelisted: bool
    quality: Literal["COMPLETE", "PARTIAL"] = "COMPLETE"


@dataclass(frozen=True, slots=True)
class ListingLevel:
    """来源于 /v2/commerce/listings 的原始挂单档位。"""
    source_id: str
    item_id: int
    observed_at: datetime
    cycle_id: str
    side: Literal["buy", "sell"]
    unit_price: int                     # > 0
    quantity: int                       # >= 0
    listings: int                       # 该价位的挂单笔数 >= 1


@dataclass(frozen=True, slots=True)
class PriceAggregate:
    """domain.pricing 由 ListingLevel 序列算出的每分钟/每小时派生。"""
    item_id: int
    bucket_ts: datetime
    best_bid: int | None
    best_ask: int | None
    spread: int | None                  # best_ask - best_bid；必须为 >= 0，由一致性规则校验
    mid: int | None
    vwap_buy: float | None
    vwap_sell: float | None
    depth_buy: int
    depth_sell: int
    samples: int


@dataclass(frozen=True, slots=True)
class ItemMeta:
    source_id: str
    item_id: int
    name: str
    item_type: str | None
    rarity: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Announcement:
    """来源：官方 RSS/Atom。"""
    source_id: str
    guid: str                           # 自然主键（优先 <guid>；缺失用 sha1(link|title)）
    title: str
    link: str
    published_at: datetime | None
    summary: str | None
    content_hash: str                   # sha256(title|summary|link) 前 32 hex
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class AlertEvent:
    alert_id: str                       # uuid4
    dedupe_key: str                     # sha1(source_id|rule|item_id|cooldown_bucket)
    rule: str                           # 见 config/alerts.yaml 的 rule id
    source_id: str
    item_id: int | None
    severity: Literal["info", "warn", "critical"]
    metric: str                         # 'sell_unit_price' | 'spread' | 'depth_sell' | ...
    prev_value: float | None
    curr_value: float | None
    delta_pct: float | None
    message: str
    fired_at: datetime
    cooldown_until: datetime
    acknowledged: bool = False


@dataclass(frozen=True, slots=True)
class ConsistencyViolation:
    rule: str
    item_id: int | None
    field: str
    observed: str
    expected: str
    severity: Literal["warn", "critical"]


@dataclass(frozen=True, slots=True)
class Politeness:
    """每主机的礼貌预算；adapter 声明，config 钳制。"""
    rps: float
    burst: int
    concurrency: int
    min_interval_seconds: int
    respect_max_age: bool = True
    daily_request_budget: int | None = None     # None = 不限制；可对 duty cycle 做保护


@dataclass(frozen=True, slots=True)
class RateState:
    host: str
    current_rps: float
    capacity: int
    tokens: float
    consumed_total: int
    throttled_total: int
    last_penalty_at: datetime | None


# ===== 必须在文件末尾（四个实体类之后）声明 =====
Record = PriceTick | ListingLevel | ItemMeta | Announcement
type WriteBatch = tuple[str, Sequence[tuple]]      # (table_name, rows)，交给 SnapshotSink.submit
```

### 7.3 `SourceAdapter` 最小接口契约（可插拔的核心）

```python
# src/market_watch/domain/ports.py
from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .models import Cursor, FetchResult, FetchTask, ParseResult, Politeness, ResponseView


@runtime_checkable
class SourceAdapter(Protocol):
    """新增一个官方公开源 = 实现这 6 个成员，采集内核一行不改。"""

    # ---- 声明区（类属性/小 property） ----
    source_id: str                                   # 全局唯一，落 natural key
    kind: str                                        # 'price' | 'orderbook' | 'item_meta' | 'announcement'
    host: str                                        # 单一主机；跨主机请在仓库里建两个 adapter
    politeness: Politeness                           # 声明式礼貌预算（会被 Config 钳制）

    # ---- 任务区（同步、纯函数、不发请求） ----
    def initial_tasks(self, cursor: Cursor | None) -> Sequence[FetchTask]:
        """给出本轮首批任务（通常是 page=0，或单次请求的 URL）。"""
        ...

    def next_tasks(self, task: FetchTask, view: ResponseView) -> Sequence[FetchTask]:
        """依据响应头推进分页；不推进时返回 ()。禁止在此发请求。"""
        ...

    # ---- 解析区（同步、纯函数） ----
    def parse(self, task: FetchTask, view: ResponseView) -> ParseResult:
        """把 payload 转成 domain 模型 + 可选的下游任务。抛 PayloadError/SchemaError 由内核分类。"""
        ...

    # ---- 增量区 ----
    def cursor_from(self, view: ResponseView, result: ParseResult) -> Cursor | None:
        """从 ETag / Last-Modified / 最大 published_at 导出下轮游标。"""
        ...

    # ---- 合规区 ----
    def user_agent_suffix(self) -> str:
        """形如 'market-watch/0.1.0 (+https://you.example/contact)'，最终 UA 由内核拼接。"""
        ...
```

**契约五条硬规则（契约测试会跑，见 §9.2）**

1. `initial_tasks` / `next_tasks` / `parse` / `cursor_from` **必须是同步纯函数**，不得 `await`、不得 IO、不得读时钟（时间由调用方通过 `FetchTask`/`CycleContext` 注入）。
2. `next_tasks` **必须可终止**：当 `page + 1 >= X-Page-Total` 或响应为空数组时返回 `()`。测试会强制注入「永不分页」的 mock 并断言有限步收敛。
3. `parse` **不得因单条坏数据抛异常中断整页**：坏记录应进 `ParseResult.consistency_failures`，继续产出其余记录。
4. `politeness` 是声明值，实际生效值 = `min(adapter.politeness.rps, OFFICIAL_LIMIT/60 * HOST_SAFETY_FACTOR)`，**adapter 无权突破**。
5. 一个 adapter 只能用一个 `host`。多 host → 多 adapter。**禁止**在 adapter 内 new httpx.Client（`fetcher` 是全仓唯一出网点）。

---

## 8. 存储设计

### 8.1 连接与 PRAGMA

```python
# src/market_watch/infrastructure/storage/sqlite.py
PRAGMAS = (
    "PRAGMA journal_mode=WAL",          # 读写不互斥，看板读不被写阻塞
    "PRAGMA synchronous=NORMAL",        # WAL 下的性价比最优；掉电丢失窗口 ~last checkpoint，可接受
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",         # 单写者 + 5s 等待，够覆盖 99% 争用
    "PRAGMA wal_autocheckpoint=1024",   # 每 1024 页（~4MB）自动 checkpoint，避免 WAL 无限膨胀
    "PRAGMA journal_size_limit=67108864",  # WAL 文件上限 64MB
    "PRAGMA temp_store=MEMORY",
    "PRAGMA mmap_size=268435456",       # 256MB 只读侧 mmap，看板查询提速
    "PRAGMA cache_size=-65536",         # 64MB page cache
)
```

单写者瓶颈的解法：**一个专用写线程 + `asyncio.Queue`**。 asyncio 侧 `await writer.submit(batch)`，线程侧批量 `executemany`。看板读侧另开**只读连接**（`file:...?mode=ro&immutable=0` URI + `query_only`）。

### 8.2 表结构全量 DDL

```sql
-- src/market_watch/infrastructure/storage/migrations/0001_init.sql
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

CREATE TABLE IF NOT EXISTS source_state (
    source_id            TEXT PRIMARY KEY,
    last_cursor          TEXT,
    last_cycle_id        TEXT,
    last_run_at          TEXT,
    last_success_at      TEXT,
    last_error_class     TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    robots_checked_at    TEXT,
    robots_allowed       INTEGER NOT NULL DEFAULT 1,
    updated_at           TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS items (
    item_id      INTEGER PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    item_type    TEXT,
    rarity       TEXT,
    whitelisted  INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS watchlist (
    item_id  INTEGER PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE,
    reason   TEXT,
    added_at TEXT NOT NULL
) STRICT;

-- 全市场「最新态」：每 item 一行，每周期 upsert，行数恒定 ≈ 27997
CREATE TABLE IF NOT EXISTS price_latest (
    item_id         INTEGER PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE,
    observed_at     TEXT NOT NULL,
    cycle_id        TEXT NOT NULL,
    buy_unit_price  INTEGER,
    buy_quantity    INTEGER,
    sell_unit_price INTEGER,
    sell_quantity   INTEGER,
    whitelisted     INTEGER NOT NULL DEFAULT 1,
    quality         TEXT NOT NULL DEFAULT 'COMPLETE' CHECK (quality IN ('COMPLETE','PARTIAL'))
) STRICT, WITHOUT ROWID;

-- 变更事件流：仅在数值与上一次相比有变化时追加（进程内 change gate）
CREATE TABLE IF NOT EXISTS price_change (
    change_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    observed_at     TEXT NOT NULL,
    cycle_id        TEXT NOT NULL,
    buy_unit_price  INTEGER,
    buy_quantity    INTEGER,
    sell_unit_price INTEGER,
    sell_quantity   INTEGER,
    prev_sell_price INTEGER,
    delta_pct       REAL,
    rolled_up       INTEGER NOT NULL DEFAULT 0   -- 是否已滚入 price_hourly；防止 rollup 重复执行造成 samples 翻倍
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_price_change ON price_change(item_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_price_change_item_time ON price_change(item_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_price_change_pending ON price_change(observed_at) WHERE rolled_up = 0;

-- 原始挂单档位：仅观测清单内的 item 采集
CREATE TABLE IF NOT EXISTS listing_level (
    item_id     INTEGER NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    side        TEXT NOT NULL CHECK (side IN ('buy','sell')),
    unit_price  INTEGER NOT NULL CHECK (unit_price > 0),
    quantity    INTEGER NOT NULL CHECK (quantity >= 0),
    listings    INTEGER NOT NULL CHECK (listings >= 1),
    observed_at TEXT NOT NULL,
    cycle_id    TEXT NOT NULL,
    PRIMARY KEY (item_id, side, unit_price, observed_at)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_listing_time ON listing_level(item_id, side, observed_at DESC);

-- 小时 K 线 rollup：只做「观测清单 + TopN 成交额」的物品，长期保留
CREATE TABLE IF NOT EXISTS price_hourly (
    item_id     INTEGER NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    bucket_ts   TEXT NOT NULL,                 -- 'YYYY-MM-DDTHH:00:00Z'
    open_sell   INTEGER, high_sell INTEGER, low_sell INTEGER, close_sell INTEGER,
    avg_sell    REAL, vwap_sell REAL, spread REAL,
    depth_buy   INTEGER NOT NULL DEFAULT 0, depth_sell INTEGER NOT NULL DEFAULT 0,
    samples     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, bucket_ts)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_price_hourly_bucket ON price_hourly(bucket_ts DESC);

CREATE TABLE IF NOT EXISTS announcement (
    source_id     TEXT NOT NULL,
    guid          TEXT NOT NULL,
    title         TEXT NOT NULL,
    link          TEXT NOT NULL,
    published_at  TEXT,
    summary       TEXT,
    content_hash  TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (source_id, guid)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_announcement_published ON announcement(published_at DESC);

CREATE TABLE IF NOT EXISTS alert_event (
    alert_id        TEXT PRIMARY KEY,
    dedupe_key      TEXT NOT NULL,
    rule            TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    item_id         INTEGER,
    severity        TEXT NOT NULL CHECK (severity IN ('info','warn','critical')),
    metric          TEXT NOT NULL,
    prev_value      REAL, curr_value REAL, delta_pct REAL,
    message         TEXT NOT NULL,
    fired_at        TEXT NOT NULL,
    cooldown_until  TEXT NOT NULL,
    acknowledged    INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_dedupe ON alert_event(dedupe_key, cooldown_until);
CREATE INDEX IF NOT EXISTS idx_alert_fired ON alert_event(fired_at DESC);

-- 数据质量隔离区：被一致性规则拦下的记录，永不见异动检测，也不静默丢弃
CREATE TABLE IF NOT EXISTS data_reject (
    reject_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id    TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    item_id     INTEGER,
    rule        TEXT NOT NULL,
    field       TEXT NOT NULL,
    observed    TEXT,
    expected    TEXT,
    severity    TEXT NOT NULL,
    payload     TEXT,          -- 截断到 4096 字符的原文片段
    created_at  TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_reject_created ON data_reject(created_at DESC);

-- 合规审计：每一次出站请求的留痕
CREATE TABLE IF NOT EXISTS fetch_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id     TEXT,
    host         TEXT NOT NULL,
    path         TEXT NOT NULL,
    method       TEXT NOT NULL,
    status_code  INTEGER,
    attempts     INTEGER NOT NULL,
    duration_ms  REAL,
    resp_bytes   INTEGER,
    error_class  TEXT,
    robots_ok    INTEGER NOT NULL DEFAULT 1,
    ua           TEXT NOT NULL,
    ts           TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_fetch_log_ts ON fetch_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_fetch_log_host_ts ON fetch_log(host, ts DESC);
```

### 8.3 Upsert 与增量去重策略

**为什么必须分层（这是本设计里最容易被低估的一条）**：
全部 `X-Result-Total=27997` 个物品，若直接每 5 分钟 append 一行 = `27997 × 288/天 ≈ 806 万行/天`，保留 7 天 ≈ **5600 万行**，单文件 SQLite 会迅速膨胀到数 GB。所以拆三层：

| 层 | 表 | 写入策略 | 规模（预算，需实测确认） |
|---|---|---|---|
| 最新态 | `price_latest` | 每周期全量 `INSERT ... ON CONFLICT DO UPDATE`，行数**恒定** ≈ 28k | 28k 行，稳定 |
| 变更流 | `price_change` | **仅当四项数值任一变化时追加**（进程内 change gate） | 假设每周期 15% 变动 → ~4.2k 行/周期 → ~120 万行/天；默认保留 72h ≈ 360 万行 |
| 长期 | `price_hourly` | 由 change/latest 在 GC 周期 rollup，**仅 TopN=2000 物品** | 2000 × 24 × 180 天 ≈ 864 万行 |

**Change gate 实现（进程内，热路径）**：
```python
# src/market_watch/application/dedupe.py
class LastSeenTracker:
    """dict[item_id] -> (buy_px, buy_qty, sell_px, sell_qty, observed_at)
    命中即跳过写入；用 collections.OrderedDict 手工维护 LRU，容量=最近一轮的 item 数 + 20%"""
    def changed(self, tick: PriceTick) -> tuple[bool, int | None]: ...
```
容量 ~34k 条 tuple × ~120B ≈ 4MB RSS，可接受；进程重启后第一轮全写（天然触发一次补写），这不算故障。

**核心 upsert 语句**：

```sql
-- price_latest：幂等覆盖
INSERT INTO price_latest
  (item_id, observed_at, cycle_id, buy_unit_price, buy_quantity, sell_unit_price, sell_quantity, whitelisted, quality)
VALUES (?,?,?,?,?,?,?,?,?)
ON CONFLICT(item_id) DO UPDATE SET
  observed_at     = excluded.observed_at,
  cycle_id        = excluded.cycle_id,
  buy_unit_price  = excluded.buy_unit_price,
  buy_quantity    = excluded.buy_quantity,
  sell_unit_price = excluded.sell_unit_price,
  sell_quantity   = excluded.sell_quantity,
  whitelisted     = excluded.whitelisted,
  quality         = excluded.quality
WHERE excluded.observed_at >= price_latest.observed_at;   -- 防止乱序回写旧值

-- listing_level：同一 (item_id, side, unit_price, observed_at) 覆盖，天然幂等
INSERT INTO listing_level (item_id, side, unit_price, quantity, listings, observed_at, cycle_id)
VALUES (?,?,?,?,?,?,?)
ON CONFLICT(item_id, side, unit_price, observed_at) DO UPDATE SET
  quantity = excluded.quantity, listings = excluded.listings;

-- announcement：仅当内容确实变化时才刷新 updated_at（配合 rowcount 判定 new/updated）
INSERT INTO announcement (source_id, guid, title, link, published_at, summary, content_hash, first_seen_at, updated_at)
VALUES (?,?,?,?,?,?,?,?,?)
ON CONFLICT(source_id, guid) DO UPDATE SET
  title = excluded.title, summary = excluded.summary,
  content_hash = excluded.content_hash, updated_at = excluded.updated_at
WHERE announcement.content_hash IS NOT excluded.content_hash;

-- price_hourly rollup：幂等重算
INSERT INTO price_hourly (item_id, bucket_ts, open_sell, high_sell, low_sell, close_sell,
                          avg_sell, vwap_sell, spread, depth_buy, depth_sell, samples)
SELECT pc.item_id, :bucket,
       -- open：已有桶则继承既有 open_sell；首次入库则取本桶最早一条
       COALESCE(
           (SELECT ph.open_sell FROM price_hourly ph
             WHERE ph.item_id = pc.item_id AND ph.bucket_ts = :bucket),
           (SELECT pc0.sell_unit_price FROM price_change pc0
             WHERE pc0.item_id = pc.item_id
               AND pc0.observed_at >= :bucket_start AND pc0.observed_at < :bucket_end
             ORDER BY pc0.observed_at ASC LIMIT 1)
       ),
       MAX(pc.sell_unit_price),
       MIN(pc.sell_unit_price),
       -- close：本桶最后一条
       (SELECT pc2.sell_unit_price FROM price_change pc2
         WHERE pc2.item_id = pc.item_id
           AND pc2.observed_at >= :bucket_start AND pc2.observed_at < :bucket_end
         ORDER BY pc2.observed_at DESC LIMIT 1),
       AVG(pc.sell_unit_price),
       SUM(pc.sell_unit_price * pc.sell_quantity) / NULLIF(SUM(pc.sell_quantity), 0),
       AVG(pc.sell_unit_price - pc.buy_unit_price),
       SUM(pc.buy_quantity), SUM(pc.sell_quantity), COUNT(*)
FROM price_change pc
WHERE pc.observed_at >= :bucket_start AND pc.observed_at < :bucket_end
  AND pc.rolled_up = 0                            -- 幂等关键：只滚未被滚过的行
GROUP BY pc.item_id
ON CONFLICT(item_id, bucket_ts) DO UPDATE SET
  high_sell=MAX(high_sell,excluded.high_sell), low_sell=MIN(low_sell,excluded.low_sell),
  close_sell=excluded.close_sell, avg_sell=excluded.avg_sell, vwap_sell=excluded.vwap_sell,
  spread=excluded.spread, depth_buy=excluded.depth_buy, depth_sell=excluded.depth_sell,
  samples=price_hourly.samples+excluded.samples;
```

**批参数**：`executemany` 批大小 `500` 行；每个 cycle 一个显式事务；写线程每次从队列里尽量取到 `MAX_BATCH=2000` 条一次性提交。`busy_timeout=5000` + 单写线程 ⇒ 不需要 `ThreadPoolExecutor` 里的锁。

### 8.4 保留策略与清理

```python
# src/market_watch/domain/retention.py
RETENTION = {
    "price_change":   timedelta(hours=72),
    "listing_level":  timedelta(hours=48),
    "price_hourly":   timedelta(days=180),      # 且仅 TopN 物品在列
    "fetch_log":      timedelta(days=14),       # 合规审计留痕
    "data_reject":    timedelta(days=14),
    "alert_event":    timedelta(days=90),       # acknowledged=1 才允许删
    "price_latest":   None,                     # 最新态不删
    "announcement":   None,                     # 公告永久保留（量小）
}

def cutoffs(now: datetime) -> dict[str, str]: ...   # 返回 ISO8601 cutoff，纯函数，可单测
```

```sql
-- src/market_watch/infrastructure/storage/maintenance.py 每小时跑一次
-- 步骤 1：先把即将被删的 price_change 滚进 price_hourly（语句全文见 §8.3 的 rollup Upsert，此处重复列出以保证顺序自洽）
INSERT INTO price_hourly (item_id, bucket_ts, open_sell, high_sell, low_sell, close_sell,
                          avg_sell, vwap_sell, spread, depth_buy, depth_sell, samples)
SELECT pc.item_id, :bucket,
       COALESCE(
           (SELECT ph.open_sell FROM price_hourly ph
             WHERE ph.item_id = pc.item_id AND ph.bucket_ts = :bucket),
           (SELECT pc0.sell_unit_price FROM price_change pc0
             WHERE pc0.item_id = pc.item_id
               AND pc0.observed_at >= :bucket_start AND pc0.observed_at < :bucket_end
             ORDER BY pc0.observed_at ASC LIMIT 1)),
       MAX(pc.sell_unit_price), MIN(pc.sell_unit_price),
       (SELECT pc2.sell_unit_price FROM price_change pc2
         WHERE pc2.item_id = pc.item_id
           AND pc2.observed_at >= :bucket_start AND pc2.observed_at < :bucket_end
         ORDER BY pc2.observed_at DESC LIMIT 1),
       AVG(pc.sell_unit_price),
       SUM(pc.sell_unit_price * pc.sell_quantity) / NULLIF(SUM(pc.sell_quantity), 0),
       AVG(pc.sell_unit_price - pc.buy_unit_price),
       SUM(pc.buy_quantity), SUM(pc.sell_quantity), COUNT(*)
FROM price_change pc
WHERE pc.observed_at >= :bucket_start AND pc.observed_at < :bucket_end
  AND pc.rolled_up = 0                            -- 幂等关键：只滚未被滚过的行
GROUP BY pc.item_id
ON CONFLICT(item_id, bucket_ts) DO UPDATE SET
  high_sell = MAX(high_sell, excluded.high_sell),
  low_sell  = MIN(low_sell,  excluded.low_sell),
  close_sell = excluded.close_sell, avg_sell = excluded.avg_sell, vwap_sell = excluded.vwap_sell,
  spread = excluded.spread, depth_buy = excluded.depth_buy, depth_sell = excluded.depth_sell,
  samples = price_hourly.samples + excluded.samples;

-- 步骤 1b：打标记（与 1a 在同一事务）。只靠「rollup 后再删」不够：
--       若进程在 rollup 与 delete 之间崩溃，重跑会把同一批行再滚一次，samples 翻倍。
UPDATE price_change SET rolled_up = 1
 WHERE observed_at >= :bucket_start AND observed_at < :bucket_end AND rolled_up = 0;

-- 步骤 2：删过期数据（必须在 rollup 之后）
DELETE FROM price_change  WHERE observed_at < :cut_price_change;
DELETE FROM listing_level WHERE observed_at < :cut_listing_level;
DELETE FROM fetch_log     WHERE ts          < :cut_fetch_log;
DELETE FROM data_reject   WHERE created_at  < :cut_data_reject;
DELETE FROM alert_event   WHERE fired_at < :cut_alert_event AND acknowledged = 1;
DELETE FROM price_hourly  WHERE bucket_ts < :cut_price_hourly
                            AND item_id NOT IN (SELECT item_id FROM watchlist);

-- 步骤 3：整理
PRAGMA optimize;
PRAGMA incremental_vacuum;
```

顺序铁律：**先 rollup → 再删 change → 最后 vacuum**。颠倒会永久丢数据。

---

## 9. 可测试性

### 9.1 三级测试策略（不依赖网络）

| 级 | 手段 | 覆盖目标 |
|---|---|---|
| L1 单元（毫秒级） | 纯函数直接测：`pricing`/`anomaly`/`consistency`/`retention`/`policy`/`retry.decide()`/`TokenBucket`（`loop.time()` 用 anyio 的 mock clock）/ `Breaker`（注入假时钟） | 全部决策逻辑，目标 ≥85% 分支覆盖 |
| L2 契约（秒级） | `httpx.MockTransport` 喂 `adapters/fixtures/` 的录制样本 + `test_adapter_contract.py` 参数化跑全部 adapter | 每个 adapter 的行为不变量 |
| L3 集成（秒级） | `respx` mock 全链路 + 临时 SQLite（`tmp_path`）+ 真跑一轮 `--once` | pipe 拼接正确性、写入行数、告警触发 |

fixture 分层约定（`conftest.py`）：

```python
# tests/conftest.py
@pytest.fixture(scope="session")
def http_fixtures() -> FixtureRouter: ...                 # 读 tests/fixtures/http/*，构建 URL→payload 路由

@pytest.fixture
def mock_transport(http_fixtures) -> httpx.MockTransport: ...   # function 级，可注入故障

@pytest.fixture
async def client_pool(mock_transport): ...                # ClientPool(transport_factory=lambda: mock_transport)

@pytest.fixture
def sqlite_conn(tmp_path): ...                            # 跑 0001_init.sql 的空库，PRAGMA 与生产一致

@pytest.fixture
def fault_injector(mock_transport): ...                   # .fail_next(n=1, exc=ReadTimeout) / .status_once({"*": 429})
```

`respx` 用于 L3 integration（全局拦截真 httpx 请求，防止 CI 里意外出网）：

```python
# tests/integration/test_pipeline_once.py
import respx, httpx

@respx.mock(assert_all_called=False)
async def test_one_cycle_persists_and_alerts(sqlite_conn, http_fixtures):
    respx.get(url__startswith="https://api.guildwars2.com/v2/commerce/prices").mock(
        side_effect=http_fixtures.price_pages(status=200, pages=3, per_page=200)
    )
    ...
```

**防出网护栏**（CI 必须有一条）：`conftest.py` 的 autouse session fixture 里让 `socket.socket.connect` 抛异常，任何测试里的真实网络调用立即失败并给出提示：

```python
# tests/conftest.py
import socket
import pytest

class NetworkAccessDenied(RuntimeError): ...

@pytest.fixture(autouse=True, scope="session")
def _no_network():
    original = socket.socket.connect

    def _blocked(self, *args, **kwargs):
        raise NetworkAccessDenied(
            "测试禁止出网：请使用 httpx.MockTransport 或 respx，见 docs/01-architecture.md §9.1"
        )

    socket.socket.connect = _blocked          # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = original      # type: ignore[method-assign]
```

### 9.2 契约测试清单（`test_adapter_contract.py`）

对 `registry.all_adapters()` 里的**每一个** adapter 参数化跑：

1. `politeness.rps <= 有效上限`（= `OFFICIAL_LIMIT/60 * HOST_SAFETY_FACTOR`）——防止有人写了个 50 rps 的 adapter。
2. `politeness.min_interval_seconds >= 0` 且 `respect_max_age is True`。
3. `host` 是单个 hostname，不含 scheme、不含 path、不含端口以外的东西。
4. `user_agent_suffix()` 非空、含版本号、含联系 URI（正则 `market-watch/\d+\.\d+\.\d+ \(\+https?://`）。
5. **终止性**：用「永远返回同样 `X-Page-Total`」的恶意 mock，`next_tasks` 必须在有限步（≤ `max_total_pages + 5`）返回 `()`。
6. **纯函数性**：用同一 `(task, view)` 调两次 `parse`，结果必须深度相等。
7. **容错性**：注入 1 条坏记录（负价格/缺字段）的全页，`parse` 不得抛异常，坏记录必须出现在 `consistency_failures`。
8. **只读性**：`initial_tasks` 产出的所有 task `method ∈ {"GET","HEAD"}` 且不得带 body。
9. **robots 一致性**：所有 task 的 `path` 都必须通过 `RobotsGuard`（用仓库留存的 robots 快照）。

### 9.3 目录与命名约定

- 测试文件 `test_<被测模块>.py`；类名 `Test<Thing>`；用例名 `test_<行为>_<条件>`，例如 `test_retry_decide__429_respects_retry_after`。
- 每个 behavior 一个 assert 簇，**禁止**一个测试函数断言 8 件事。
- 覆盖率门槛：`--cov=market_watch --cov-fail-under=80`；`domain/` 与 `infrastructure/http/` 建议目标 ≥90（这是出 bug 代价最高的两层）。
- 异步统一用 `pytest-asyncio`（`asyncio_mode = "auto"`），**不要**在测试中 `asyncio.run()`。
- mock HTTP 只用 `httpx.MockTransport` / `respx`；**禁止** monkeypatch `httpx.AsyncClient.get`（那测不到 transport 层的重试与连接池逻辑）。

---

## 10. 性能剖析与压测

### 10.1 预算（目标值，**均需实测确认**，不是 benchmark）

| 指标 | 预算目标 | 计算依据 |
|---|---|---|
| 全市场快照单轮耗时 | ≤ 60 s（`PRICES_INTERVAL_S=300` 的 20%） | 140 页 @ 8 rps ≈ 17.5 s 网络下限 + 解析/写入开销；按 3× 余量给预算 |
| 观测清单 listings 单轮耗时 | ≤ 150 s（500 items @ LISTINGS_INTERVAL_S=300） | 500 请求 @ 8 rps ≈ 62.5 s 网络下限 |
| 单页解析 P99 | ≤ 150 ms | 200 条记录的 `json.loads` + dataclass 构建；超过就换 `orjson` 或 `msgspec`，**不是**开进程池 |
| 进程 RSS 峰值 | ≤ 300 MB | Python 解释器 ~25MB + httpx 连接池 + `LastSeenTracker` ≈4MB + 最多 4 份 page buffer（~1.2MB×4） |
| SQLite 写入 | 单轮 28k upsert 在 ≤10 s 内完成 | 单写线程 + `executemany(500)` + WAL + `synchronous=NORMAL`；**需实测**，不够就把 `MAX_BATCH` 调到 5000 或临时 `synchronous=OFF` |
| 看板首屏 | ≤ 300 ms（本地 sqlite） | 只读连接 + `price_latest` JOIN `items` + 服务端降采样到 ≤2000 点 |
| DB 文件 30 天体积 | ≤ 1.5 GB | 按 §8.3 分层：change 360 万行 + hourly 864 万行；**需实测**，超了先砍 `TopN` 与 `price_change` 保留期 |

### 10.2 剖析接入点

```bash
# 1) 命令行一次性跑：CPU 热点（orchestrator + parse + pricing）
uv run python -m bench.profile_cli --source gw2_prices --once
#   -> 内部用 cProfile.Profile() + pstats.SortKey.CUMULATIVE 打印 top 30
#   -> 输出 profile/<ts>.prof，用 snakeviz 查看（可选）

# 2) 常驻进程： py-spy（无侵入采样，Windows 需在管理员终端或有 SeDebugPrivilege）
#    先拿到 pid：  uv run python -m market_watch serve --pid-file data/run.pid
py-spy top --pid <pid>                       # 实时火焰/函数排行，看是否被 GIL 或 await 卡住
py-spy record --pid <pid> --duration 60 --format flamegraph -o profile/live.svg
py-spy dump --pid <pid>                      # 卡死时抓所有协程栈

# 3) 内存：memray（RSS 增长/泄漏定位，重点看 page buffer 与 LastSeenTracker）
uv run memray run -o profile/mem.bin -- python -m market_watch once --cycles 5
uv run memray flamegraph profile/mem.bin -o profile/mem.svg
uv run memray summary profile/mem.bin

# 4) 慢查询：sqlite3 explain query plan（看板用 EXPLAIN QUERY PLAN 验证走索引）
EXPLAIN QUERY PLAN SELECT ... FROM price_hourly WHERE item_id=? AND bucket_ts>=?;
```

优先优化顺序（不要跳步）：**连接池/keep-alive 是否生效 → `page_size` 是否已用满 200 → 是否命中 change gate（写入行数是否符合预期）→ 是否 N+1（禁止按 item 单条 `UPDATE`，一律 `executemany`）→ 才轮到 JSON 库替换**。

### 10.3 压测必须打本地 mock server（硬性）

> **铁律：`bench/bench_fetch.py` 的 `--base-url` 必须是 loopback（`127.0.0.1` / `localhost` / `::1`）。这不是建议，是代码里的断言，且没有 `--force` 逃生开关。**

理由：对真实官方端点压测，等于用我们自己的 retry/429 处理逻辑去触发对方的限流——既违反 §11 红线（并发的目的是在限额内把吞吐拉满，不是对抗限额），也会污染同一出口 IP 的配额，甚至影响其他使用者的正常访问。**要探索天花板，就用能确定额度的地方。**

```python
# bench/bench_fetch.py
import ipaddress, socket
from urllib.parse import urlparse

LOOPBACK = {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}

def assert_loopback(base_url: str) -> None:
    host = urlparse(base_url).hostname or ""
    addrs = {info[4][0] for info in socket.getaddrinfo(host, None)}
    if not any(ipaddress.ip_address(a) in LOOPBACK or a in {"127.0.0.1", "::1"} for a in addrs):
        raise SystemExit(
            f"压测只允许对本地 mock server 进行：{base_url!r} 非 loopback。"
            " 真实官方端点只允许功能冒烟（单次 --once，且不提高任何限额参数）。"
        )
```

`bench/mock_server.py` 用 FastAPI + uvicorn 在同一进程起，**可注入**：

| 参数 | 用途 |
|---|---|
| `--pages 140 --per-page 200` | 复现实测分页规模 |
| `--latency-ms 5:50` | 延迟分布（基础值/抖动） |
| `--fault 429@0.05,read_timeout@0.02` | 故障注入率，专测 retry/breaker |
| `--max-age 120` | 复现 `Cache-Control` |
| `--ratelimit 600/60` | 模拟服务端限流，验证我们 8 rps 的自限不触发 429 |

**唯一的例外**：功能冒烟允许对真实官方端点发单次请求（`--max-requests 1`），用于验证端点没改版；它复用生产限额参数，不得提高。

---

## 11. 合规边界

### 11.1 红线原文（不得放宽，Phase 2/3 逐条对齐）

> - **禁止**任何键鼠模拟（PyAutoGUI / pydirectinput / OpenCV 屏幕输入层自动化）、禁止客户端内存读取或注入、禁止封包/协议逆向。本仓库**零依赖**这些库。
> - **禁止**绕过登录鉴权、付费墙、验证码；**禁止**使用代理池对抗封禁；**禁止**抓取或转售个人信息。
> - 数据源**仅限官方公开端点**（免鉴权公开 API / 官方公告页 / 官方 RSS），只读，不写任何游戏服务端。
> - 必须遵守 robots.txt 与官方公布的速率限制；**并发的目的是在官方限额内把吞吐拉满，不是对抗限额**。这是设计原则，不是可选建议。

附加说明（本方案新增，同等效力）：

- 原需求中的**多尺度匹配（OpenCV 图像金字塔多分辨率模板匹配）已随红线整体移除**。在本方案中它被替换为 **`domain/consistency.py` 的「多源冗余校验 + 数据一致性校验」**：
  1. **多源冗余**：同一 `item_id` 的两个官方端点互相印证 —— `/v2/commerce/prices` 的 `sells.unit_price` 必须等于 `/v2/commerce/listings` 中 `side='sell'` 的最低 `unit_price`（容忍 ±1 tick 的时序差）；`buys.unit_price` 同理须等于最高买价。
  2. **数据一致性**：`spread >= 0`（best ask ≥ best bid，注意这不是交易所，允许 zero-spread，不允许负 spread）；`unit_price` 为 `>0` 的整数且 ≤ `10**9`；`listings >= 1`；`quantity >= 0`；同一 item 的档位顺序应在 buy 侧 `unit_price` 单调递减、sell 侧单调递增（违反即记为可疑数据）；环比跳变 ≤ `MAX_JUMP_RATIO=10` 倍，否则标记 `suspicious` 而非丢弃；本轮 universe 总数相对上轮波动 ≤ ±5%。
  3. 失败产出 `ConsistencyViolation` → 落 `data_reject` 表 → `severity=warn` 的告警，**同时把该快照隔离：异动检测不得消费**。全部违规规则的命中数在看板「数据质量」卡片可见。

### 11.2 运行期强制检查点（每次进程启动都会执行，失败即 `SystemExit(2)`）

| # | 检查点 | 实现位置 | 失败行为 | 可被配置绕过 |
|---|---|---|---|---|
| C1 | **UA 必须标识自己**，形如 `market-watch/<version> (+<contact_url>)`；启动时校验正则且 `<contact_url>` 不得是占位符 `example.invalid` | `domain/policy.py::validate_user_agent` | 拒绝启动，打印修复指引 | **不可** |
| C2 | **robots.txt 强制**：按 host 抓取并解析（`urllib.robotparser`，缓存 24h 落 `source_state.robots_checked_at`），任一请求 path 未被允许则拒绝发起该请求并把 `robots_allowed=0` 落审计表；404/无 robots.txt → RFC 9309 视为无指令（允许） | `infrastructure/http/robots.py` | 该源本轮终止 + critical 告警 | **不可**。`policy.ROBOTS_MODE` **不存在**；任何豁免必须是代码里的 `AuthorizationRecord` + PR 评审 + `docs/04-compliance.md` 登记 |
| C3 | **速率硬钳制**：`per_host_rps = min(configured, OFFICIAL_LIMIT/60 * HOST_SAFETY_FACTOR)`。`OFFICIAL_LIMIT` 与 `HOST_SAFETY_FACTOR` 是**代码常量**，不在 `.env` 里暴露 | `application/config.py` 的 `@model_validator(mode="after")` | 静默钳制 + WARNING 日志 `event="rate.clamped"` | **不可**（环境变量只能调低，不能调高） |
| C4 | **单一方法出网**：`fetcher` 校验 `method ∈ {"GET","HEAD"}` 且 `content is None`；任何其它 HTTP 方法抛 `ReadOnlyViolation` | `infrastructure/http/fetcher.py` | 抛异常 + critical 告警 | **不可** |
| C5 | **单一代理、无轮换**：`ClientPool` 显式 `trust_env=False`，只接受 `Settings.proxy_url` 一个值；`proxy_url` 不支持逗号列表 | `client_pool.py` | 含 `,` 时拒绝启动 | **不可** |
| C6 | **禁用依赖黑名单**：启动与 CI 各查一次。运行时检查 `sys.modules`，CI 检查 `uv export --format requirements.txt` 的锁文件 | `domain/policy.py::assert_no_forbidden_deps` | 发现即 `SystemExit(2)` / CI fail | **不可** |
| C7 | **压测 Host 白名单**：`bench/bench_fetch.py` 只允许 loopback | `bench/bench_fetch.py::assert_loopback` | `SystemExit`，无 `--force` | **不可** |
| C8 | **不采集个人信息**：`prices`/`listings`/`items`/RSS 四类 payload 均不含任何自然人标识；`parse` 侧加一道反向断言 —— 若某原始字段命中 PII 正则（邮箱/@handle/连续 11 位手机样式数字/经纬度对），直接 reject 该字段 | `domain/policy.py::assert_no_pii` | 字段丢弃 + warn 告警，且记 `data_reject` | **不可** |
| C9 | **robots/UA/效能证据归档**：每个 source 的 robots.txt 原始响应、headers 快照、手写的授权来源说明，必须存入 `docs/04-compliance.md` | Phase 2 落盘 | PR review 卡点 | 不可 |

**C6 黑名单（运行时 + CI 双查）**：

```python
# src/market_watch/domain/policy.py
FORBIDDEN_MODULES: frozenset[str] = frozenset({
    # 键鼠/输入层模拟（红线：禁止）
    "pyautogui", "pydirectinput", "pydirectinput_rs", "pywinauto", "ahk", "autohotkey.py",
    "mouse", "keyboard", "pyinterception", "interception",
    # 屏幕/图像模板匹配（多尺度匹配已移除，本仓无图像处理需求）
    "cv2", "skimage", "scikit-image", "pyautocv",
    # 进程内存读取 / 注入 / 封包
    "pymem", "pymem.exception", "ReadWriteMemory", "ctypes.windll__kernel32", "frida",
    "scapy", "pypacket",
    # 打码平台 / 反爬代理池
    "chaojiying", "ruokuai", "proxy_pool", "rotating_proxies",
})
```

> 注：`cv2` 进黑名单是因为本方案没有任何图像处理需求；若将来要加，必须走 PR 说明为何绕过了这条约束。**默认立场是"没有就是不能出现"。**

### 11.3 `policy-check` CLI（Phase 2 实现，`market_watch policy-check`）

```
$ uv run market-watch policy-check
[OK]  python=3.13.x  platform=Windows-11
[OK]  依赖黑名单: 0 hits (runtime imports + uv.lock)
[OK]  UA: market-watch/0.1.0 (+https://you.example/contact)
[OK]  robots: api.guildwars2.com -> 404 (无指令 -> allow)   www.guildwars2.com -> 404 (无指令 -> allow)
[OK]  有效速率: 8.00 rps (= 官方 600/min × 安全系数 0.80)   声明=8.00  钳制=否
[OK]  出网方法白名单: GET, HEAD
[OK]  代理: 未配置（trust_env=False，环境变量代理被忽略）
[OK]  数据源注册表: gw2_prices/gw2_listings/gw2_items/gw2_news_rss -> 全部标记为 'official_public'
[OK]  压测目标: 仅 loopback
合规检查通过 9/9
```

### 11.4 与 Phase 1 判定的关系

用户原始需求「全自动游戏日常 + PyAutoGUI/OpenCV 键鼠模拟」已由主理人判定命中红线，改为**方案 A：合规版公开数据物价看板**。本架构的每一处设计都不得重新引入该红线：

- **没有**任何图形/输入自动化，因此 `listings` 的深度、`items` 的元数据全部来自官方 REST，不做客户端内的二次复现。
- **没有**任何服务端写入 — `fetcher` 只允许 GET/HEAD（C4）。
- **没有**任何鉴权绕过 — 选的源本来就免鉴权；出现 401/403 的处理是**停机**，不是换 UA / 换 IP（错误矩阵 #14）。
- **没有**无限扩容的并发 — 8 rps 是官方额度 80% 的**硬上限**（C3）。

---

## 12. 依赖清单（下界版本，精确锁定由 `uv.lock` 负责）

> 全部标注「常用稳定版本，以官方发布为准」。

```toml
# pyproject.toml（dependencies 片段，完整文件由 Phase 2 落盘）
[project]
name = "market-watch"
requires-python = ">=3.13"

dependencies = [
    "httpx>=0.28",              # 异步 HTTP；唯一出网客户端
    "pydantic>=2.11",           # 模型与校验（若只需解析可不引，但模型校验收益大于成本）
    "pydantic-settings>=2.10",  # .env -> Settings
    "structlog>=25.1",          # 服务向结构化日志
    "feedparser>=6.0",          # RSS/Atom 通用解析；注意它是阻塞库 -- 在 run_in_executor 里跑
    "uvicorn>=0.34",            # ASGI server（内含标准版即可）
    "fastapi>=0.115",           # 看板 Web 框架
    "jinja2>=3.1",              # 模板
]

[project.optional-dependencies]
ui = ["jinja2>=3.1"]
dev = [
    "ruff>=0.11", "black>=25.1", "mypy>=1.15", "pre-commit>=4.2",
    "pytest>=8.4", "pytest-asyncio>=0.26", "pytest-cov>=6.1",
    "respx>=0.22", "anyio>=4.9",
]
bench = ["orjson>=3.10", "py-spy>=0.4", "memray>=1.17"]

[project.scripts]
market-watch = "market_watch.cli:main"
```

**明确不引入**：`scrapy`（不抓 HTML）、`requests`（一个或两个 HTTP 客户端即可）、`pandas`（近 3 万行的聚合用 Python + SQL 足够，引入 pandas 会让 exe 体积 +40MB 且我们只在 domain 里做简单算术）、`sqlalchemy`（8 张表的 DDL 与查询，原生 SQL 更可控，`%s` → `?` 的方言差异不存在）、`celery/redis/kafka/elasticsearch`（单机看板，引入即负债）、`pymysql/psycopg`（无 Postgres/MySQL）。

**关于 `feedparser` 的说明**：`feedparser` 是阻塞库（blocking），`rss_generic.py` 只在 `NEWS_INTERVAL_S=900` 跑一次、单份 78KB，成本可忽略；用 `asyncio.to_thread()` 包一层即可，不值得为它手写 Atom+RSS+RSS1.0 三套解析。

---

## 13. 交给 Phase 2 的接口/函数签名清单（唯一输入）

> Phase 2 只需按下列签名实现；`domain/*.py` 属于 Phase 1 契约，若发现接口冲突请回给架构（Phase 1），**不要在 crawler 侧改 port**。

```text
# ---- 必须实现的 Protocol ----
domain/ports.py::SourceAdapter              # 6 个成员，见 §7.3
domain/ports.py::RateLimiter                # acquire(host, cost) / penalize(host, factor) / reward(host) / snapshot(host) -> RateState
domain/ports.py::Breaker                    # before(host) -> bool|raise OpenCircuit / record(host, ErrorClass) / state(host) -> str
domain/ports.py::Fetcher                    # async fetch(task: FetchTask) -> tuple[ResponseView, ErrorClass, attempts:int]
domain/ports.py::SnapshotSink               # submit(WriteBatch) -> None / async flush(timeout_s=10.0) -> None
domain/ports.py::AlertSink                  # emit(AlertEvent) -> None

# ---- 必须实现的错误->策略映射（唯一真相源） ----
domain/errors.py::POLICY: Mapping[ErrorClass, AttemptPolicy]       # 18 行，逐条对应 §6 矩阵
infrastructure/http/classification.py::classify_exc(exc) -> ErrorClass
infrastructure/http/classification.py::classify_response(view: ResponseView) -> ErrorClass

# ---- 必须实现的执行链路 ----
infrastructure/http/rate_limiter.py::TokenBucket(capacity, rps) -> RateLimiter impl
infrastructure/http/breaker.py::CircuitBreaker(cfg) -> Breaker impl
infrastructure/http/retry.py::decide(err: ErrorClass, view: ResponseView | None, attempt: int) -> tuple[bool, float]
infrastructure/http/retry.py::async def execute(task, breaker, limiter, pool, sink_metrics) -> FetchResult
infrastructure/http/client_pool.py::ClientPool.get / invalidate / aclose_all
infrastructure/http/fetcher.py::Fetcher.fetch                      # 顺序: robots -> breaker -> limiter.acquire -> request -> classify -> return
infrastructure/http/robots.py::RobotsGuard.allowed(host, path, ua) -> bool
infrastructure/http/lifecycle.py::run_until_stopped(start_callable, *, shutdown_grace_s=30.0) -> int

# ---- 必须实现的适配器 ----
infrastructure/adapters/gw2_prices.py::Gw2PricesAdapter      # kind='price',      paged
infrastructure/adapters/gw2_listings.py::Gw2ListingsAdapter  # kind='orderbook',  watchlist
infrastructure/adapters/gw2_items.py::Gw2ItemsAdapter        # kind='item_meta',  single(bulk ids)
infrastructure/adapters/rss_generic.py::RssAdapter           # kind='announcement', single + If-None-Match

# ---- 编排 ----
application/orchestrator.py::async def run_cycle(ctx: CycleContext) -> CycleReport
application/fanout.py::async def drive(initial_tasks, adapter, fetcher, sink, *, workers: int) -> FanoutReport
application/config.py::Settings  # 含 rate clamp validator + forbidden deps check on validate()

# ---- 存储 ----
infrastructure/storage/writer.py::SqliteWriter(cfg) -> SnapshotSink impl  # 单写线程 + Queue(maxsize=20000)
infrastructure/storage/repositories.py::
    latest_snapshot(item_ids: Sequence[int]) -> list[Row]
    series(item_id: int, table: Literal["price_change","price_hourly"], since: datetime, limit: int = 2000) -> list[Row]
    alerts(limit: int = 50, unack_only: bool = False) -> list[AlertEvent]
    announcements(limit: int = 50) -> list[Announcement]
    search_items(q: str, limit: int = 20) -> list[Row]
    health(cursor_limit: int = 100) -> HealthReport       # 给看板「健康」卡片 + /api/health

# ---- 看板 JSON API ----
GET  /                              -> dashboard.html
GET  /api/summary                   -> {cycle_ts, items_total, alerts_24h, ann_24h, quality_partial}
GET  /api/search?q=                -> [{item_id, name}]
GET  /api/series?item_id=&table=&since=&points=  -> {ts: [...], sell: [...], buy: [...]}   # 服务端降采样 <=2000 点
GET  /api/alerts?limit=&unack=1    -> [AlertEvent]
GET  /api/announcements?limit=     -> [Announcement]
GET  /api/health                   -> {limiter: RateState[], breaker: {...}, db_size_mb, last_cycle}
GET  /api/quality                  -> {rejects_24h, rules: [{rule, hits}], partial_cycles}
```

**Definition of Done（Phase 2 验收，Phase 1 复核）**
1. `uv run market-watch policy-check` → 9/9 通过。
2. `uv run pytest --cov=market_watch --cov-fail-under=80` → 绿。
3. `uv run market-watch once --source gw2_prices --dry-run` → 在**真实官方端点**（real endpoint，单次、不提额）下跑通单轮；输出页数、跳过数、reject 数、耗时。
4. `uv run pytest tests/adapters/test_adapter_contract.py` → 全部 adapter 9 条契约全绿。
5. `uv run python bench/bench_fetch.py --base-url http://127.0.0.1:8765` → 报告 rps/latency/内存； `--base-url` 指向公网时必崩。
6. `docs/04-compliance.md` 已落：每个 source 的 robots 快照（原文 + 抓取时间戳）+ UA 声明 + 授权来源判定（`official_public`）+ 实际观测到的 `X-Rate-Limit-Limit` 存档。

---

## 14. 风险与技术债

| 级 | 风险 | 影响 | 处置 |
|---|---|---|---|
| **P0** | 官方端点改版（字段/分页语义/限流额度变化） | 静默采到脏数据或全量 4xx | `SchemaError` ≥20/轮 → 立即停机；每日 `--once` 冒烟 + Schema 变更断言；`consistency.py` 作为第二道网 |
| **P0** | robots.txt 变卦（今天 404 → 明天 Disallow） | 继续抓即违规 | robots 24h 重取；一旦从「允许」变「禁止」，该源立即停用并 critical 告警。**新 source 上线前必须人工复核 robots 原文** |
| **P0** | 配额超限、被服务端临时封 IP | 失去数据源 | 处置方式是逐级 Step-Down：8 rps → 4 → 1 → 停；**禁止**换出口 IP。被封即停机，等官方限定期过，不改 UA / 不上代理 |
| **P1** | SQLite 写入跟不上（28k upsert/周期） | 队列积压 → 内存上涨 → 周期重叠 | 队列 `maxsize=20000` 触发背压（此时宁可丢一轮变更也不能拖垮进程）；`scheduler` 检测到上一轮未结束则**跳过**本轮并记录 `cycle.skipped` |
| **P1** | `X-Rate-Limit-Remaining` 头缺失 → 只能盲自限 | 无法感知对端剩余额度 | 保守自限 80% + AIMD + 429 立刻减半；Phase 2 若发现该头出现（官方可能变动），打开 `RATELIMIT_HEADER_PROBE` |
| **P1** | 时钟/时区污染 | `observed_at` 乱序导致 K 线错乱 | 全部 `datetime` timezone-aware UTC，落库 ISO8601 `Z`；`price_latest` upsert 带 `WHERE excluded.observed_at >= ...` 防回写；CI 加一条禁止 `datetime.now()` 无 tz 的 ruff 规则（`DTZ005`） |
| **P1** | Windows 优雅退出不彻底 | data 目录 SQLite-WAL 残留、下次启动慢 | §4.6 的固定退出序列 + 关库前 `PRAGMA wal_checkpoint(TRUNCATE)` |
| **P2** | `feedparser` 阻塞 + 体积 | 单轮多几十 ms；exe 体积 +2MB | 用 `asyncio.to_thread()` 隔离；可接受 |
| **P2** | 数据量增长到留不住 | DB 膨胀 | 第一刀砍 `TOPN_HOURLY`（2000→500），第二刀砍 `price_change` 保留期（72h→24h），第三刀才考虑导出 Parquet 冷存 |
| **P2** | `STRICT` 表带来的迁移脆弱性 | 未来加列必须新开 migration | 迁移用 `000N_*.sql` 递增 + `meta.schema_version` 记录已应用版本；**禁止**修改已应用的迁移文件 |
| **P2** | 看板无写权限隔离（本地信任模型） | 同一局域网的其他人能看/改 | 默认 bind `127.0.0.1:8000`；`serve --host 0.0.0.0` 时必须打 WARNING 并要求二次确认；不引鉴权，Phase 3 若需远程访问再议 |

---

## 15. 附：Agenda — 需要主理人拍板的三件事

1. **数据源替换确认**：ESI → GW2 v2 API（证据 §1.2 / §1.4）。若坚持保留 ESI 适配器，则必须先有运营方书面授权，否则它与 §11.2-C2 直接冲突。
2. **`(rps, burst, concurrency) = (8, 12, 4)` 是合规换算出来的上限起点**，不是性能调优结果。若 Phase 2 实测后觉得慢，唯一合法的提速路径是「用满 `page_size=200` 减少请求数」，而不是加 rps。请确认这条约束不被性能诉求压垮。
3. **`cv2` 进黑名单**（§11.2-C6）。它本身不是红线库，但本方案没有任何图像处理需求；把它视为"没有就是不能出现"可以杜绝 Phase 3 有人顺手加回来。请确认接受。
