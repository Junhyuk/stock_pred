# DB_SCHEMA.md

SQLite MVP schema.

## Core tables

```sql
CREATE TABLE market_session (
    session_date TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    is_open INTEGER NOT NULL,
    open_at TEXT,
    close_at TEXT
);

CREATE TABLE index_eod (
    index_code TEXT NOT NULL,
    session_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    turnover REAL,
    source TEXT NOT NULL,
    published_at TEXT,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY(index_code, session_date)
);

CREATE TABLE investor_flow (
    market TEXT NOT NULL,
    session_date TEXT NOT NULL,
    investor_type TEXT NOT NULL,
    buy_value REAL,
    sell_value REAL,
    net_value REAL,
    source TEXT NOT NULL,
    published_at TEXT,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY(market, session_date, investor_type)
);

CREATE TABLE derivatives_eod (
    instrument_code TEXT NOT NULL,
    session_date TEXT NOT NULL,
    close REAL,
    volume REAL,
    open_interest REAL,
    basis REAL,
    foreign_net REAL,
    source TEXT NOT NULL,
    published_at TEXT,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY(instrument_code, session_date)
);

CREATE TABLE macro_series (
    series_code TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    value REAL,
    published_at TEXT,
    revision_no INTEGER DEFAULT 0,
    source TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY(series_code, observation_date, revision_no)
);

CREATE TABLE news_event (
    event_id TEXT PRIMARY KEY,
    published_at TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT,
    topic TEXT,
    sentiment REAL,
    uncertainty REAL,
    entity_json TEXT,
    cluster_id TEXT,
    ingested_at TEXT NOT NULL
);

CREATE TABLE disclosure_event (
    receipt_no TEXT PRIMARY KEY,
    corp_code TEXT,
    stock_code TEXT,
    published_at TEXT NOT NULL,
    report_name TEXT,
    event_type TEXT,
    payload_json TEXT,
    ingested_at TEXT NOT NULL
);

CREATE TABLE feature_snapshot (
    index_code TEXT NOT NULL,
    prediction_time TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    features_json TEXT NOT NULL,
    PRIMARY KEY(index_code, prediction_time, feature_version)
);

CREATE TABLE model_registry (
    model_version TEXT PRIMARY KEY,
    model_type TEXT NOT NULL,
    horizon TEXT NOT NULL,
    trained_from TEXT,
    trained_to TEXT,
    feature_version TEXT NOT NULL,
    metrics_json TEXT,
    artifact_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE prediction (
    index_code TEXT NOT NULL,
    prediction_time TEXT NOT NULL,
    horizon TEXT NOT NULL,
    model_version TEXT NOT NULL,
    p_up REAL,
    p_flat REAL,
    p_down REAL,
    expected_return REAL,
    expected_volatility REAL,
    signal TEXT,
    confidence REAL,
    data_quality REAL,
    reason_codes_json TEXT,
    PRIMARY KEY(index_code, prediction_time, horizon, model_version)
);

CREATE TABLE realized_outcome (
    index_code TEXT NOT NULL,
    prediction_time TEXT NOT NULL,
    horizon TEXT NOT NULL,
    realized_return REAL,
    realized_direction TEXT,
    evaluated_at TEXT NOT NULL,
    PRIMARY KEY(index_code, prediction_time, horizon)
);
```

## SQLite rules

- WAL mode
- foreign_keys=ON
- busy_timeout
- transaction 사용
- batch insert
- indexes는 query pattern 기반
- raw payload는 필요시 압축 파일/parquet와 hybrid 사용

대용량 tick 데이터가 커지면 SQLite를 무조건 고집하지 않는다.
DuckDB/Parquet/PostgreSQL/Timescale 등으로 migration을 검토한다.
