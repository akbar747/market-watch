# 密钥扫描报告（仓库级）

| 项 | 值 |
|---|---|
| 文档编号 | `docs/04-secrets-scan.md` |
| 扫描时间 | 2026-10-03（Phase 3a 发布准备） |
| 扫描范围 | `src/**`、`config/**`、`bench/**`、`scripts/**`、`tests/**`、`.github/**`、`docs/**`、`config/*.yaml`、`pyproject.toml`、`README.md`、`.env.example`、新增的 `release/**` |
| 执行方式 | **人工正则扫描**（本环境无法下载 gitleaks 二进制 —— GitHub Release 下载被证书吊销检查拦截：`schannel: CRYPT_E_NO_REVOCATION_CHECK`），因此下面是正则扫描的结论；CI 里那一步仍会跑真正的 gitleaks 二进制，与本文件互为双保险 |
| 扫描方法 | ① 关键词/正则全仓文本扫描（`api_key` / `apikey` / `token` / `password` / `passwd` / `secret` / `authorization` / `credential` / `cookie` / `session` / `Bearer` / `Basic` / `AKIA` / `private_key` / `BEGIN RSA|PRIVATE KEY` / `proxy_url` / `x-api-key` / `ghp_`, `sk-`, `xoxb-`, `eyJ…` / 长度 ≥32 的高熵字符串）② 与 `.github/gitleaks.toml` 同规则的补充扫描 |
| 结论 | **未发现任何真实硬编码密钥。** 全部命中均为变量名、脱敏白名单、文档样例或 CDN 资源里的 CSS 类名 |
| 自动化 | CI 每次 push/PR 跑 `gitleaks dir . --config .github/gitleaks.toml --exit-code 1`；本文件是那次自动扫描的人工复核记录 |

---

## 1. 命中清单（逐条给结论）

| # | 文件:行 | 命中内容 | 判定 | 处置 |
|---|---|---|---|---|
| 1 | `src/market_watch/logging_setup.py:31-41` | `"authorization"`、`"proxy_url"`、`"password"`、`"token"`、`"api_key"`、`"apikey"`、`"secret"`、`"cookie"`、`"set-cookie"`、`"x-api-key"`、`"mw_proxy_url"` | **假阳性**：这是 structlog 的**脱敏字段名白名单**（`_SENSITIVE_KEYS`），作用是把这些名字的值在日志里替换成 `[REDACTED]`。恰好是"密钥相关"的标识符清单，本身不含任何密钥 | 保留；这是安全特性不是风险 |
| 2 | `src/market_watch/logging_setup.py:56-66` | `secret_values` / `if secret in msg` / `msg.replace(secret, _SCRUBBED)` | **假阳性**：`_SCRUB_ENV_VALUES` 读环境变量值并做字符串替换，变量名叫 secret | 保留 |
| 3 | `src/market_watch/domain/models.py:148-151` | `token = part.strip().lower()` / `if token.startswith("max-age=")` | **假阳性**：解析 `Cache-Control` 头时把分片叫 `token`（Padding Oracle 那个 `token` 的巧合重名） | 保留（可读性好，无风险） |
| 4 | `src/market_watch/domain/policy.py:141-154` | `assert_single_proxy(proxy_url)` 内含 `"C5 失败：proxy_url 含逗号（代理池）…"` | **假阳性**：C5 的实现与报错文案 | 保留 |
| 5 | `src/market_watch/application/config.py:128,157-158` | `proxy_url: str \| None = None` / `policy.assert_single_proxy(self.proxy_url)` | **假阳性**：Settings 字段名与校验调用 | 保留 |
| 6 | `src/market_watch/interfaces/cli.py:157-163` | `settings.proxy_url` / 输出 `f"代理: 单一静态代理已配置（{'含口令已脱敏' if '@' in …}）"` | **假阳性**：policy-check 第 7 项，仅检测字符串中是否含 `@` 来决定提示文案，**不打印口令** | 保留（这正是"脱敏打印"的设计） |
| 7 | `src/market_watch/infrastructure/http/client_pool.py:94,108` | `"_proxy_url"`、`proxy_url: str \| None = None` | **假阳性**：字段名 | 保留 |
| 8 | `src/market_watch/interfaces/wiring.py:124` | `proxy_url=settings.proxy_url` | **假阳性**：构造参数传递 | 保留 |
| 9 | `config/sources.yaml:22,39,57,73` 与 `README.md:247` | `authorization: official_public` | **假阳性**：数据源授权等级字段（取值只有 `official_public` 等少数几档），不是 HTTP 鉴权 | 保留 |
| 10 | `.github/workflows/release.yml:91` | `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}` | **假阳性**：引用 GitHub Actions 自动注入的临时 token，是**引用不是字面量** | 保留（如需推 PyPI 应换成 OIDC，勿把 token 写进 workflow） |
| 11 | `.env.example:93` | `MW_PROXY_URL=`（空值） | **假阳性**：示例文件里的空值占位，真实值只能来自环境变量；`_env.example` 是本仓唯一允许提交的 `.env*` | 保留 |
| 12 | `docs/_robots_snapshot.json:18` | 3 处长度 ≥32 的 `[\w-]{32,}` 字符串 | **假阳性**：官方 404 页面 HTML 里的 CSS 媒体查询类名（`hover-hover-and-pointer-fine-and-min-width-1024px-css` 等） | 保留；此文件是 C2 合规证据（robots 404 快照），必须提交 |
| 13 | `.github/gitleaks.toml:25-47` 与 `.github/workflows/ci.yml:33,89` | `MW_PROXY_URL=$`、`Bearer <token>`、`GITLEAKS_LICENSE` 提及 | **假阳性**：扫描配置与 workflow 自身 | 保留 |

