# README 素材片段集合

> 本文**不是散文**，是一组可直接粘贴进 `README.md` 的 Markdown 片段。每个片段用标题 + 围栏代码块包裹，标题里的「粘贴位置」说明该插在 README 的哪一段。
>
> 用途：README 由发布阶段组装，本文提供**可复用素材**与**合规口径的唯一版本**（尤其是合规声明与免责声明，任何组装都不得弱化）。
>
> 素材口径以 `src/market_watch/**` 与 `docs/01-architecture.md` 为准；核对时间：本文件落盘时 `211 passed / 44%` 基线下取数。

---

## 1. 徽标组（badges）

**粘贴位置**：README 主标题 `# market-watch` 之后，紧随一行 hero 定位语；放在 `---` 分隔线之前。

```markdown
![python](https://img.shields.io/badge/python-3.13%2B-blue?style=flat-square&logo=python&logoColor=white)
![license](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![compliance](https://img.shields.io/badge/compliance-9%2F9%20(C1%E2%80%93C9)-brightgreen?style=flat-square)
![rate-limit](https://img.shields.io/badge/rate_limit-8.0_rps_on_600%2Fmin-lightgreen?style=flat-square)
![sqlite](https://img.shields.io/badge/storage-SQLite_WAL-blueviolet?style=flat-square)
![offline](https://img.shields.io/badge/dashboard-ECharts_vendored-lightgrey?style=flat-square)
```

**说明**：除 CI 构建徽标外，其余均为静态 `shields.io` URL，仓库不存在也能渲染。CI 徽标的 `OWNER` 占位在上线前必须替换。

---

## 2. Hero 区（一句话定位 + 三句核心能力）

**粘贴位置**：标题与徽标之间或紧接其后；「这是什么」章节正文即可整体复用本片段。

```markdown
> 本地行情看板 + 异动提醒。**只**读取官方公开 v2 API 与官方公告 RSS，只读、免鉴权、自限速率；本地 SQLite 落库，看板离线可用。

一个**跑在你自己机器上的信息监控与提醒工具**：轮询官方市场的价格行情与官方公告 RSS，算出 best bid / best ask / spread / VWAP 与盘口水深，用 z-score 与环比阈值判定价格异动，把结果本地落进 SQLite，再在浏览器里给你一张自己的看板 + 一份本地告警记录。

三句话讲清边界：

1. **数据源只有两个**：官方 REST 与官方公告 RSS。没有第三方镜像、没有聚合黑盒、没有爬虫、没有登录墙。
2. **只出不进**：出网方法白名单只有 `GET` / `HEAD` 且不带 body（`C4`），对官方服务端没有任何写入。
3. **速率是算出来的，不是调出来的**：官方实测 `X-Rate-Limit-Limit: 600`（每 IP 每分钟），乘安全系数 `0.8` → **硬上限 8.0 rps**，validator 钳制，环境变量只能调低不能调高（`C3`）。
```

---

## 3. 功能特性（8 行表）

**粘贴位置**：`## 特性` 章节；README 现有「特性」若为条目列表，可直接整体替换为下表，或下表作为首张表。

