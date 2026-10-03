# src/market_watch/domain/policy.py
"""合规判定纯逻辑：UA 校验 / 额度钳制算术 / 只读判定 / 禁用库名单 / PII 反向断言。

架构依据：docs/01-architecture.md §11.2（C1/C4/C5/C6/C8），§5.1（额度常量）。

设计约束：
- **纯逻辑、零 IO**（不发请求、不读文件、不读环境变量），便于毫秒级单测。
- 本模块定义的两个常量是「官方额度换算」的唯一真相：`application/config.py`
  从这里导入并再导出，`.env` **不得**覆盖（C3：环境变量只能调低，不能调高）。
- 本层禁止 import httpx / sqlite3 / fastapi。
"""

from __future__ import annotations

from collections.abc import Mapping
import re
import sys
from typing import Final

from .errors import PolicyViolation, ReadOnlyViolation

__all__ = [
    "ALLOWED_METHODS",
    "EFFECTIVE_RPS_CAP",
    "FORBIDDEN_MODULES",
    "HOST_SAFETY_FACTOR",
    "MAX_PRICE",
    "OFFICIAL_RATE_LIMIT_PER_MIN",
    "PII_PATTERNS",
    "PLACEHOLDER_HOSTS",
    "SOFT_PLACEHOLDER_HOSTS",
    "UA_PATTERN",
    "assert_no_forbidden_deps",
    "assert_no_pii",
    "assert_readonly",
    "assert_single_proxy",
    "build_user_agent",
    "contact_host",
    "effective_rps",
    "forbidden_import_hits",
    "placeholder_kind",
    "scan_pii",
    "validate_user_agent",
]

# ===========================================================================
# C3 速率硬钳制常量（§5.1）
# ===========================================================================
#: 官方公布额度：实测响应头 `X-Rate-Limit-Limit: 600`（按官方 Wiki 为每 IP 每分钟）。
#: 这是**代码常量**，不在 `.env` 暴露；随官方文档变动时改这里（走 PR）。
OFFICIAL_RATE_LIMIT_PER_MIN: Final[int] = 600

#: 安全系数：为其他客户端、重试、时钟误差留余量。
HOST_SAFETY_FACTOR: Final[float] = 0.8

#: 推导出的有效上界 = 600 / 60 × 0.8 = 8.0 rps。**所有 rps 配置的上界**。
#: 这不是性能调优结果，是合规换算的上限起点。
#: ⚠️ 想提速的唯一合法路径是「用满 page_size=200 减少请求数」，禁止提高 rps/concurrency。
EFFECTIVE_RPS_CAP: Final[float] = OFFICIAL_RATE_LIMIT_PER_MIN / 60.0 * HOST_SAFETY_FACTOR  # 8.0


def effective_rps(configured: float) -> float:
    """把配置值钳制到合规上界。**只降不升**（C3）。

    >>> effective_rps(50.0)
    8.0
    >>> effective_rps(2.0)
    2.0
    """
    return max(0.0, min(float(configured), EFFECTIVE_RPS_CAP))


# ===========================================================================
# C1 用户代理（必须标识自己，禁止伪装搜索引擎）
# ===========================================================================
#: `market-watch/<semver> (+<contact_url>)`，contact_url 必须可联系、不得是占位域。
UA_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^market-watch/\d+\.\d+\.\d+ \(\+https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+\)$"
)
#: 硬拒绝：RFC 2606 保留域与 `.invalid`。命中即拒绝启动（主理人裁决：不得是 `example.invalid`）。
PLACEHOLDER_HOSTS: Final[frozenset[str]] = frozenset(
    {"example.invalid", "example.com", "example.org", "example.net", "invalid", "localhost"}
)

#: 软提示：文档样例用的占位域。放行但必须打 WARNING——部署者**必须**替换成真实可联系地址。
SOFT_PLACEHOLDER_HOSTS: Final[frozenset[str]] = frozenset({"you.example"})


def build_user_agent(version: str, contact_url: str) -> str:
    return f"market-watch/{version} (+{contact_url})"


def contact_host(ua: str) -> str | None:
    """从 UA 中抽出 contact_url 的主机部分。"""
    m = re.search(r"\+(https?://([^)/]+))", ua)
    return None if m is None else m.group(2).lower()


def placeholder_kind(ua: str) -> str:
    """'none' | 'soft' | 'hard'。hard 必须拒绝启动，soft 只告警。"""
    host = contact_host(ua)
    if host is None:
        return "none"
    if host in PLACEHOLDER_HOSTS or host.endswith(".invalid"):
        return "hard"
    if host in SOFT_PLACEHOLDER_HOSTS:
        return "soft"
    return "none"