**未命中的高危模式**：`AKIA[0-9A-Z]{16}`、`ghp_/gho_/ghu_/ghs_/ghr_[A-Za-z0-9]{20,}`、`sk-[A-Za-z0-9]{20,}`、`xox[baprs]-…`、`eyJ[A-Za-z0-9_-]{20,}`（JWT）、`-----BEGIN … PRIVATE KEY-----`、`-----BEGIN RSA PRIVATE KEY-----`、`-----BEGIN OPENSSH PRIVATE KEY-----`、`postgresql://user:pass@host`、`https://user:pass@host/path`、`AIza…`、`.env` 文件中的 `KEY=value` 字面量 —— **全部零命中**。

## 2. 运行期数据目录的复核

| 路径 | 状态 |
|---|---|
| `.env` / `.env.local` | **仓库内不存在**（`.gitignore` 已忽略 `.env` 与 `.env.*`，仅 `!.env.example` 放行；本仓 `.env.example` 全部为示例值） |
| `data/market_watch.db` 及 `-wal`/`-shm` | 运行期产物，已被 `.gitignore` 忽略（`data/*` + `!data/.gitkeep`）；未纳入任何提交 |
| `data/rejects/`、`data/pending/` | 落库/拒收缓冲，同属运行期产物，已忽略 |
| `docs/_robots_snapshot.json` | 官方 robots 响应快照（无指令 → 404），已提交，不含凭据 |

## 3. 结论

1. 当前仓库**不存在任何需要轮换的硬编码密钥**——没有可轮换的东西，也就没有"必须立刻改"的项。
2. 设计上密钥**无处可硬编码**：本项目对接的是免鉴权官方公开接口（无 API Key、无 OAuth），唯一可能带凭据的入口是 `MW_PROXY_URL`（代理口令），而 `logging_setup._SCRUBBED` 已把 `proxy_url` / `password` / `authorization` 列为脱敏键，`policy-check` 第 7 项只提示"含口令已脱敏"，不回显口令。
3. 提交前的最后一道闸在 CI：`gitleaks dir . --redact --exit-code 1`（配置见 `.github/gitleaks.toml`，含两条自定义规则专门盯 `http(s)://user:pass@host` 与 `Bearer/Basic <16+ 字符>`）。

## 4. 给后续贡献者的硬规则（写入 PR 评审清单）

- 任何新凭证必须来自环境变量或 CI secrets，**不得**进 `.env.example` 的示例值以外的任何文件。
- 新增 workflow 若要推 PyPI，使用 **OIDC trusted publishing**（`id-token: write`），禁止明文 `PYPI_TOKEN`。
- 首次对外发布前再跑一次 `gitleaks dir . --redact`，并把结果续写进本文件。