| # | 特性 | 落地位置 |
|---|---|---|
| 1 | **多源冗余校验**：`prices` 与 `listings` 两端点互证，`spread >= 0` / `unit_price > 0` / 档位单调性三项门控；违规进 `data_reject` 表并**隔离快照**，异动检测不得消费 | `domain/consistency.py`、`domain/pricing.py` |
| 2 | **价格异动告警**：z-score + 环比阈值双路判定，按严重级分流；每条告警带 `dedupe_key` 与冷却窗，冷却期内不重复打扰 | `domain/anomaly.py`、`application/alert_service.py` |
| 3 | **断线重连与连接池重建**：根据 `Retry-After` / `Connection: close` 主动失效并重建 httpx 连接池，重建后回调限流器降权再渐进恢复 | `infrastructure/http/client_pool.py`、`interfaces/wiring.py` |
| 4 | **指数退避 + 全抖动重试**：`base=1.0 / factor=2.0 / max=30s`，每次抖动取 `[0, backoff]` 均匀随机，累计时间预算封顶，预算耗尽即放弃而非无限重试 | `infrastructure/http/retry.py` |
| 5 | **熔断与半开探针**：每主机 closed / open / half-open 状态机，open 到期放单个探针，探针成功才整体恢复（探针前必须先刷新状态，否则电路只能等进程重启） | `infrastructure/http/breaker.py`、`domain/ports.py` |
| 6 | **增量去重与分层归档**：`dedupe_key` 幂等落库，hour bucket 分层老化 + `PRAGMA optimize` 的 `gc`，历史数据按保留策略滚动清理 | `application/dedupe.py`、`domain/retention.py` |
| 7 | **合规 9 检查点**：`market-watch policy-check` 一次跑完 C1–C9，任一不过**退出码 2**，CI 里就是硬 gate；关键项不可被配置绕过 | `domain/policy.py`、`application/rates.py` |
| 8 | **本地看板**：FastAPI + Jinja2 单页 + **vendored ECharts**（离线可用、无 CDN），服务端降采样 ≤2000 点，不依赖任何外部字体/脚本 | `interfaces/web/` |

---

## 4. 架构图（Mermaid `graph TD`）

**粘贴位置**：建议放在 `## 这是什么` 末尾或独立 `## 架构` 章节开头。

```mermaid
graph TD
    subgraph IF["interfaces（外层：只做 I/O 与展示）"]
        CLI["cli.py<br/>六子命令"]
        WEB["web/<br/>FastAPI + vendored ECharts"]
    end

    subgraph AP["application（用例编排）"]
        ORCH["orchestrator.py"]
        CFG["config.py<br/>Settings 硬钳制"]
        RATES["rates.py<br/>缓存友好的轮询间隔"]
        SCHED["scheduler.py / fanout.py"]
        ING["price_ingest.py / news_ingest.py"]
        DED["dedupe.py"]
        ALERT["alert_service.py"]
    end

    subgraph DM["domain（纯业务：禁 import httpx/sqlite3/requests/fastapi）"]
        MODELS["models.py / errors.py"]
        POLICY["policy.py<br/>C1 UA · C6 黑名单元表<br/>C8 PII 正则 · C9 证据"]
        PRICING["pricing.py<br/>mid/vwap/depth/bucket"]
        CONS["consistency.py<br/>互证 · 单调性"]
        ANOM["anomaly.py<br/>z-score · 环比 · 冷却"]
        RET["retention.py<br/>分桶归档"]
        PORTS["ports.py<br/>RateLimiter / Breaker /<br/>SourceAdapter / AlertSink"]
    end

    subgraph INF["infrastructure（外层：io 实现，可 import domain，禁 import application）"]
        HTTP["http/<br/>client_pool · fetcher · rate_limiter<br/>breaker · retry · classification · robots"]
        ADAPTERS["adapters/<br/>gw2_prices · gw2_listings · gw2_items · rss_generic"]
        STORE["storage/<br/>sqlite · writer · repositories · maintenance"]
        ALERTSINK["alerting/<br/>sqlite_sink · stdout_sink"]
    end

    CLI --> ORCH
    WEB --> ALERT
    CLI --> POLICY

    ORCH --> ING
    ORCH --> SCHED
    ORCH --> ALERT
    ING --> DED
    SCHED --> RATES
    ALERT --> ANOM

    ING --> PRICING
    ANOM --> PRICING
    PRICING --> CONS
    DED --> RET
    CFG --> RATES

    PORTS -.乐观注入-.> HTTP
    PORTS -.乐观注入-.> STORE
    PORTS -.乐观注入-.> ALERTSINK
    HTTP --> ADAPTERS
    ADAPTERS -.返回 ResponseView.> PORTS
    ORCH --> PORTS

    POLICY ==C1 UA 标识==> HTTP
    POLICY ==C2 robots 强制==> HTTP
    CFG ==C3 速率硬钳制==> RATES
    HTTP ==C4 只 GET/HEAD==> ADAPTERS
    HTTP ==C5 单一静态代理==> CLI
    POLICY ==C6 依赖黑名单==> CFG
    HTTP ==C7 压测仅 loopback==> SCHED
    POLICY ==C8 PII 反向断言==> ADAPTERS
    HTTP ==C9 证据归档==> STORE

    classDef l1 fill:#e8f0fe,stroke:#4285f4,color:#000;
    classDef l2 fill:#fef7e0,stroke:#f9ab00,color:#000;
    classDef l3 fill:#e6f4ea,stroke:#34a853,color:#000;
    classDef l4 fill:#fce8e6,stroke:#ea4335,color:#000;
    class IF,CLI,WEB l1;
    class AP,ORCH,CFG,RATES,SCHED,ING,DED,ALERT l2;
    class DM,MODELS,POLICY,PRICING,CONS,ANOM,RET,PORTS l3;
    class INF,HTTP,ADAPTERS,STORE,ALERTSINK l4;
```

