# tests/unit/test_retention.py
"""保留窗口计算（§8.4）。纯函数 + 时间注入。

注意：本模块只算 cutoff，**不执行删除**；执行顺序铁律
（rollup → 打 rolled_up 标记 → 删 change → vacuum）由 storage/maintenance.py 保证，
该顺序在 tests/integration 里用真实 sqlite 验证。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from market_watch.domain.retention import (
    RETENTION,
    bucket_key,
    cutoff_for,
    cutoffs,
    hour_buckets_between,
    time_column,
)

_NOW = datetime(2026, 3, 15, 10, 30, 45, tzinfo=timezone.utc)


class TestCutoff:
    def test_price_change_is_72h(self) -> None:
        assert cutoff_for("price_change", _NOW) == "2026-03-12T10:30:45Z"

    def test_price_hourly_is_180d(self) -> None:
        # 2026-03-15 - 180d = 2025-09-16
        assert cutoff_for("price_hourly", _NOW) == "2025-09-16T10:30:45Z"

    def test_latest_and_announcement_never_deleted(self) -> None:
        """最新态与公告永久保留 —— cutoff 必须是 None，而不是「很久以前」。"""
        assert cutoff_for("price_latest", _NOW) is None
        assert cutoff_for("announcement", _NOW) is None

    def test_unknown_table_is_none(self) -> None:
        assert cutoff_for("no_such_table", _NOW) is None

    def test_cutoffs_excludes_forever_tables(self) -> None:
        got = cutoffs(_NOW)
        assert "price_change" in got
        assert "price_latest" not in got
        assert "announcement" not in got
        assert len(got) == len([w for w in RETENTION.values() if w is not None])

    def test_all_cutoffs_are_iso_z(self) -> None:
        for value in cutoffs(_NOW).values():
            assert value.endswith("Z") and "T" in value and "." not in value


class TestTimeColumn:
    def test_known_columns(self) -> None:
        assert time_column("price_change") == "observed_at"
        assert time_column("price_hourly") == "bucket_ts"
        assert time_column("fetch_log") == "ts"
        assert time_column("data_reject") == "created_at"
        assert time_column("alert_event") == "fired_at"

    def test_unknown_defaults_to_observed_at(self) -> None:
        assert time_column("whatever") == "observed_at"


class TestBuckets:
    def test_bucket_key_truncates_to_hour(self) -> None:
        assert bucket_key(_NOW) == "2026-03-15T10:00:00Z"

    def test_hour_buckets_between_is_half_open(self) -> None:
        start = datetime(2026, 3, 15, 10, 45, tzinfo=timezone.utc)
        end = datetime(2026, 3, 15, 13, 0, tzinfo=timezone.utc)
        got = hour_buckets_between(start, end)
        assert got == [
            "2026-03-15T10:00:00Z",
            "2026-03-15T11:00:00Z",
            "2026-03-15T12:00:00Z",
        ]

    def test_empty_range(self) -> None:
        t = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        assert hour_buckets_between(t, t) == []

    def test_crosses_day_boundary(self) -> None:
        start = datetime(2026, 3, 15, 23, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=2)
        assert hour_buckets_between(start, end) == [
            "2026-03-15T23:00:00Z",
            "2026-03-16T00:00:00Z",
        ]
