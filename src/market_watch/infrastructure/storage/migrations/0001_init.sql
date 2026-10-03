-- src/market_watch/infrastructure/storage/migrations/0001_init.sql
-- 全量 DDL，13 张 STRICT 表（架构 §8.2）。
--
-- ★ Phase 1 实跑发现的缺陷修复（必须保留，不得"优化"掉）：
--   price_change 增加 `rolled_up INTEGER NOT NULL DEFAULT 0` 与部分索引
--   `idx_price_change_pending ... WHERE rolled_up = 0`。
--   原因：若进程在 rollup 与 delete 之间崩溃，重跑会把同一批行再滚一次，
--   price_hourly.samples 翻倍。顺序铁律见 maintenance.py：
--   先 rollup → 打 rolled_up 标记 → 再删 change → 最后 vacuum。
--
-- 迁移纪律（§14 P2）：禁止修改已应用的迁移文件；加列请开 0002_*.sql，
-- 并在 meta.schema_version 记录版本。

CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

CREATE TABLE IF NOT EXISTS source_state (
    source_id            TEXT PRIMARY KEY,
    last_cursor          TEXT,
    last_cycle_id        TEXT,
    last_run_at          TEXT,
    last_success_at      TEXT,
    last_error_class     TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    robots_checked_at    TEXT,
    robots_allowed       INTEGER NOT NULL DEFAULT 1,
    updated_at           TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS items (
    item_id      INTEGER PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    item_type    TEXT,
    rarity       TEXT,
    whitelisted  INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS watchlist (
    item_id  INTEGER PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE,
    reason   TEXT,
    added_at TEXT NOT NULL
) STRICT;

-- 全市场「最新态」：每 item 一行，每周期 upsert，行数恒定 ≈ 27997
CREATE TABLE IF NOT EXISTS price_latest (
    item_id         INTEGER PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE,
    observed_at     TEXT NOT NULL,
    cycle_id        TEXT NOT NULL,
    buy_unit_price  INTEGER,
    buy_quantity    INTEGER,
    sell_unit_price INTEGER,
    sell_quantity   INTEGER,
    whitelisted     INTEGER NOT NULL DEFAULT 1,
    quality         TEXT NOT NULL DEFAULT 'COMPLETE' CHECK (quality IN ('COMPLETE','PARTIAL'))
) STRICT, WITHOUT ROWID;

-- 变更事件流：仅在数值与上一次相比有变化时追加（进程内 change gate）
CREATE TABLE IF NOT EXISTS price_change (
    change_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    observed_at     TEXT NOT NULL,
    cycle_id        TEXT NOT NULL,
    buy_unit_price  INTEGER,
    buy_quantity    INTEGER,
    sell_unit_price INTEGER,
    sell_quantity   INTEGER,
    prev_sell_price INTEGER,
    delta_pct       REAL,
    rolled_up       INTEGER NOT NULL DEFAULT 0   -- 是否已滚入 price_hourly；防止 rollup 重复执行造成 samples 翻倍
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_price_change ON price_change(item_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_price_change_item_time ON price_change(item_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_price_change_pending ON price_change(observed_at) WHERE rolled_up = 0;

-- 原始挂单档位：仅观测清单内的 item 采集
CREATE TABLE IF NOT EXISTS listing_level (
    item_id     INTEGER NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    side        TEXT NOT NULL CHECK (side IN ('buy','sell')),
    unit_price  INTEGER NOT NULL CHECK (unit_price > 0),
    quantity    INTEGER NOT NULL CHECK (quantity >= 0),
    listings    INTEGER NOT NULL CHECK (listings >= 1),
    observed_at TEXT NOT NULL,
    cycle_id    TEXT NOT NULL,
    PRIMARY KEY (item_id, side, unit_price, observed_at)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_listing_time ON listing_level(item_id, side, observed_at DESC);

-- 小时 K 线 rollup：只做「观测清单 + TopN 成交额」的物品，长期保留
CREATE TABLE IF NOT EXISTS price_hourly (
    item_id     INTEGER NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    bucket_ts   TEXT NOT NULL,                 -- 'YYYY-MM-DDTHH:00:00Z'
    open_sell   INTEGER, high_sell INTEGER, low_sell INTEGER, close_sell INTEGER,
    avg_sell    REAL, vwap_sell REAL, spread REAL,
    depth_buy   INTEGER NOT NULL DEFAULT 0, depth_sell INTEGER NOT NULL DEFAULT 0,
    samples     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, bucket_ts)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_price_hourly_bucket ON price_hourly(bucket_ts DESC);

CREATE TABLE IF NOT EXISTS announcement (
    source_id     TEXT NOT NULL,
    guid          TEXT NOT NULL,
    title         TEXT NOT NULL,
    link          TEXT NOT NULL,
    published_at  TEXT,
    summary       TEXT,
    content_hash  TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (source_id, guid)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_announcement_published ON announcement(published_at DESC);

CREATE TABLE IF NOT EXISTS alert_event (
    alert_id        TEXT PRIMARY KEY,
    dedupe_key      TEXT NOT NULL,
    rule            TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    item_id         INTEGER,
    severity        TEXT NOT NULL CHECK (severity IN ('info','warn','critical')),
    metric          TEXT NOT NULL,
    prev_value      REAL, curr_value REAL, delta_pct REAL,
    message         TEXT NOT NULL,
    fired_at        TEXT NOT NULL,
    cooldown_until  TEXT NOT NULL,
    acknowledged    INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_dedupe ON alert_event(dedupe_key, cooldown_until);
CREATE INDEX IF NOT EXISTS idx_alert_fired ON alert_event(fired_at DESC);

-- 数据质量隔离区：被一致性规则拦下的记录，永不见异动检测，也不静默丢弃
CREATE TABLE IF NOT EXISTS data_reject (
    reject_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id    TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    item_id     INTEGER,
    rule        TEXT NOT NULL,
    field       TEXT NOT NULL,
    observed    TEXT,
    expected    TEXT,
    severity    TEXT NOT NULL,
    payload     TEXT,          -- 截断到 4096 字符的原文片段
    created_at  TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_reject_created ON data_reject(created_at DESC);

-- 合规审计：每一次出站请求的留痕
CREATE TABLE IF NOT EXISTS fetch_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id     TEXT,
    host         TEXT NOT NULL,
    path         TEXT NOT NULL,
    method       TEXT NOT NULL,
    status_code  INTEGER,
    attempts     INTEGER NOT NULL,
    duration_ms  REAL,
    resp_bytes   INTEGER,
    error_class  TEXT,
    robots_ok    INTEGER NOT NULL DEFAULT 1,
    ua           TEXT NOT NULL,
    ts           TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_fetch_log_ts ON fetch_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_fetch_log_host_ts ON fetch_log(host, ts DESC);