**读图要点**（写进图下方一句话）：

```markdown
依赖方向是 `interfaces → application → domain ← infrastructure`：orchestrator 只依赖 `domain/ports` 里的 Protocol，infrastructure 的实现由装配层反向注入；因此 domain 一行 I/O 库都不 import，也就不需要为网络/数据库准备任何 mock。
```

---

## 5. 快速上手（uv / pip 两条路径）

**粘贴位置**：`## 安装` 章节内，作为 uv 与 pip 两个子段的命令块。

```bash
# 路径一：uv（推荐，锁版本、一步装齐工具链）
git clone https://github.com/OWNER/market-watch.git
cd market-watch
uv sync --extra dev          # 含 ruff / black / mypy / pytest
uv run market-watch policy-check   # 应输出 9/9；任一不过则退出码 2
uv run market-watch once           # 跑一轮真实行情（唯一会出网的命令）
uv run market-watch serve          # 起看板 → http://127.0.0.1:8000
```

```bash
# 路径二：pip（不引入 uv 也能跑）
git clone https://github.com/OWNER/market-watch.git
cd market-watch
python -m venv .venv
# Windows:  .venv\Scripts\python -m pip install -e ".[dev]"
# Linux/macOS: .venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pip install .            # 只要运行时
python -m market_watch policy-check
python -m market_watch once
python -m market_watch serve                 # → http://127.0.0.1:8000
```

> [!IMPORTANT]
> 两种方式都要求 **Python ≥ 3.13**（`requires-python = ">=3.13"`）。首次运行前必须先做一次合规自检 `policy-check`，不通过不要启动采集。

---

## 6. src-layout 目录结构

**粘贴位置**：`## 目录结构` 章节，代码块语言用 `text`。

```text
market-watch/
├── pyproject.toml                 # 依赖 / 构建 / 工具链（唯一真相源，无 setup.py、无 requirements.txt）
├── LICENSE                        # MIT
├── .env.example                   # 全部可配置项，必须提交
├── config/
│   ├── sources.yaml               # 数据源清单 + 礼貌参数 + 否决源登记表
│   └── alerts.yaml                # 异动规则（阈值/窗口/冷却/严重级）
├── src/market_watch/
│   ├── domain/                    # 纯业务层：禁 import httpx/sqlite3/requests/fastapi/pandas
│   │   ├── models.py  ports.py  errors.py  policy.py
│   │   ├── pricing.py  consistency.py  anomaly.py  retention.py
│   ├── application/               # 用例编排
│   │   ├── config.py  rates.py  scheduler.py  orchestrator.py  fanout.py
│   │   ├── price_ingest.py  news_ingest.py  dedupe.py  alert_service.py
│   ├── infrastructure/
│   │   ├── http/                  # client_pool / fetcher / rate_limiter / breaker / retry
│   │   │                          # classification / robots / lifecycle / metrics
│   │   ├── adapters/              # gw2_prices / gw2_listings / gw2_items / rss_generic + fixtures
│   │   ├── storage/               # sqlite(migrations) / writer / repositories / maintenance
│   │   └── alerting/              # sqlite_sink / stdout_sink
│   └── interfaces/
│       ├── cli.py                 # 六子命令（policy-check / once / serve / gc / doctor / ...）
│       └── web/                   # app / routes / deps / static(vendor) / templates
├── examples/                      # 离线可跑的最小示例（--off 回放 fixtures，不出网）
├── tests/                         # unit / adapters / integration（三层，不依赖网络）
├── bench/                         # bench_fetch.py（仅 loopback）
├── release/                       # PyInstaller 打包：spec / 入口 / build_exe.py
├── docs/                          # 01-architecture.md / 03-readme-blocks.md / 04-secrets-scan.md
└── data/                          # 运行期数据（gitignore，仅 .gitkeep）
```

