# v0.1.0 发布手册

本文件是 **v0.1.0 的发布操作手册**：tag 命令序列 / 发布检查清单 / 回滚方案。
**执行者：team-lead 或用户。本文只准备命令，不代执行。**

---

## 0. 前提：tag 的触发方式

`.github/workflows/release.yml` 的触发条件：

```yaml
# .github/workflows/release.yml
on:
  push:
    tags: ["v*"]
  workflow_dispatch:
```

**关键**：它监听的是 `push` 事件里的 tag，**不是本地 tag**。也就是说——

> 本地 `git tag -a v0.1.0` 打完之后，**只要不 push，Release 工作流一次都不会跑**，
> 不会有 Windows exe 构建、不会有 GitHub Release、不会有 `market-watch.zip` 产物。
> 这是本次「打 tag、暂不推远端」的**预期结果**，不是故障。需要出产物时再 push 或走 `workflow_dispatch`。

---

## 1. tag 命令序列（可直接粘贴）

```bash
# ---- 步骤 0：确认起点干净（应无输出；有输出先处理完再往下）----
git status --short

# ---- 步骤 1：把待发布内容入库（若尚未提交）----
git add README.md CHANGELOG.md docs/RELEASE.md docs/demo.gif
git commit -m "docs(release): v0.1.0 发布物料（CHANGELOG / 发布手册 / demo GIF）与 README 收口"

# ---- 步骤 2：确认要打 tag 的那个 commit 就是门禁全绿的那个 ----
git log --oneline -1
git show --stat HEAD

# ---- 步骤 3：打 annotated tag（别用 lightweight：Release 需要 tagger 信息与 tag message）----
git tag -a v0.1.0 -m "v0.1.0 —— 首个可用版本：合规 9/9、Windows onedir 发布包、离线看板"

# ---- 步骤 4：自验（此时仍全在本地，任何错都能改）----
git tag -n99 -l v0.1.0                       # 看 tag message
git show v0.1.0 --stat                       # 看它指向的 commit
git ls-tree -r v0.1.0 --name-only | grep -E "\.db$|\.db-wal|\.db-shm|\.log$|demo\.gif"
#   ↑ 预期：只有 docs/demo.gif 命中。出现任何 .db / .log = 运行期数据混进 tag，先回滚再查

# ---- 步骤 5：暂不推远端（本次决策）——下面这行【不要执行】----
# git push origin v0.1.0
```

**为什么步骤 5 不执行**：见第 0 节。推上去才会触发 Release 构建（打 exe、挂 `market-watch.zip`）。
在用户明确放行之前，tag 只停在本地。

---

## 2. 发布检查清单（打 tag 前逐项确认）

| # | 检查项 | 判据 |
|---|---|---|
| 1 | 工作区干净 | `git status --short` 无输出（或只剩预期项） |
| 2 | tag 指向的 commit = 门禁全绿那个 | `git log --oneline -1` 与 CI 绿的 commit 一致 |
| 3 | `docs/demo.gif` 已 `git add` | `git ls-files docs/demo.gif` 有输出 |
| 4 | `CHANGELOG.md` 已提交 | `git ls-files CHANGELOG.md` 有输出 |
| 5 | `docs/RELEASE.md` 已提交 | `git ls-files docs/RELEASE.md` 有输出 |
| 6 | mypy | `Success: no issues found in 59 source files` |
| 7 | ruff / black | `All checks passed` / `88 files unchanged` |
| 8 | pytest | `269 passed / 0 failed`（269 collected）。**本机的汇总行会被 tmp 清理异常吞掉、退出码变 1**，通过数必须走 `--junitxml` 判据，见 `CONTRIBUTING.md` §4 |
| 9 | 覆盖率闸门 | `--cov-fail-under=35` 通过（当前实测总覆盖率 **50%** = 行覆盖 52.7% / 分支覆盖 39.2%） |
| 10 | 发布链路自验 | `python release/build_exe.py` 末尾打印「全部验证通过。」 |
| 11 | 发布包卫生 | zip 内 `*.db` / `*.db-wal` / `*.db-shm` / `*.log` 均为 0（实测 461 条目） |
| 12 | 运行期数据不入库 | `git check-ignore -v src/market_watch/data/market_watch.db` 有命中（`.gitignore:58:*.db`） |
| 13 | 密钥扫描 | 本地 gitleaks 未安装，由 CI 那一步兜（`.github/gitleaks.toml`） |

第 13 项照实标注：**本机没装 gitleaks，本地这一步跑不了**，不是「已扫过」。

---

## 3. 回滚方案

### 3.1 tag 只在本地（当前状态）——零成本，删了重打

```bash
git tag -d v0.1.0
# 若已 commit 但还没打 tag，只想撤那次提交：
git reset --soft HEAD~1        # 改动保留在工作区，可改完再提交
```

### 3.2 已经推到远端（更糟的情况）

```bash
git push --delete origin v0.1.0    # 先删远端
git tag -d v0.1.0                  # 再删本地
```

**远端 tag 删除的风险，说清楚再动手：**

- **Release 工作流可能已经跑起来了**：`on: push: tags: ["v*"]` 是推上去就触发的，删 tag 不会回滚已经产出的 Release 与 `market-watch.zip`；要连产物一起撤，得再去 Releases 页面删那个 release。
- **别人可能已经拉到了这个 tag**：`git fetch` 拿到过 v0.1.0 的人，本地仍会留着它；他们需要 `git tag -d v0.1.0` 或 `git fetch --prune --tags` 才会消失。删不掉别人手里的引用。
- **同名 tag 重打会被拒绝**：远端删掉后如果又推一个同名但指向不同 commit 的 v0.1.0，协作方会拿到非快进更新，需要强制同步。**更干净的做法是别复用版本号**——发现问题就发 `v0.1.1`，别去改已经存在的 `v0.1.0`。
- 这条正是「暂不推远端」的好处：**现在删本地 tag 没有任何外溢影响**。

---

## 4. 之后（等用户放行 push 之后才做）

1. `git push origin v0.1.0` → 触发 `release.yml`：Windows runner 上 `uv pip install -e .` → PyInstaller 打 onedir → 跑自验 → 出 `market-watch.zip` → 建 GitHub Release 并挂 zip + CHANGELOG。
2. 建完 Release 后回来看一眼：zip 体积约 27 MB、条目里没有 db/log、`config/sources.yaml` 与 `docs/_robots_snapshot.json` 在。
