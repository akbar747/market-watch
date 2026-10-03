# tests/unit/test_retry.py
"""§6 矩阵的唯一决策点：`decide(err, view, attempt)`。"""

from __future__ import annotations

import random

import pytest

from market_watch.domain.errors import ErrorClass, policy_for
from market_watch.domain.models import ResponseView
from market_watch.infrastructure.http.retry import (
    BACKOFF_MAX_S,
    RETRY_AFTER_MAX_S,
    VENDOR_FLOOR_S,
    decide,
)

RNG = random.Random(42)


def _view(*, status: int = 200, retry_after: str | None = None) -> ResponseView:
    headers = {"Content-Type": "application/json"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return ResponseView(status, headers, b"{}", "https://x.example/p", 1.0)


class TestNonRetryable:
    @pytest.mark.parametrize(
        "err",
        [
            ErrorClass.OK,
            ErrorClass.AUTH_403,
            ErrorClass.ROBOTS_DENIED,
            ErrorClass.SSL_INVALID,
            ErrorClass.SCHEMA_INVALID,
            ErrorClass.CONSISTENCY_FAILED,
            ErrorClass.PAGINATION_DRIFT,
            ErrorClass.NOT_MODIFIED,
            ErrorClass.CANCELLED,
            ErrorClass.UNKNOWN,
        ],
    )
    def test_never_retries(self, err: ErrorClass) -> None:
        retry, wait = decide(err, None, 1, rng=RNG)
        assert retry is False
        assert wait == 0.0


class TestAttemptLimits:
    def test_429_stops_after_max_attempts(self) -> None:
        pol = policy_for(ErrorClass.RATE_LIMITED_429)
        assert pol["max_attempts"] == 4
        assert decide(ErrorClass.RATE_LIMITED_429, None, 4, rng=RNG)[0] is False
        assert decide(ErrorClass.RATE_LIMITED_429, None, 3, rng=RNG)[0] is True

    def test_5xx_stops_after_max_attempts(self) -> None:
        assert decide(ErrorClass.SERVER_5XX, None, 4, rng=RNG)[0] is False

    def test_json_decode_stops_after_3(self) -> None:
        assert decide(ErrorClass.JSON_DECODE, None, 3, rng=RNG)[0] is False


class TestRetryAfter:
    def test_429_prefers_retry_after(self) -> None:
        view = _view(status=429, retry_after="7")
        retry, wait = decide(ErrorClass.RATE_LIMITED_429, view, 1, rng=RNG)
        assert retry is True
        assert wait == pytest.approx(7.0)

    def test_retry_after_beyond_trust_cap_falls_back(self) -> None:
        """RETRY_AFTER_MAX_S=120：超过则回落到 BACKOFF_MAX_S，不傻等。"""
        view = _view(status=429, retry_after="9999")
        retry, wait = decide(ErrorClass.RATE_LIMITED_429, view, 1, rng=RNG)
        assert retry is True
        assert wait <= BACKOFF_MAX_S
        assert wait < RETRY_AFTER_MAX_S

    def test_vendor_420_forces_30s_floor(self) -> None:
        """§6 #6：强制 max(Retry-After, 30s) 起步。"""
        retry, wait = decide(ErrorClass.RATE_LIMITED_VENDOR, None, 1, rng=RNG)
        assert retry is True
        assert wait >= VENDOR_FLOOR_S

    def test_vendor_420_uses_larger_retry_after(self) -> None:
        view = _view(status=420, retry_after="45")
        _retry, wait = decide(ErrorClass.RATE_LIMITED_VENDOR, view, 1, rng=RNG)
        assert wait == pytest.approx(45.0)


class TestBackoffShape:
    def test_json_decode_is_linear_not_exponential(self) -> None:
        """§6 #8：JSON 解析失败固定 2.0s * n，不指数。"""
        _, w1 = decide(ErrorClass.JSON_DECODE, None, 1, rng=RNG)
        _, w2 = decide(ErrorClass.JSON_DECODE, None, 2, rng=RNG)
        assert w1 == pytest.approx(2.0)
        assert w2 == pytest.approx(4.0)

    def test_exponential_growth_is_capped(self) -> None:
        _, wait = decide(ErrorClass.READ_TIMEOUT, None, 10, rng=RNG)
        assert wait <= BACKOFF_MAX_S

    def test_full_jitter_never_exceeds_cap(self) -> None:
        for _ in range(200):
            _, wait = decide(ErrorClass.CONNECT_TIMEOUT, None, 3, rng=RNG)
            assert 0.0 <= wait <= BACKOFF_MAX_S

    def test_jitter_actually_varies(self) -> None:
        waits = {decide(ErrorClass.CONNECT_TIMEOUT, None, 3, rng=RNG)[1] for _ in range(50)}
        assert len(waits) > 10, "full jitter 应当产生多样化的等待值（防重试同步再撞）"


class TestPolicyTable:
    def test_cool_factors(self) -> None:
        assert policy_for(ErrorClass.RATE_LIMITED_429)["cool_factor"] == pytest.approx(0.5)
        assert policy_for(ErrorClass.RATE_LIMITED_VENDOR)["cool_factor"] == pytest.approx(0.5)
        assert policy_for(ErrorClass.RATE_LIMIT_HEADER_LOW)["cool_factor"] == pytest.approx(0.6)
        assert policy_for(ErrorClass.READ_TIMEOUT)["cool_factor"] == pytest.approx(0.7)

    def test_terminal_errors_counted_as_failure(self) -> None:
        for err in (ErrorClass.AUTH_403, ErrorClass.SSL_INVALID, ErrorClass.SERVER_5XX):
            assert policy_for(err)["count_as_failure"] is True

    def test_cancelled_and_dedupe_not_counted(self) -> None:
        assert policy_for(ErrorClass.CANCELLED)["count_as_failure"] is False
        assert policy_for(ErrorClass.DEDUPE_CONFLICT)["count_as_failure"] is False

    def test_unknown_defaults_to_no_retry(self) -> None:
        pol = policy_for(ErrorClass.UNKNOWN)
        assert pol["retryable"] is False
        assert pol["alert_level"] == "critical"