---

## 7. 限速与礼貌参数表

**粘贴位置**：`## 限速与礼貌策略` 章节首表。

| 项 | 值 | 从哪来 | 可配置 |
|---|---|---|---|
| 官方速率上限 | **600 req/min**（`X-Rate-Limit-Limit` 实测） | 官方响应头 → `domain/policy.py::OFFICIAL_RATE_LIMIT_PER_MIN` | **不可**（代码常量） |
| 安全系数 | **0.8** | `domain/policy.py::HOST_SAFETY_FACTOR` | **不可**（代码常量） |
| 自限上限 | **8.0 rps**（= 600÷60×0.8） | `EFFECTIVE_RPS_CAP`，`application/config.py` validator 硬钳制 | **不可**（只能调低） |
| 单主机并发 | **4** | `per_host_concurrency`（`Field(ge=1, le=8)`） | 可调低 |
| 全局在飞 | **16** | `GLOBAL_INFLIGHT` | 可调低 |
| 令牌突发容量 | **12** | `per_host_burst`（`Field(ge=1, le=64)`） | 可调低 |
| 页大小 | **200** | 官方 `X-Page-Size` 上限（`Field(le=200)`） | 只能调小 |
| min 间隔 | **120 s**（跟随 `Cache-Control: max-age`，listings 实测 1200 s） | `application/rates.py` | 配小了只会被抬上去并打 `interval.clamped` |
| 压测目标 | **仅 loopback**，无 `--force` | `bench/bench_fetch.py::assert_loopback` | **不可** |

```markdown
**想提速就少发请求**（用满 `page_size=200`、关掉不需要的源、只观测清单内的物品），**不要提 rps**——那是对抗限额。
```

> [!WARNING]
> `per_host_rps` 配高了不会被采纳、只会静默/告警降级到 cap；`OFFICIAL_RATE_LIMIT_PER_MIN`、`HOST_SAFETY_FACTOR`、`EFFECTIVE_RPS_CAP` 是代码常量，`.env` 与 `config/*.yaml` **没有任何字段能覆盖它们**。

---

## 8. 合规检查点 C1–C9

**粘贴位置**：`## 合规检查点 C1–C9` 章节。

| # | 检查点 | 实现 | 可配置绕过 |
|---|---|---|---|
| C1 | UA 自我标识 `market-watch/<ver> (+<contact>)`，占位域拒绝启动 | `domain/policy.py::validate_user_agent` | **不可** |
| C2 | robots.txt 强制（404/无指令按 RFC 9309 视为允许），无开关 | `infrastructure/http/robots.py` | **不可**（豁免需书面授权 + PR 评审） |
| C3 | 速率硬钳制 8.0 rps | `application/config.py` validator | **不可**（env 只能调低） |
| C4 | 只出 `GET`/`HEAD`、无 body | `infrastructure/http/fetcher.py` | **不可** |
| C5 | 单一静态代理、`trust_env=False`、禁代理池 | `infrastructure/http/client_pool.py` | **不可** |
| C6 | 依赖黑名单（运行时 + CI 双查） | `domain/policy.py::assert_no_forbidden_deps` | **不可** |
| C7 | 压测只打 loopback，无 `--force` | `bench/bench_fetch.py::assert_loopback` | **不可** |
| C8 | 不采集个人信息（PII 正则反向断言） | `domain/policy.py::assert_no_pii` | **不可** |
| C9 | robots / UA / 授权证据归档 | `docs/_robots_snapshot.json` + 架构 §11 | 不可 |

