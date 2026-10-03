# 演示 GIF 录制说明

README 顶部已经留好引用位：

```markdown
![market-watch 演示：policy-check 9/9 → once 跑一轮 → 看板显示价格曲线与告警卡片](docs/demo.gif)
```

**目标文件**：`docs/demo.gif`（**尚未录制**，仓库里还不存在；录制后放进来即可，README 无需改动）。
**目标时长**：10–15 秒，无旁白，靠终端与浏览器画面自己说话，≤3 MB，建议 1280×720 / 15 fps。

---

## 1. 录什么（分镜，按时间轴）

| 时间 | 画面 | 操作 | 要在画面里看到的东西 |
|---|---|---|---|
| 0–2 s | 终端（PowerShell，推荐 Windows Terminal） | `cp .env.example .env`（可选）→ `market-watch policy-check` | 9 行 `[OK]` + 末行 `合规检查通过 9/9`，含 `有效速率: 8.00 rps (= 官方 600/min × 安全系数 0.80)` |
| 2–4 s | 同上 | `market-watch once --source gw2_prices` | 一行 `cycle=… pages=… records=… written=… alerts=… drift=… duration=…`，给出 `pages` 与 `drift` 两个关键指标就够 |
| 4–6 s | 终端 → 浏览器 | `market-watch serve` | 命令回显 + 浏览器打开 `http://127.0.0.1:8000` |
| 6–12 s | 浏览器看板 | 静态停留 + 鼠标轻移 | ① 价格曲线（ECharts，vendored 离线渲染）② 告警卡片 ③ 数据质量卡片。**真实数据真实渲染，不要 P 图、不要假曲线** |
| 12–15 s | 浏览器 → 终端收尾 | `Ctrl+C` 退出 serve | 优雅退出日志（`event="lifecycle.shutdown"` 之类，按实际输出） |

**一条纪律**：GIF 里出现的每一个数字都必须是你本机真实跑出来的。没有第二次跑出来的数据不要进画面。

## 2. 推荐录制方式

| 方案 | 适用场景 | 命令要点 |
|---|---|---|
| **snipaste + OBS 录屏 + ffmpeg 转 gif**（Windows 推荐，免费） | 要 10–15 s 完整流程，含浏览器 | OBS 录屏（画布 1280×720，60 fps 录完再降帧）→ 见下方 ffmpeg 命令 |
| **Kap**（Electron 应用，Mac/Win） | 只想录一小段终端 | 选中终端窗口拖进 Kap，调 15 fps，导出 gif |
| **Windows Terminal + `ttyd`** | 纯终端流程 | 可选，超出了必需范围 |

### ffmpeg 转 gif（OBS 录屏后）

```bat
:: 1) OBS 录屏导出 mp4：源 = market-watch，时长 15s，帧率原样
:: 2) 降到 15 fps 并限制调色板，控制体积
ffmpeg -i market-watch-record.mp4 -vf "fps=15,scale=1280:-1:flags=lanczos,split[s1][s2];[s1]palettegen=max_colors=128[p];[s2]paletteuse=dither=bayer:bayer_scale=3" -loop 0 -an docs/demo.gif
:: 如果画面里有中文乱码，先确认录屏编码是 UTF-8（Windows Terminal 默认 UTF-8），
:: 必要时加 -vf "subtitles=..." 或改用 -vf "fps=15,scale=1280:-1" 观察是否可辨。
```

体积超标时先降 `max_colors`（128 → 64）和 `bayer_scale`（3 → 2），**不要**靠裁剪内容凑时长。

## 3. 环境准备（先跑通再录）

```bash
git clone https://github.com/OWNER/market-watch.git && cd market-watch
uv sync                      # 或 pip install .
cp .env.example .env          # 然后改 MW_CONTACT_URL 为真实地址

market-watch policy-check     # 必须 9/9，否则录出来的也是红的
market-watch once --source gw2_prices
market-watch serve            # 浏览器打开 http://127.0.0.1:8000 先看一眼有没有数据
```

只有 `policy-check` 9/9 且看板有真实曲线时才开始录。

## 4. 交付检查清单

- [ ] `docs/demo.gif` 存在，≤3 MB，1280×720 左右，循环播放
- [ ] 前 3 秒能看清 `合规检查通过 9/9`
- [ ] 看板那段是真的浏览器窗口（不是截图、不是录屏软件界面）
- [ ] 无任何真实邮箱 / 令牌 / 内网路径暴露在画面里（`MW_CONTACT_URL` 就用文档样例占位域）
- [ ] 重录一遍跟第一次内容一致（不因重录产生"看起来更好看"的假数字）

## 5. 录不了怎么办

直接**不录**，README 顶部那段已经写了"GIF 位"`+alt 文案指向本文件，读者点 `docs/DEMO_RECORDING.md` 就能拿到录制脚本。
**严禁伪造截图或伪造曲线**——README 里所有数字（覆盖率 40%、600 req/min、8.0 rps）都来自本仓真实实测，GIF 里也一样。
