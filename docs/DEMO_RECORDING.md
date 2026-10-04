# 演示 GIF 录制说明

`docs/demo.gif` **已录制并入库**（`fe69c59`）：5 帧 / 1406×803 / 76 KB / 总时长 7800 ms / 无限循环，录于 v0.1.0。

本文件不是"录制计划"，是**怎么重录出同样一版**的说明——脚本在 [`scripts/record_demo_gif.py`](../scripts/record_demo_gif.py)（412 行；改动历史用 `git log -- scripts/record_demo_gif.py` 看，本文件不再钉具体 hash，免得改一次脚本就过期一次），**本文件内不含任何脚本**。

README 顶部的引用位（以 README 为准，两处 alt 文案必须一致）：

```markdown
![market-watch 本地看板：在物品搜索框输入 Mighty 选中 #68 Mighty Country Coat，点「查询」后价格曲线随 Y 轴重算而分离](docs/demo.gif)
```

---

## 1. 录出来的东西长什么样（5 帧）

| 帧 | 画面 | 要看清的东西 |
|---|---|---|
| 1 | 首屏 | 概览卡（物品总数 **24754** / 最近周期 `2026-10-04T05:39:17Z` / 24h 告警 0 / 24h 公告 0 / DB **17.71 MB**）、数据质量卡（24h 隔离记录 **10414** = `price.positive` 9808 + `spread.non_negative` 606）、采集健康卡（`api.guildwars2.com` 与 `www.guildwars2.com` 各 **8.00 rps**）。图表此时是默认 `item_id=24` |
| 2 | 搜索 | 物品搜索框输入 `Mighty` → 下拉出一条 `#68 Mighty Country Coat` |
| 3 | 选中 | 点该结果 → `item_id` 自动填 `68`，下拉收起（图表尚未刷新） |
| 4 | 查询 | 点「查询」→ 图表切到 item 68：Y 轴从 125–126 **整体重算成 0–50**，最低卖价 ≈48、最高买价 ≈7，两条线分离 |
| 5 | 滚到底部 | 「最新告警」「官方公告」区块 + 页脚合规声明原文 |

**全程是纯浏览器交互，一个终端画面都没有。** 帧时长 1400 / 1400 / 1000 / 1800 / 2200 ms，帧率由帧时长决定（不是固定 fps）。

**一条纪律**：GIF 里出现的每一个数字都必须是你本机真实跑出来的。没有第二次跑出来的数据不要进画面。

---

## 2. 录制方式：Edge headless + CDP

不是 OBS录屏，也不是 ffmpeg 转码。实际做法是：

- 用 `--remote-debugging-port` 起 **Edge headless**；
- 通过 **websocket** 连上 CDP，用 `Runtime.evaluate` 在页面里派发输入/点击事件、用 `Page.captureScreenshot` 逐帧取图；
- 拿到 5 张 PNG 后用 **Pillow** 合帧成 GIF。

驱动脚本已落库：**`scripts/record_demo_gif.py`**。

### 前置

```bash
pip install websocket-client Pillow      # 这两个不在项目运行期依赖里
```

### 录一版

```bash
market-watch serve --host 127.0.0.1 --port 8765
market-watch once --source gw2_prices    # 没数据就没曲线，录出来是空图
python scripts/record_demo_gif.py        # 默认覆盖 docs/demo.gif
python scripts/record_demo_gif.py --verify docs/demo.gif   # 废帧自检
```

### 参数（`--help` 实测）

```
usage: record_demo_gif.py [-h] [--verify GIF] [--url URL] [--out OUT]
                          [--edge EDGE] [--cdp-port CDP_PORT]
                          [--search SEARCH] [--keep-frames DIR]
                          [--min-diff MIN_DIFF]
```

| 参数 | 说明 | 默认 |
|---|---|---|
| `--url` | 看板地址 | `http://127.0.0.1:8765/` |
| `--out` | 输出 GIF 路径 | `docs/demo.gif` |
| `--edge` | Edge 可执行文件路径 | 自动探测（PATH 与两处常见安装路径） |
| `--cdp-port` | CDP 调试端口 | `9333` |
| `--search` | 第 2 帧在搜索框输入的关键字 | `Mighty` |
| `--keep-frames` | 把 5 张中间 PNG 留在该目录（默认写临时目录并清掉，避免把 `docs/` 弄脏） | — |
| `--min-diff` | 废帧判定阈值（比例） | `0.002` |
| `--verify GIF` | 只自检已有 GIF 的帧间差异，不录制 | — |

