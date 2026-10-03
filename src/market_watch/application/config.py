# src/market_watch/application/config.py
"""pydantic-settings Settings；**官方额度硬钳制**的唯一落点（C3）。

架构依据：docs/01-architecture.md §5.1、§11.2-C3/C5/C6/C7、§13。

三条不可协商的约束（主理人裁决 2，Phase 2 落地）：
1. `OFFICIAL_LIMIT` 与 `HOST_SAFETY_FACTOR` 是**代码常量**，定义在 `domain/policy.py`，
   **不进 .env、不允许环境变量覆盖**。
2. 环境变量只能把 rps **调低**，不能调高；调高会被静默钳制并打 `rate.clamped` WARNING。
3. 想提速的唯一合法路径是**用满 page_size=200 减少请求数**，禁止提高 rps/concurrency。
   —— 这条写进 `clamp_rps` 的 docstring 与 `sources.yaml` 注释，防止后人"优化"掉。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..domain import policy
from ..domain.errors import PolicyViolation

__all__ = [
    "EFFECTIVE_RPS_CAP",
    "GLOBAL_INFLIGHT",
    "HOST_SAFETY_FACTOR",
    "OFFICIAL_LIMIT",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]

# ---- C3 常量：从 domain/policy 再导出（唯一真相源在 policy.py） ----
OFFICIAL_LIMIT: Final[int] = policy.OFFICIAL_RATE_LIMIT_PER_MIN  # 600 req/min
HOST_SAFETY_FACTOR: Final[float] = policy.HOST_SAFETY_FACTOR  # 0.8
EFFECTIVE_RPS_CAP: Final[float] = policy.EFFECTIVE_RPS_CAP  # 8.0 rps

#: §5.1 全局在飞任务闸
GLOBAL_INFLIGHT: Final[int] = 16

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]


def _default_db_path() -> Path:
    return _REPO_ROOT / "src" / "market_watch" / "data" / "market_watch.db"


class Settings(BaseSettings):
    """全量运行配置。`.env` 只提供**可下调**的旋钮，额度上界由代码决定。"""

    model_config = SettingsConfigDict(
        env_prefix="MW_",
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=False,
    )

    # ---- 身份（C1） ----
    version: str = "0.1.0"
    contact_url: str = Field(
        default="https://you.example/contact",
        description="C1：必须是真实可联系地址，占位域会被启动检查拒绝",
    )

    # ---- 速率（C3：只能调低） ----
    per_host_rps: Annotated[float, Field(gt=0, le=EFFECTIVE_RPS_CAP)] = EFFECTIVE_RPS_CAP
    per_host_burst: Annotated[int, Field(ge=1, le=64)] = 12
    per_host_concurrency: Annotated[int, Field(ge=1, le=8)] = 4
    global_inflight: Annotated[int, Field(ge=1, le=64)] = GLOBAL_INFLIGHT

    # ---- 超时（§5.1） ----
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 15.0
    write_timeout_s: float = 5.0
    pool_timeout_s: float = 10.0
    keepalive_expiry_s: float = 20.0
    pool_max_connections: int = 20
    pool_max_keepalive: int = 20
    http2: bool = False

    # ---- 重试（§5.2） ----
    max_attempts: int = 4
    backoff_base_s: float = 1.0
    backoff_factor: float = 2.0
    backoff_max_s: float = 30.0
    retry_after_max_s: float = 120.0
    total_attempt_budget_s: float = 90.0
    retry_cost: float = 1.0

    # ---- 熔断（§5.3） ----
    breaker_window_s: float = 60.0
    breaker_min_samples: int = 20
    breaker_failure_ratio: float = 0.5
    breaker_consecutive_5xx: int = 8
    breaker_open_duration_s: float = 60.0
    breaker_half_open_probes: int = 3
    breaker_half_open_success_to_close: int = 3

    # ---- 自适应（§4.2 AIMD） ----
    adaptive_floor_rps: float = 1.0
    adaptive_healthy_window_s: float = 300.0
    adaptive_recover_step: float = 1.10
    ratelimit_header_probe: bool = False  # 实测源不返回 Remaining 头 → 默认关闭（§6 #7）

    # ---- 调度周期（§5.4） ----
    prices_interval_s: int = 300
    listings_interval_s: int = 300
    items_interval_s: int = 86400
    news_interval_s: int = 900
    gc_interval_s: int = 3600

    # ---- 采集规模 ----
    page_size: Annotated[int, Field(ge=1, le=200)] = 200  # 官方上限 200，用满以减少请求数
    max_pages_per_cycle: int = 200  # 分页终止性的硬护栏（契约测试用）
    watchlist_max_items: int = 500
    topn_hourly: int = 2000

    # ---- 存储 ----
    db_path: Path = Field(default_factory=_default_db_path)
    writer_queue_size: int = 20000
    writer_batch_size: int = 500
    writer_max_batch: int = 2000

    # ---- 代理（C5：单一静态代理，禁止代理池） ----
    proxy_url: str | None = None

    # ---- 看板 ----
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    downsample_points: int = 2000

    # ---- 日志 ----
    log_json: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ---- 路径 ----
    config_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "config")
    rejects_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "data" / "rejects")
    pending_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "data" / "pending")

    # =====================================================================
    # 校验器：C3 硬钳制 / C5 代理 / 派生字段
    # =====================================================================
    @model_validator(mode="after")
    def _clamp_and_validate(self) -> Settings:
        # --- C3：速率硬钳制（只降不升） ---
        clamped = policy.effective_rps(self.per_host_rps)
        if clamped < self.per_host_rps:
            # 静默钳制 + WARNING 日志 event="rate.clamped"（§11.2-C3 规定行为）
            self._warn_clamped("per_host_rps", self.per_host_rps, clamped)
        self.per_host_rps = clamped

        # --- C5：代理不得是代理池 ---
        policy.assert_single_proxy(self.proxy_url)
        if self.proxy_url:
            os.environ.setdefault("MW_PROXY_CONFIGURED", "1")

        # --- C1：UA 必须标识自己 ---
        policy.validate_user_agent(self.user_agent)

        # --- 目录就绪（不在此处建 DB 文件，由 storage 负责） ---
        for directory in (self.rejects_dir, self.pending_dir):
            directory.mkdir(parents=True, exist_ok=True)

        return self

    def _warn_clamped(self, field: str, declared: float, effective: float) -> None:
        """钳制告警。此处不能用 structlog（配置尚未初始化），回落到 logging。"""
        import logging

        logging.getLogger("market_watch.config").warning(
            "rate.clamped field=%s declared=%.3f effective=%.3f cap=%.3f "
            "(官方 %d/min × 安全系数 %.2f)；想提速请减少请求数（用满 page_size=%d），禁止提高 rps/concurrency",
            field,
            declared,
            effective,
            EFFECTIVE_RPS_CAP,
            OFFICIAL_LIMIT,
            HOST_SAFETY_FACTOR,
            self.page_size,
        )

    # =====================================================================
    # 派生属性
    # =====================================================================
    @property
    def user_agent(self) -> str:
        """C1：`market-watch/<version> (+<contact_url>)`。禁止伪装搜索引擎 UA。"""
        return policy.build_user_agent(self.version, self.contact_url)

    @property
    def effective_rps_cap(self) -> float:
        return EFFECTIVE_RPS_CAP

    def httpx_timeout(self) -> Any:
        """构造 httpx.Timeout。局部导入避免 domain 层沾 httpx。"""
        import httpx

        return httpx.Timeout(
            connect=self.connect_timeout_s,
            read=self.read_timeout_s,
            write=self.write_timeout_s,
            pool=self.pool_timeout_s,
        )

    def httpx_limits(self) -> Any:
        import httpx

        return httpx.Limits(
            max_connections=self.pool_max_connections,
            max_keepalive_connections=self.pool_max_keepalive,
            keepalive_expiry=self.keepalive_expiry_s,
        )

    def forbidden_dependency_hits(self) -> list[str]:
        """C6 运行期检查入口。"""
        return policy.assert_no_forbidden_deps()


# ---------------------------------------------------------------------------
# 进程级缓存（避免每条请求都重读 .env）
# ---------------------------------------------------------------------------
_INSTANCE: Settings | None = None


def get_settings(**overrides: Any) -> Settings:
    """取 Settings（进程级单例）。带 overrides 时不走缓存（测试用）。"""
    global _INSTANCE
    if overrides:
        return Settings(**overrides)
    if _INSTANCE is None:
        try:
            _INSTANCE = Settings()
        except PolicyViolation as exc:
            raise SystemExit(
                f"[C1/C5 失败] {exc}\n修复：在 .env 中设置 MW_CONTACT_URL=<真实可联系地址>"
            ) from exc
        if policy.placeholder_kind(_INSTANCE.user_agent) == "soft":
            import logging

            logging.getLogger("market_watch.config").warning(
                "ua.placeholder contact_url=%s 是文档样例占位域，部署前必须替换为真实可联系地址",
                _INSTANCE.contact_url,
            )
    return _INSTANCE


def reset_settings_cache() -> None:
    global _INSTANCE
    _INSTANCE = None
