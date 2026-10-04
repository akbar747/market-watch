"""录制 market-watch 看板演示 GIF（Edge headless + CDP）。

为什么用 CDP 而不是 `msedge --screenshot`：
    命令行截图只能拿到静态首屏。演示要展示「搜索 → 选中 → 提交查询 → 图表随
    Y 轴重算」这条**交互链**，必须在页面里派发事件。CDP 是本机不用额外装
    Chromium 的唯一路子（agent-browser 之类要另下约 500 MB）。

依赖（不在项目运行期依赖里，需要单独装）：
    pip install websocket-client Pillow

用法：
    # 1) 先把看板跑起来，并灌上真实数据（没有数据就没有曲线，录出来是空图）
    market-watch serve --host 127.0.0.1 --port 8765
    market-watch once --source gw2_prices

    # 2) 录制（默认覆盖 docs/demo.gif）
    python scripts/record_demo_gif.py

    # 3) 自检帧间差异，确认没有「看着动了其实没动」的废帧
    python scripts/record_demo_gif.py --verify docs/demo.gif

**严禁伪造**：本脚本只截真实页面，不 P 图、不造曲线。GIF 里出现的每个数字都
必须是本机真跑出来的。
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Sequence
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from PIL import Image, ImageChops, ImageStat

#: 相邻帧平均像素差低于此比例即判为废帧（0.002 = 0.2%）
DEFAULT_MIN_DIFF = 0.002

#: 看板表单里 item_id 的默认值；录到的物品若还是它，第 4 帧必然是废帧
DEFAULT_ITEM_ID = "24"

_EDGE_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
_EDGE_NAMES: tuple[str, ...] = ("msedge", "microsoft-edge", "microsoft-edge-stable")


def _shutdown_edge(proc: subprocess.Popen, profile: str) -> None:
    """结束本次录制启的 Edge，含子进程。

    为什么不是 `taskkill /T /PID`：Edge 启动后会把真正的浏览器进程另起一个
    （launcher 自身随即退出），我们手里的 pid 往往已经不是进程树的根，`/T`
    因此找不到子进程——实测孤儿进程一个没少，还白攒了 12 个 msedge 在后台。

    可靠的做法是按 `--user-data-dir` 匹配命令行：每次录制都给一个唯一的临时
    目录，所以这个匹配**只会命中本次启的进程**，不会碰用户自己开的 Edge。
    profile 路径通过环境变量传入，避免在命令串里做引号转义（路径含中文与空格）。
    """
    if sys.platform == "win32":
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
            "Where-Object { $_.CommandLine -and "
            "$_.CommandLine.Contains($env:MW_GIF_PROFILE) } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
            "-ErrorAction SilentlyContinue }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "MW_GIF_PROFILE": profile},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    print("Edge 已关闭")


def find_edge(explicit: str | None) -> str:
    """定位 Edge 可执行文件：显式路径 > PATH > 常见安装位置。"""
    if explicit:
        if not Path(explicit).is_file():
            raise SystemExit(f"--edge 指向的文件不存在: {explicit}")
        return explicit
    for name in _EDGE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for candidate in _EDGE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("没找到 Edge。用 --edge 指定可执行文件，或装一个 Chromium 系浏览器。")


class CdpSession:
    """极简 CDP 客户端：只实现录制用到的调用。"""

    def __init__(self, ws_url: str) -> None:
        try:
            import websocket  # noqa: PLC0415 - 惰性导入，让 --verify 不必装它
        except ModuleNotFoundError as exc:
            raise SystemExit(
                "缺少 websocket-client。录制需要：pip install websocket-client Pillow"
            ) from exc
        self._ws = websocket.create_connection(ws_url, timeout=30)
        self._seq = 0

    def call(self, method: str, **params: object) -> dict:
        self._seq += 1
        mid = self._seq
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        # 只认属于本次调用的响应，跳过页面主动推的事件
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} -> {msg['error']}")
                return msg.get("result", {})

    def evaluate(self, expression: str) -> object:
        result = self.call(
            "Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True
        )
        return result.get("result", {}).get("value")

    def screenshot(self, path: Path) -> Image.Image:
        raw = base64.b64decode(self.call("Page.captureScreenshot", format="png")["data"])
        path.write_bytes(raw)
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        print(f"  帧 {path.name}: {len(raw) // 1024} KB, {image.size}")
        return image

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._ws.close()


def _http_json(url: str, method: str = "GET") -> dict:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_cdp(base: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _http_json(f"{base}/json/version")
        except OSError:
            time.sleep(0.4)
        else:
            return True
    return False


def _js_string(value: str) -> str:
    """把 Python 字符串安全地嵌进 JS 字面量。"""
    return json.dumps(value)


def record(url: str, out: Path, edge: str, cdp_port: int, search: str) -> int:
    pictures = out.parent / "_frames"
    pictures.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{cdp_port}"

    # ignore_cleanup_errors：Edge 的子进程可能还攥着 profile 里 Cache 的文件句柄
    # （实测 `PermissionError: [WinError 32]`），删不掉也不该让整次录制失败。
    with tempfile.TemporaryDirectory(
        prefix="mw-gif-profile-", ignore_cleanup_errors=True
    ) as profile:
        print(f"启动 Edge (headless, CDP 端口 {cdp_port})...")
        proc = subprocess.Popen(
            [
                edge,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={cdp_port}",
                "--remote-allow-origins=*",
                "--hide-scrollbars",
                "--window-size=1440,900",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if not _wait_for_cdp(base):
                raise SystemExit("Edge 的 CDP 端口没起来。")
            print("Edge 就绪")

            # 新版 Chrome/Edge 的 /json/new 只接受 PUT，用 GET 会 405
            target = _http_json(
                f"{base}/json/new?" + urllib.parse.quote(url, safe=":/?=&"), method="PUT"
            )
            session = CdpSession(target["webSocketDebuggerUrl"])
            session.call("Page.enable")
            session.call("Runtime.enable")
            session.call("Page.navigate", url=url)
            time.sleep(4.0)  # 等 ECharts 与首屏几个 JSON API 画完

            frames: list[Image.Image] = []

            print("[1] 首屏")
            frames.append(session.screenshot(pictures / "01_home.png"))

            print(f"[2] 搜索 {search!r}")
            session.evaluate(
                "(()=>{const i=document.getElementById('fq');"
                f"i.value={_js_string(search)};"
                "i.dispatchEvent(new Event('input',{bubbles:true}));"
                "return i.value;})()"
            )
            time.sleep(1.6)  # 输入防抖 + 请求 + 渲染
            hits = session.evaluate("document.querySelectorAll('#fq-results a').length")
            print(f"    下拉结果数={hits}")
            if not hits:
                raise SystemExit(
                    f"搜索 {search!r} 没有下拉结果。换一个 --search 关键字，"
                    "或确认库里已有数据（先跑 `market-watch once --source gw2_prices`）。"
                )
            frames.append(session.screenshot(pictures / "02_search.png"))

            print("[3] 选中第一个结果")
            picked = session.evaluate(
                "(()=>{const a=document.querySelector('#fq-results a');"
                "if(!a) return null; a.click();"
                "return {text:a.textContent, item:document.getElementById('f-item').value};})()"
            )
            print(f"    选中={picked}")
            chosen = (picked or {}).get("item") if isinstance(picked, dict) else None
            if chosen is not None and str(chosen) == DEFAULT_ITEM_ID:
                print(
                    f"    ! 选中的仍是默认 {DEFAULT_ITEM_ID}，第 4 帧会与第 3 帧几乎一致"
                    "（图表不会变）。换个 --search 关键字。"
                )
            time.sleep(0.7)
            frames.append(session.screenshot(pictures / "03_picked.png"))

            print("[4] 提交查询（把图表切到选中的物品）")
            current = session.evaluate(
                "(()=>{const f=document.getElementById('f-series');"
                "if(f.requestSubmit) f.requestSubmit();"
                "else f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));"
                "return document.getElementById('f-item').value;})()"
            )
            print(f"    查询 item={current}")
            time.sleep(2.2)
            frames.append(session.screenshot(pictures / "04_series.png"))

            print("[5] 滚到底部（告警 / 官方公告 / 合规声明）")
            session.evaluate("window.scrollTo(0, document.body.scrollHeight); true")
            time.sleep(1.2)
            frames.append(session.screenshot(pictures / "05_bottom.png"))

            session.close()

            width = min(f.width for f in frames)
            height = min(f.height for f in frames)
            frames = [
                f.crop(((f.width - width) // 2, 0, (f.width - width) // 2 + width, height))
                for f in frames
            ]

            out.parent.mkdir(parents=True, exist_ok=True)
            durations = [1400, 1400, 1000, 1800, 2200]
            frames[0].save(
                out,
                save_all=True,
                append_images=frames[1:],
                duration=durations,
                loop=0,
                optimize=True,
            )
            size_kb = out.stat().st_size // 1024
            print(
                f"\nGIF 完成: {out}\n"
                f"  {len(frames)} 帧, {width}x{height}, {size_kb} KB, "
                f"总时长 {sum(durations)} ms"
            )
        finally:
            _shutdown_edge(proc, profile)
            # 父进程退了不代表子进程松手；等一会儿让它们释放 profile 里的句柄。
            time.sleep(1.5)

    script = Path(__file__).resolve()
    with contextlib.suppress(ValueError):
        script = script.relative_to(Path.cwd())
    print(f"\n下一步自检（务必跑）：\n  python {script} --verify {out}")
    return 0


def verify(gif: Path, min_diff: float) -> int:
    """检查相邻帧差异，揪出「看着动了其实没动」的废帧。"""
    if not gif.is_file():
        raise SystemExit(f"找不到 {gif}")
    image = Image.open(gif)
    count = getattr(image, "n_frames", 1)
    print(f"文件 {gif}")
    print(f"  帧数 {count} 尺寸 {image.size} 大小 {gif.stat().st_size // 1024} KB")

    frames = []
    for index in range(count):
        image.seek(index)
        frames.append(image.convert("RGB"))

    weak: list[int] = []
    for index in range(count - 1):
        diff = ImageChops.difference(frames[index], frames[index + 1])
        mean = ImageStat.Stat(diff).mean
        average = sum(mean) / len(mean)
        ratio = average / 255.0
        passed = ratio >= min_diff
        print(
            f"  帧{index + 1}->帧{index + 2}: {ratio * 100:.3f}%  "
            f"平均差 [{mean[0]:.2f},{mean[1]:.2f},{mean[2]:.2f}]  "
            f"{'OK' if passed else '★变化过小'}"
        )
        if not passed:
            weak.append(index + 1)

    if weak:
        print(
            f"\n结论：废帧 {weak}（阈值 {min_diff * 100:.1f}%）——重录，或换一个能让画面真变的操作"
        )
        return 1
    print(f"\n结论：全部 {count - 1} 段过渡均达标（阈值 {min_diff * 100:.1f}%）")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="录制 / 自检 market-watch 看板演示 GIF")
    parser.add_argument(
        "--verify",
        type=Path,
        metavar="GIF",
        help="只自检已有 GIF 的帧间差异，不录制",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8765/", help="看板地址")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "docs" / "demo.gif",
        help="输出 GIF 路径（默认 docs/demo.gif）",
    )
    parser.add_argument("--edge", default=None, help="Edge 可执行文件路径（默认自动探测）")
    parser.add_argument("--cdp-port", type=int, default=9333, help="CDP 调试端口")
    parser.add_argument(
        "--search", default="Mighty", help="第 2 帧在搜索框输入的关键字（别用命中的默认物品）"
    )
    parser.add_argument(
        "--min-diff",
        type=float,
        default=DEFAULT_MIN_DIFF,
        help=f"废帧判定阈值，比例（默认 {DEFAULT_MIN_DIFF}）",
    )
    args = parser.parse_args(argv)

    if args.verify is not None:
        return verify(args.verify, args.min_diff)

    try:
        _http_json(f"{args.url.rstrip('/')}/api/health")
    except OSError as exc:
        raise SystemExit(
            f"连不上看板 {args.url}。先起服务：market-watch serve --host 127.0.0.1 --port 8765"
        ) from exc

    return record(args.url, args.out, find_edge(args.edge), args.cdp_port, args.search)


if __name__ == "__main__":
    sys.exit(main())