---

## 3. 脚本内建的三个防坑（都是真踩过的）

1. **搜到的物品不能还是表单默认的 `item_id=24`**：那样第 4 帧会和帧 3 一模一样（图表根本没动）。脚本检测到这种情况会打印告警，让你换 `--search`。**第一版就踩了**：搜 `Snow` 选中的仍是 #24，帧间差异只有 0.068%。
2. **`--verify` 会逐段算相邻帧平均像素差**，低于 0.2% 阈值判为废帧并**返回退出码 1**。别把退出码 1 当成"脚本挂了"，那是它在告诉你"这帧白录了"。
3. **关 Edge 别用 `taskkill /T /PID`**（脚本早期版本踩过）：`msedge.exe` 启动后会把真正的浏览器进程**另起一个**，launcher 自身随即退出，于是手里的 pid 已经不是进程树的根，`/T` 找不到子进程。后果是每录一轮就攒下 **12 个孤儿 headless 进程**，外加删不掉的临时 profile 目录（`WinError 32` 卡在文件句柄上）。脚本现在的做法是**按 `--user-data-dir` 匹配命令行**精确结束——每次录制都是唯一临时目录，所以只会命中本次启的进程，**不会碰你自己开着的 Edge**。

### `--verify` 实测输出（已入库那版）

```
文件 docs/demo.gif
  帧数 5 尺寸 (1406, 803) 大小 76 KB
  帧1->帧2: 0.281%  OK
  帧2->帧3: 0.282%  OK
  帧3->帧4: 0.209%  OK
  帧4->帧5: 5.215%  OK
结论：全部 4 段过渡均达标（阈值 0.2%）
```

---

## 4. 画面里的数据从哪来（真采集，不是 mock）

库里的数据来自 **4 轮 `market-watch once --source gw2_prices`**：

```
每轮 pages=140 records=27997 written=24754 rejects=3445 quality=COMPLETE
```

写入去重后就是概览卡上的 24754 个物品。页面由本机 `market-watch serve` 提供（`http://127.0.0.1:8765`）。

### ⚠️ 数据边界（重录时别越过这条线）

- 这次只覆盖约 **15 分钟的采集跨度（4 个时间点）**，所以曲线是**短序列**，**不代表长期历史行情**；
- `price_hourly` 表这一次是 **0 行**（rollup 要等 `gc` 滚动窗口才会填充），GIF 画的是默认的 `price_change` 表；
- 想要长序列：多跑几轮 `once`，再跑一次 `gc`，然后重录。

---

## 5. 重录检查清单

- [ ] `policy-check` 9/9（否则录出来的画面里数字是红的）
- [ ] 看板里确实有数据（空库录出来是空图）
- [ ] 帧 4 的图表真的换了 `item_id`（Y 轴重算），不是和帧 3 一样
- [ ] `--verify` 四段过渡全部 `OK`、退出码 0
- [ ] 无真实邮箱 / 令牌 / 内网路径进画面（`MW_CONTACT_URL` 用文档样例占位域即可）
- [ ] 重录后 README 那段的帧内容描述仍然对得上；**对不上就一起改，别让文档说一套、GIF 是另一套**

## 6. 录不了怎么办

**直接不录，也不要伪造。** README 顶部那段是 `alt` 文案 + 图片引用，GIF 缺失只会让图裂，不会让文档变成假的。

脚本已落库在 `scripts/record_demo_gif.py`，换台机器照第 2 节跑即可；需要人肉排查时先看 `--verify` 的退出码和那两行告警。

**严禁伪造截图或伪造曲线。** README 里所有数字（覆盖率 **50%**、600 req/min、8.0 rps）都来自本仓真实实测，GIF 里也一样。

---

## 7. 关于文档里的占位符

仓库地址里的 `OWNER` 已落地为 `akbar747/market-watch`（`pyproject.toml` 的 `project.urls`、`README` 的 badge 与 clone 命令、`CHANGELOG` 的 compare/release 链接均已同步）。

`https://you.example/contact` 仍是**占位符**：`.example` 是 [RFC 2606](https://datatracker.ietf.org/doc/html/rfc2606) 保留的示例顶级域，不会被真实解析，仓库里保留它是为了让合规检查在「克隆下来」时就能跑通，部署前在 `.env` 里把 `MW_CONTACT_URL` 换成自己的可联系地址。
