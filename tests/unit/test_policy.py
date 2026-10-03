# tests/unit/test_policy.py
"""C1/C3/C4/C5/C6/C8 的断言（§11.2）。"""

from __future__ import annotations

import pytest

from market_watch.domain import policy
from market_watch.domain.errors import PolicyViolation, ReadOnlyViolation


class TestRateClamp:
    def test_effective_rps_never_exceeds_cap(self) -> None:
        assert policy.effective_rps(50.0) == policy.EFFECTIVE_RPS_CAP
        assert policy.effective_rps(8.0) == 8.0

    def test_effective_rps_can_only_go_lower(self) -> None:
        assert policy.effective_rps(2.0) == 2.0
        assert policy.effective_rps(0.1) == pytest.approx(0.1)

    def test_cap_is_official_times_safety(self) -> None:
        assert policy.EFFECTIVE_RPS_CAP == pytest.approx(
            policy.OFFICIAL_RATE_LIMIT_PER_MIN / 60.0 * policy.HOST_SAFETY_FACTOR
        )
        assert policy.EFFECTIVE_RPS_CAP == pytest.approx(8.0)

    def test_settings_clamps_and_warns(self, settings) -> None:
        """C3：Settings 校验器把超限值静默钳制（不抛异常，只告警）。"""
        from market_watch.application.config import Settings

        s = Settings(per_host_rps=8.0, db_path=settings.db_path)
        assert s.per_host_rps <= policy.EFFECTIVE_RPS_CAP

    def test_env_cannot_raise_rps(self, settings, monkeypatch) -> None:
        """C3：环境变量只能调低，不能调高（上界由 Field(le=...) 在入口就拒绝）。"""
        from pydantic import ValidationError

        from market_watch.application.config import Settings

        monkeypatch.setenv("MW_PER_HOST_RPS", "50")
        with pytest.raises(ValidationError):
            Settings(db_path=settings.db_path, _env_file=None)


class TestUserAgent:
    def test_valid_ua_passes(self) -> None:
        """真实可识别 UA（含可联系站点）应放行。"""
        policy.validate_user_agent("market-watch/0.1.0 (+https://market-watch.dev/bot)")

    def test_placeholder_invalid_is_rejected(self) -> None:
        with pytest.raises(PolicyViolation):
            policy.validate_user_agent("market-watch/0.1.0 (+https://example.invalid/contact)")

    def test_rfc2606_domains_rejected(self) -> None:
        for host in ("example.com", "example.org", "example.net"):
            with pytest.raises(PolicyViolation):
                policy.validate_user_agent(f"market-watch/0.1.0 (+https://{host}/x)")

    def test_soft_placeholder_only_warns(self) -> None:
        assert (
            policy.placeholder_kind("market-watch/0.1.0 (+https://you.example/contact)") == "soft"
        )
        policy.validate_user_agent("market-watch/0.1.0 (+https://you.example/contact)")

    def test_malformed_ua_rejected(self) -> None:
        for ua in ("curl/8.0", "Mozilla/5.0", "Googlebot/2.1", "market-watch/0.1.0"):
            with pytest.raises(PolicyViolation):
                policy.validate_user_agent(ua)

    def test_no_search_engine_impersonation(self) -> None:
        """禁止伪装搜索引擎 UA。"""
        with pytest.raises(PolicyViolation):
            policy.validate_user_agent(
                "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
            )


class TestReadonly:
    def test_get_head_allowed(self) -> None:
        policy.assert_readonly("GET", None)
        policy.assert_readonly("HEAD", None)

    def test_post_rejected(self) -> None:
        with pytest.raises(ReadOnlyViolation):
            policy.assert_readonly("POST", None)

    def test_get_with_body_rejected(self) -> None:
        with pytest.raises(ReadOnlyViolation):
            policy.assert_readonly("GET", b"{}")


class TestProxy:
    def test_none_ok(self) -> None:
        policy.assert_single_proxy(None)
        policy.assert_single_proxy("")

    def test_single_ok(self) -> None:
        policy.assert_single_proxy("http://proxy.corp.local:3128")

    def test_proxy_pool_rejected(self) -> None:
        """C5：含逗号 = 代理池，拒绝启动。"""
        with pytest.raises(PolicyViolation):
            policy.assert_single_proxy("http://p1:3128,http://p2:3128")

    def test_bad_scheme_rejected(self) -> None:
        with pytest.raises(PolicyViolation):
            policy.assert_single_proxy("ftp://proxy:21")


class TestForbiddenDeps:
    def test_blacklist_contains_key_items(self) -> None:
        for name in ("cv2", "pyautogui", "pymem", "frida", "proxy_pool", "pydirectinput"):
            assert name in policy.FORBIDDEN_MODULES

    def test_no_hits_in_clean_process(self) -> None:
        assert policy.assert_no_forbidden_deps() == []


class TestPII:
    def test_email_detected(self) -> None:
        assert "email" in policy.scan_pii("contact me at a.b@example.org now")

    def test_phone_detected(self) -> None:
        assert "phone_cn11" in policy.scan_pii("tel 13800138000")

    def test_handle_detected(self) -> None:
        assert "handle" in policy.scan_pii("thanks @someone123")

    def test_price_not_flagged(self) -> None:
        """关键负例：价格/数量是纯数字，不得被误判为 PII。"""
        assert policy.scan_pii(198) == []
        assert policy.scan_pii(108640) == []
        assert policy.scan_pii("Sealed Package of Snowballs") == []

    def test_item_name_not_flagged(self) -> None:
        assert policy.scan_pii("Vial of Powerful Blood") == []