def validate_user_agent(ua: str) -> None:
    """C1：UA 必须标识自己。失败即 `PolicyViolation`（→ 拒绝启动）。"""
    if not UA_PATTERN.match(ua):
        raise PolicyViolation(
            f"C1 失败：UA 必须形如 'market-watch/<x.y.z> (+<contact_url>)'，实际={ua!r}。"
            " 修复：在 .env 设置 MW_CONTACT_URL=https://你的可联系地址"
        )
    if placeholder_kind(ua) == "hard":
        raise PolicyViolation(
            f"C1 失败：contact_url 主机 {contact_host(ua)!r} 是保留占位域，"
            "必须替换为真实可联系地址（.env: MW_CONTACT_URL）。"
        )


# ===========================================================================
# C4 只读出网
# ===========================================================================
ALLOWED_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})


def assert_readonly(method: str, content: object) -> None:
    """C4：只允许 GET/HEAD 且无 body。任何其它方法抛 `ReadOnlyViolation`。"""
    if method.upper() not in ALLOWED_METHODS:
        raise ReadOnlyViolation(method=method, has_body=content is not None)
    if content is not None:
        raise ReadOnlyViolation(method=method, has_body=True)


# ===========================================================================
# C5 单一代理、无轮换（禁止代理池）
# ===========================================================================
def assert_single_proxy(proxy_url: str | None) -> None:
    """C5：只接受单一静态代理；含 `,` 视为代理池，直接拒绝启动。"""
    if proxy_url is None:
        return
    value = proxy_url.strip()
    if not value:
        return
    if "," in value:
        raise PolicyViolation(
            "C5 失败：proxy_url 含逗号（代理池）。本方案架构上禁止代理池对抗封禁；"
            "只允许单一静态代理（企业合规出口场景）。"
        )
    if value.startswith(("http://", "https://", "socks5://")) is False:
        raise PolicyViolation(f"C5 失败：proxy_url 协议不受支持: {value!r}")


# ===========================================================================
# C6 禁用依赖黑名单（运行时 + CI 双查）
# ===========================================================================
FORBIDDEN_MODULES: Final[frozenset[str]] = frozenset(
    {
        # 键鼠/输入层模拟（红线：禁止）
        "pyautogui",
        "pydirectinput",
        "pydirectinput_rs",
        "pywinauto",
        "ahk",
        "autohotkey.py",
        "mouse",
        "keyboard",
        "pyinterception",
        "interception",
        # 屏幕/图像模板匹配（多尺度匹配已移除，本仓无图像处理需求）
        "cv2",
        "skimage",
        "scikit-image",
        "pyautocv",
        # 进程内存读取 / 注入 / 封包
        "pymem",
        "pymem.exception",
        "ReadWriteMemory",
        "ctypes.windll__kernel32",
        "frida",
        "scapy",
        "pypacket",
        # 打码平台 / 反爬代理池
        "chaojiying",
        "ruokuai",
        "proxy_pool",
        "rotating_proxies",
    }
)


def forbidden_import_hits() -> list[str]:
    """运行期扫 `sys.modules`：命中即说明有代码路径引入了红线库。"""
    return sorted(name for name in FORBIDDEN_MODULES if name in sys.modules)


def assert_no_forbidden_deps() -> list[str]:
    """C6 运行期检查。返回 hits（空列表 = 通过）；非空由调用方决定 SystemExit(2)。"""
    return forbidden_import_hits()


# ===========================================================================
# C8 不采集个人信息（反向断言）
# ===========================================================================
#: 命中任一正则的原始字段会被丢弃（字段级），并记 data_reject。
PII_PATTERNS: Final[Mapping[str, re.Pattern[str]]] = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "handle": re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,30}\b"),
    "phone_cn11": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "coord_pair": re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}"),
}


def scan_pii(value: object) -> list[str]:
    """返回命中的 PII 规则名列表；空列表表示干净。"""
    if value is None or isinstance(value, (int, float, bool)):
        return []
    text = value if isinstance(value, str) else str(value)
    return [name for name, pattern in PII_PATTERNS.items() if pattern.search(text)]


def assert_no_pii(field: str, value: object) -> list[str]:
    """C8：命中即返回规则名，由调用方丢弃该字段 + 记 data_reject + warn 告警。"""
    return scan_pii(value)


# ===========================================================================
# §11.1 数据一致性常量
# ===========================================================================
MAX_PRICE: Final[int] = 10**9  # unit_price 上界（铜币）
