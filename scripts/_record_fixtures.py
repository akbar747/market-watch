# scripts/_record_fixtures.py
"""一次性录制真实端点样本（脚本向，用 loguru 风格输出）。

合规说明：
- 仅对官方公开、免鉴权端点发 GET；
- 请求间隔 >= 1.0s（远低于自限 8 rps 上限），总量 <= 6 次；
- 录制目的：为契约测试提供「结构真实」的样本，脱敏不删结构。
运行一次即可，产物进 git；之后禁止重复跑。
"""

from __future__ import annotations

import json
from pathlib import Path
import time
import urllib.error
import urllib.request

FIX = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "market_watch"
    / "infrastructure"
    / "adapters"
    / "fixtures"
)
HTTP = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "http"
DOCS = Path(__file__).resolve().parent.parent / "docs"
UA = "market-watch/0.1.0 (+https://you.example/contact) fixture-recorder"

TARGETS: list[tuple[str, str, Path, str]] = [
    (
        "https://api.guildwars2.com/v2/commerce/prices?page=0&page_size=200",
        "gw2_prices_page1.json",
        FIX,
        "json",
    ),
    ("https://api.guildwars2.com/v2/commerce/listings/24", "gw2_listings_item24.json", FIX, "json"),
    ("https://api.guildwars2.com/v2/items?ids=24,68", "gw2_items_ids24_68.json", FIX, "json"),
    ("https://www.guildwars2.com/en/feed/", "gw2_news_feed.xml", FIX, "xml"),
]

ROBOTS = [
    ("https://api.guildwars2.com/robots.txt", "api.guildwars2.com"),
    ("https://www.guildwars2.com/robots.txt", "www.guildwars2.com"),
]


def get(url: str, accept: str) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - 固定官方 https 端点
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:  # robots.txt 404 走这里
        return exc.code, dict(exc.headers), exc.read()


def main() -> int:
    FIX.mkdir(parents=True, exist_ok=True)
    HTTP.mkdir(parents=True, exist_ok=True)

    robots_report: list[dict[str, object]] = []
    for url, host in ROBOTS:
        status, headers, body = get(url, "text/plain")
        robots_report.append(
            {
                "host": host,
                "url": url,
                "status": status,
                "content_type": headers.get("Content-Type"),
                "server": headers.get("Server"),
                "bytes": len(body),
                "body": body.decode("utf-8", errors="replace"),
            }
        )
        print(f"[robots] {host} -> {status} ({len(body)} B)")
        time.sleep(1.0)

    (DOCS / "_robots_snapshot.json").write_text(
        json.dumps(robots_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for url, filename, dest, kind in TARGETS:
        status, headers, body = get(
            url, "application/json" if kind == "json" else "application/rss+xml"
        )
        print(f"[record] {url} -> {status} ({len(body)} B)")
        interesting = {
            k: v
            for k, v in headers.items()
            if k.lower()
            in {
                "content-type",
                "cache-control",
                "x-page-total",
                "x-page-size",
                "x-result-total",
                "x-result-count",
                "x-rate-limit-limit",
                "server",
                "etag",
                "last-modified",
                "access-control-expose-headers",
            }
        }
        print(f"         headers={interesting}")
        (dest / filename).write_bytes(body)
        (dest / (filename + ".headers.json")).write_text(
            json.dumps(
                {"url": url, "status": status, "headers": interesting}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        # tests/fixtures/http 同源副本
        (HTTP / filename).write_bytes(body)
        time.sleep(1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
