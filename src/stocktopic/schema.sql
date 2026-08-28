PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trading_calendar (
    cal_date TEXT PRIMARY KEY,
    is_open INTEGER NOT NULL CHECK (is_open IN (0, 1)),
    pretrade_date TEXT,
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT NOT NULL,
    exchange TEXT,
    market TEXT,
    industry TEXT,
    list_date TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    excluded_reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stocks_active ON stocks(active, market);

CREATE TABLE IF NOT EXISTS stock_tags (
    code TEXT NOT NULL REFERENCES stocks(code) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    tag_type TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (code, tag, source)
);

CREATE INDEX IF NOT EXISTS idx_stock_tags_tag ON stock_tags(tag, tag_type);

CREATE TABLE IF NOT EXISTS kpl_events (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    board_tag TEXT NOT NULL,
    themes_json TEXT NOT NULL,
    status TEXT,
    limit_up_time TEXT,
    open_time TEXT,
    last_limit_time TEXT,
    limit_reason TEXT,
    pct_change REAL,
    realtime_pct_change REAL,
    amount REAL,
    raw_json TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY(trade_date, code, board_tag)
);

CREATE INDEX IF NOT EXISTS idx_kpl_events_date_tag ON kpl_events(trade_date, board_tag);

CREATE TABLE IF NOT EXISTS daily_limits (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    pre_close REAL,
    upper_limit REAL,
    lower_limit REAL,
    PRIMARY KEY (trade_date, code)
);

CREATE TABLE IF NOT EXISTS stock_daily_metrics (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    close REAL,
    turnover_rate REAL,
    volume_ratio REAL,
    float_share REAL,
    total_mv REAL,
    circ_mv REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, code)
);

CREATE INDEX IF NOT EXISTS idx_daily_metrics_code_date
ON stock_daily_metrics(code, trade_date DESC);

CREATE TABLE IF NOT EXISTS quote_snapshots (
    trade_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    pre_close REAL NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    amount REAL NOT NULL,
    trades INTEGER NOT NULL,
    provider_trade_time TEXT,
    pct_change REAL NOT NULL,
    PRIMARY KEY (trade_date, slot, code)
);

CREATE INDEX IF NOT EXISTS idx_quotes_code_time
ON quote_snapshots(code, captured_at DESC);

CREATE TABLE IF NOT EXISTS anomaly_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    direction TEXT NOT NULL,
    severity REAL NOT NULL,
    pct_change REAL NOT NULL,
    change_5m REAL NOT NULL,
    amount_delta REAL NOT NULL,
    trade_delta INTEGER NOT NULL,
    is_hard_event INTEGER NOT NULL,
    event_types_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    UNIQUE(captured_at, code, direction)
);

CREATE INDEX IF NOT EXISTS idx_anomaly_time ON anomaly_events(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_code ON anomaly_events(code, captured_at DESC);

CREATE TABLE IF NOT EXISTS candidate_themes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    provisional_name TEXT NOT NULL,
    suggested_name TEXT,
    final_name TEXT,
    shared_tag TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    direction TEXT NOT NULL DEFAULT 'positive',
    discovered_at TEXT NOT NULL,
    day1_date TEXT NOT NULL,
    confirmed_at TEXT,
    merged_into_id INTEGER REFERENCES candidate_themes(id),
    discovery_reason TEXT NOT NULL,
    catalyst_strength REAL NOT NULL DEFAULT 0,
    catalyst_duration TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    admission_status TEXT NOT NULL DEFAULT 'legacy',
    admission_reason TEXT,
    admission_reviewed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_themes_status ON candidate_themes(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS theme_members (
    theme_id INTEGER NOT NULL REFERENCES candidate_themes(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    membership_source TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    role TEXT,
    PRIMARY KEY(theme_id, code)
);

CREATE TABLE IF NOT EXISTS theme_scores (
    theme_id INTEGER NOT NULL REFERENCES candidate_themes(id) ON DELETE CASCADE,
    calculated_at TEXT NOT NULL,
    heat REAL NOT NULL,
    persistence REAL NOT NULL,
    entry_risk REAL NOT NULL,
    lifecycle TEXT NOT NULL,
    confidence REAL NOT NULL,
    leader_code TEXT,
    leader_influence REAL,
    leader_theme_divergence INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL,
    PRIMARY KEY(theme_id, calculated_at)
);

CREATE INDEX IF NOT EXISTS idx_scores_latest ON theme_scores(theme_id, calculated_at DESC);

CREATE TABLE IF NOT EXISTS theme_cohorts (
    theme_id INTEGER NOT NULL REFERENCES candidate_themes(id) ON DELETE CASCADE,
    trade_date TEXT NOT NULL,
    board_level INTEGER NOT NULL,
    code TEXT NOT NULL,
    outcome TEXT,
    next_day_return REAL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(theme_id, trade_date, code)
);

CREATE TABLE IF NOT EXISTS ai_explanations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id INTEGER NOT NULL REFERENCES candidate_themes(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    suggested_name TEXT,
    explanation TEXT NOT NULL,
    catalyst_summary TEXT,
    catalyst_duration TEXT,
    merge_suggestions_json TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    raw_response_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS theme_catalysts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id INTEGER NOT NULL REFERENCES candidate_themes(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_name TEXT,
    source_url TEXT,
    published_at TEXT,
    catalyst_type TEXT NOT NULL DEFAULT 'update',
    evidence_level TEXT NOT NULL DEFAULT 'inference',
    captured_at TEXT NOT NULL,
    UNIQUE(theme_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_theme_catalysts_time
ON theme_catalysts(theme_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS theme_admission_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id INTEGER NOT NULL REFERENCES candidate_themes(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    is_new_theme INTEGER NOT NULL,
    novelty_confidence REAL NOT NULL,
    catalyst_confidence REAL NOT NULL,
    expected_duration_days INTEGER NOT NULL,
    leader_candidate_code TEXT,
    leader_upside_scenario_pct REAL NOT NULL,
    admitted INTEGER NOT NULL,
    decision_reason TEXT NOT NULL,
    historical_matches_json TEXT NOT NULL,
    proposed_members_json TEXT NOT NULL,
    validated_members_json TEXT NOT NULL,
    raw_response_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_admission_reviews_theme
ON theme_admission_reviews(theme_id, created_at DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    theme_id INTEGER,
    code TEXT,
    pushed_wecom INTEGER NOT NULL DEFAULT 0,
    push_error TEXT
);

CREATE TABLE IF NOT EXISTS service_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_service_runs_job ON service_runs(job_name, started_at DESC);