---

## 9. 合规声明与免责声明骨架

**粘贴位置**：`## 合规声明与免责声明` 章节。**这一节不得删减、不得弱化为"仅供参考"措辞。**

### 9.1 三条铁律

```markdown
### 三条铁律（项目设计的前提，不是可选建议）

| # | 铁律 | 落地方式 |
|---|---|---|
| 1 | **绝不触碰游戏客户端** | 无内存读写/修改、无注入、无键鼠模拟、无破解、无脱机挂。依赖黑名单（`pyautogui`/`pydirectinput`/`cv2`/`pymem`/`frida`/`proxy_pool`…）在运行时 `sys.modules` 与 CI 双查（`C6`） |
| 2 | **仅对接官方公开 API** | 数据源只有官方 v2 API 与官方公告 RSS，且严格遵守官方公布的速率限制；实测 robots.txt 返回 404 → 无指令 → 按 RFC 9309 视为允许；相关接口免鉴权；遵循 `Cache-Control: public, max-age=120` |
| 3 | **定位为信息监控与提醒工具** | 纯个人辅助，推送只到本地（看板 / stdout / SQLite sink），不涉及任何盈利、不涉及代练、不涉及任何第三方服务端施压 |
```

### 9.2 免责声明（逐条保留，不得合并成一句）

```markdown
### 免责声明

- 本项目**仅**适用于个人对**官方公开接口**的读取。任何把本项目采集到的数据用于**商业转售、分发、转授第三方**的行为都不在授权范围内（`LICENSE` 的 MIT 只覆盖代码本身，不覆盖数据的再分发权）。
- 本项目**不得**用于绕过任何鉴权、付费墙、验证码或访问控制；遇到 401/403 的处理是**停机等待人工**，而不是换 UA、换 IP 或换代理。
- 使用者需**自行遵守目标服务方的使用条款**。官方接口可能随时变更限额、加鉴权或封禁出口 IP；本项目不对因此产生的封禁、限流、数据缺失负责。
- 本项目**不包含**、也**不得**被用于任何游戏客户端自动化（键鼠模拟、内存修改、协议逆向、脱机挂）。任何违反这点的二次使用与本项目无关。
- 本项目按 **"AS IS"** 提供，无任何担保；决策请以你看板上的数据为准，工具本身不构成任何投资建议。
```

### 9.3 必须保留的 GFM 告警块

> [!IMPORTANT]
> 本项目**不包含**游戏客户端自动化、不含内存修改、不含键鼠/点击模拟、不绕过鉴权；它只是一个读写自己机器上 SQLite 的本地监控工具。

> [!WARNING]
> 本工具采集到的数据**不得**用于商业转售或转授第三方；本项目为非营利个人工具，非代练、非代打、无任何盈利用途。

---

## 10. 离线示例（可选补充段）

**粘贴位置**：`## 五分钟上手` 之后，独立小段；三个示例均可在无网络环境跑通。

````markdown
### 想先看行为再装环境？跑离线示例

```bash
# C1/C3/C4/C5/C6/C8 合规纯逻辑自检（零出网）
python examples/smoke_policy_check.py          # → 9/9 通过

# 拉单个商品的价格盘口（--off 回放 fixtures，零出网）
python examples/fetch_one_item.py --off        # → best bid / ask / spread / mid / depth
python examples/fetch_one_item.py              # → 同样打到官方端点（唯一出网模式）

# 本地告警推 Webhook（默认只打印 payload，占位域不做真实请求）
python examples/alert_webhook.py               # → 输出 payload，sent=false
python examples/alert_webhook.py --send        # → 才会真的 POST 到 --webhook
```
````

