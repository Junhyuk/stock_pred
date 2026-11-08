"""SQLite MVP store for the KOSPI/KOSPI100 research track."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

DEFAULT_SQLITE_PATH = Path("data/processed/index_forecast.sqlite")
PRODUCTION_WEIGHT = 0.0

SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS market_session (
        session_date TEXT PRIMARY KEY,
        market TEXT NOT NULL,
        is_open INTEGER NOT NULL,
        open_at TEXT,
        close_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_eod (
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS investor_flow (
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS derivatives_eod (
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS macro_series (
        series_code TEXT NOT NULL,
        observation_date TEXT NOT NULL,
        value REAL,
        published_at TEXT,
        revision_no INTEGER DEFAULT 0,
        source TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        PRIMARY KEY(series_code, observation_date, revision_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news_event (
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS disclosure_event (
        receipt_no TEXT PRIMARY KEY,
        corp_code TEXT,
        stock_code TEXT,
        published_at TEXT NOT NULL,
        report_name TEXT,
        event_type TEXT,
        payload_json TEXT,
        ingested_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feature_snapshot (
        index_code TEXT NOT NULL,
        prediction_time TEXT NOT NULL,
        feature_version TEXT NOT NULL,
        features_json TEXT NOT NULL,
        PRIMARY KEY(index_code, prediction_time, feature_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_registry (
        model_version TEXT PRIMARY KEY,
        model_type TEXT NOT NULL,
        horizon TEXT NOT NULL,
        trained_from TEXT,
        trained_to TEXT,
        feature_version TEXT NOT NULL,
        metrics_json TEXT,
        artifact_path TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prediction (
        index_code TEXT NOT NULL,
        prediction_time TEXT NOT NULL,
        horizon TEXT NOT NULL,
        model_version TEXT NOT NULL,
        p_up REAL,
        p_flat REAL,
        p_down REAL,
        expected_return REAL,
        expected_volatility REAL,
        range_low REAL,
        range_high REAL,
        signal TEXT,
        confidence REAL,
        data_quality REAL,
        production_weight REAL NOT NULL DEFAULT 0.0,
        reason_codes_json TEXT,
        PRIMARY KEY(index_code, prediction_time, horizon, model_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS realized_outcome (
        index_code TEXT NOT NULL,
        prediction_time TEXT NOT NULL,
        horizon TEXT NOT NULL,
        realized_return REAL,
        realized_direction TEXT,
        evaluated_at TEXT NOT NULL,
        PRIMARY KEY(index_code, prediction_time, horizon)
    )
    """,
]

INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_index_eod_date ON index_eod(session_date, index_code)",
    "CREATE INDEX IF NOT EXISTS idx_macro_series_date ON macro_series(observation_date, series_code)",
    "CREATE INDEX IF NOT EXISTS idx_investor_flow_date ON investor_flow(session_date, market)",
    "CREATE INDEX IF NOT EXISTS idx_prediction_latest ON prediction(index_code, prediction_time, horizon)",
]


def connect_index_forecast(
    path: str | Path,
    *,
    initialize_schema: bool = True,
) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    if initialize_schema:
        ensure_index_forecast_schema(conn)
    return conn


def ensure_index_forecast_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_SQL:
        conn.execute(statement)
    for statement in INDEX_SQL:
        conn.execute(statement)
    conn.commit()


def utcnow_text() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def upsert_frame(
    conn: sqlite3.Connection,
    table: str,
    frame: pd.DataFrame,
    key_columns: list[str],
) -> int:
    if frame is None or frame.empty:
        return 0
    records = frame.where(pd.notna(frame), None).to_dict(orient="records")
    columns = list(records[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    update_sql = ", ".join(f"{column}=excluded.{column}" for column in columns if column not in key_columns)
    sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"
    if update_sql:
        conflict = ", ".join(key_columns)
        sql = f"{sql} ON CONFLICT({conflict}) DO UPDATE SET {update_sql}"
    conn.executemany(sql, [[row.get(column) for column in columns] for row in records])
    conn.commit()
    return len(records)


def insert_immutable(
    conn: sqlite3.Connection,
    table: str,
    frame: pd.DataFrame,
    key_columns: list[str],
) -> int:
    if frame is None or frame.empty:
        return 0
    records = frame.where(pd.notna(frame), None).to_dict(orient="records")
    columns = list(records[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    conflict = ", ".join(key_columns)
    sql = (
        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict}) DO NOTHING"
    )
    conn.executemany(sql, [[row.get(column) for column in columns] for row in records])
    conn.commit()
    return len(records)


def read_table(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    return pd.read_sql_query(f"SELECT * FROM {table}", conn)


def table_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0] if row else 0)


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
