# examples/fetch_one_item.py
"""示例：拉**单个商品**的实时报价，打印 best bid / best ask / 价差 / 盘口水深。

两种模式
--------
* 默认（联网）：一次 `GET https://api.guildwars2.com/v2/commerce/prices/{--item-id}`
  —— 官方公开端点，免鉴权、只读、无 body（`C4`）。
* `--off`（离线）：回放仓库里录制的样本
  `src/market_watch/infrastructure/adapters/fixtures/gw2_prices_page1.json`，
  零出网、零网络依赖，CI 与文档构建里可以直接跑。

> [!IMPORTANT]
> 示例只在**默认联网模式**下打一次真实请求，且 URL 的 host 写死为官方主机
> `api.guildwars2.com`（不接 `--host` 参数，避免被改去打别的站点）。
> 任何出网方法都是 `GET`，没有 `--force`、没有代理池、没有并发。

用法::

    python examples/fetch_one_item.py                     # 联网：拉 item 24
    python examples/fetch_one_item.py --off               # 离线：回放录制样本
    python examples/fetch_one_item.py --item-id 68        # 换商品（id 必须是正整数）
    python examples/fetch_one_item.py --json              # 机器可读输出

退出码：0 = 成功；1 = 参数非法或抓取失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, "src")  # 未安装（uv run python）时也能 import 到 src-layout 源码

import httpx  # noqa: E402

from market_watch.domain.errors import PayloadError  # noqa: E402
from market_watch.domain.models import ResponseView  # noqa: E402
from market_watch.domain.policy import build_user_agent  # noqa: E402
from market_watch.domain.pricing import mid_of, spread_of  # noqa: E402

#: 唯一的真实主机，写死在代码里，不接受命令行覆盖
OFFICIAL_HOST = "api.guildwars2.com"

#: 仓库内录制的官方响应样本（离线模式回放它）
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "market_watch"
    / "infrastructure"
    / "adapters"
    / "fixtures"
    / "gw2_prices_page1.json"
)

#: 示例的 UA 联系地址是文档占位域，只会打 WARNING 不会拒绝；真实部署请在 .env 里设
#: MW_CONTACT_URL=<你的可联系地址>，那时 UA 就合规了（C1）
CONTACT_URL = "https://you.example/contact"
VERSION = "0.1.0"


def _print_block(payload: dict[str, Any], *, item_id: int) -> dict[str, Any]:
    """把官方 payload 提成一行人类可读 + 一行 JSON。"""
    buy = payload.get("buys") or {}
    sell = payload.get("sells") or {}
    buy_px = buy.get("unit_price")
    sell_px = sell.get("unit_price")
    buy_qty = buy.get("quantity")
    sell_qty = sell.get("quantity")

    spread = spread_of(buy_px, sell_px)
    mid = mid_of(buy_px, sell_px)

    print(f"item_id        : {item_id}")
    print(f"best bid (买一) : {buy_px if buy_px is not None else 'N/A'}")
    print(f"best ask (卖一) : {sell_px if sell_px is not None else 'N/A'}")
    print(f"spread   (价差) : {spread if spread is not None else 'N/A'}")
    print(f"mid        (中价): {mid if mid is not None else 'N/A'}")
    print(f"depth buy      : {buy_qty if buy_qty is not None else 'N/A'}")
    print(f"depth sell     : {sell_qty if sell_qty is not None else 'N/A'}")

    if spread is not None and spread < 0:
        # 官方端点不会给负价差；出现即为脏数据，与 consistency.check_spread 同一判定
        print("水位：⚠️ spread < 0 —— 脏数据，本行不会被异动检测消费")

    row = {
        "item_id": item_id,
        "buy_unit_price": buy_px,
        "sell_unit_price": sell_px,
        "spread": spread,
        "mid": mid,
        "depth_buy": buy_qty,
        "depth_sell": sell_qty,
    }
    return row


async def _fetch_online(item_id: int, version: str, contact: str) -> ResponseView:
    """联网：一次 GET，返回 `ResponseView`（不是 `httpx.Response`，跨层只用快照）。"""
    ua = build_user_agent(version, contact)
    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(10.0)) as client:
        resp = await client.get(
            f"https://{OFFICIAL_HOST}/v2/commerce/prices/{item_id}",
            headers={"User-Agent": ua, "Accept": "application/json"},
        )
        resp.raise_for_status()
        return ResponseView(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            content=resp.content,
            url=str(resp.url),
            elapsed_ms=0.0,
        )


def _fetch_offline(item_id: int) -> ResponseView:
    """离线：回放录制样本，挑出这一个 item 的官方响应体。"""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    match = [row for row in raw if int(row["id"]) == item_id]
    if not match:
        raise PayloadError(
            f"录制样本 {FIXTURE.name} 里没有 item_id={item_id}；" "换一个 id 或用联网模式跑"
        )
    return ResponseView(
        status_code=200,
        headers={"Content-Type": "application/json"},
        content=json.dumps(match[0]).encode("utf-8"),
        url=f"https://{OFFICIAL_HOST}/v2/commerce/prices/{item_id}",
        elapsed_ms=0.0,
    )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="拉单个商品的实时报价并打印买卖价/价差/水深")
    parser.add_argument("--item-id", type=int, default=24, help="GW2 商品 id（正整数），默认 24")
    parser.add_argument("--off", action="store_true", help="离线模式：回放仓库录制样本，零出网")
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")
    parser.add_argument("--contact", default=CONTACT_URL, help="C1 联系地址（占位域只告警）")
    parser.add_argument("--version", default=VERSION, help="UA 版本号")
    args = parser.parse_args(argv)

    if args.item_id <= 0:
        print("[FAIL] --item-id 必须是正整数", file=sys.stderr)
        return 1

    try:
        if args.off:
            view = _fetch_offline(args.item_id)
            mode = "offline(replay fixtures)"
        else:
            view = await _fetch_online(args.item_id, args.version, args.contact)
            mode = "online(GET /v2/commerce/prices/{id})"
    except (httpx.HTTPError, PayloadError) as exc:
        print(f"[FAIL] 取数失败（{mode}）: {exc}", file=sys.stderr)
        return 1

    payload = view.json()  # 解析失败抛 PayloadError → ErrorClass.JSON_DECODE
    row = _print_block(payload, item_id=int(payload.get("id", args.item_id)))

    if args.as_json:
        print(json.dumps({"mode": mode, **row}, ensure_ascii=False, indent=2))
    else:
        print(f"\nmode           : {mode}")
        print(f"url            : {view.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