> [!NOTE]
> `examples/fetch_one_item.py`（不带 `--off`）与 `market-watch once` 是**唯一会产生出网流量**的入口；示例默认关闭真实请求时都会显式打印 `sent : 否`。

---

## README 采用情况（落盘时点核对）

**核对对象**：`README.md`（落盘时 410 行 / 22 个标题）。

### 已合入 README 的片段

| 片段 | 对应 README 位置 | 状态 |
|---|---|---|
| 1 徽标组 | `README.md:5-10`（6 枚 shields.io 徽标） | **已合入**（口味相近；本片段多一枚 `offline/vendored ECharts` 徽标可补） |
| 2 Hero 区 | `README.md:3` 定位语 + `:36-46`「这是什么」三句边界 | **已合入**（措辞为发布顾问重写版，事实口径一致） |
| 3 功能特性 8 行表 | `README.md:78-86`「特性」 | **已合入**（README 用条目列表而非表格；8 个特性点一一对应，本片段的「落地位置」列尚未进 README） |
| 5 快速上手 | `README.md:94-112` uv / pip / Windows 三路 | **已合入**（README 多一路 Windows 发布包安装，本片段未覆盖） |
| 6 目录结构 | `README.md:326-359` 代码块 | **已合入**，但**不完整**：README 版本**缺** `examples/` 行，且 `docs/` 行写的是 `01-architecture.md / 04-secrets-scan.md / DEMO_RECORDING.md`（本片段补了 `examples/` 行与 `docs/03-readme-blocks.md` 条目） |
| 7 限速与礼貌表 | `README.md:286-304` | **已合入**（口径一致；README 更细，多出连接池/AIMD/重试/方法/代理四行） |
| 8 C1–C9 表 | `README.md:308-322` | **已合入**（逐行一致） |
| 9 合规声明骨架 | `README.md:58-74` 三条铁律 + 五条免责 | **已合入**（事实与措辞对等，未弱化） |

### 尚未进入 README 的片段（README 现有版本没有）

| 片段 | 缺口 | 建议处置 |
|---|---|---|
| **4 Mermaid 架构图** | README**完全没有**架构图，只有一行文字说明依赖方向（`:361`）。Mermaid 会在 GitHub 上直接渲染成图，对新读者价值最高 | 建议补进 `## 这是什么` 末尾或新开 `## 架构`。已在本文件备好可粘贴图 |
| **9.3 两条 GFM 告警块** | README**一个** `> [!WARNING]` / `> [!IMPORTANT]` 都没有，合规提示全靠普通加粗 | 建议至少把 9.3 的两块放进 `## 合规声明与免责声明`，GitHub 上会以黄/蓝底高亮渲染 |
| **7 的 > [!WARNING] 速率块** | 同上，README 用普通段落表达"不可调高" | 可选：补在限速表下方 |
| **10 离线示例段** | README 无 `examples/` 使用说明 | 可选：补在「五分钟上手」后 |
| **1 的 offline 徽标** | README 少这一枚 | 可选补 |
| **3 的「落地位置」列** | README 特性列表未标注实现位置 | 可选：补成表格 |

### 结论

**选 A（补上）**，理由（不是"为了交差"，是素材确实被需要）：

1. README 缺**架构图**——这是读者理解「为什么 domain 一行 I/O 都不 import」的最短路径，而现有 README 只有一行文字带过。
2. README 缺**全部 GFM 告警块**——合规是本项目最重要的卖点/风险点，GitHub 渲染下 `> [!IMPORTANT]` / `> [!WARNING]` 的视觉权重远高于加粗段落；缺了等于把最强的合规声明降级成普通文字。
3. 发布阶段（README）没有反向引用任何文档，若我不落盘，这些素材在发布时点后就没有可复用版本，后续维护口径会散。

同时说明：**已合入部分占 8/10 章节**，本文件不是"无人用的重复文档"，而是 README 的**底稿 + 尚未采用素材的容器**；未采用的 6 项均为"可选增强"，README 现有版本不采纳也不影响正确性。
