"""Append-only, timezone-aware observations for index forecast research."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pandas as pd

YIELDS = frozenset(f"{country}_YIELD_{tenor}" for country in ("KR", "US") for tenor in ("1Y", "3Y", "10Y"))
FLOWS = frozenset(("FOREIGN_NET", "INSTITUTION_NET", "RETAIL_NET", "MARKET_TURNOVER"))
MARGIN = "MARGIN_CREDIT_BALANCE"
KOFIA_CREDIT_SOURCE = "kofia_freesis_margin_credit"
MARKETS = frozenset(("KOSPI", "KOSDAQ"))
COLUMNS = (
    "market", "series", "observation_date", "value", "unit", "source",
    "received_at", "published_at", "is_final", "availability_policy",
)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS session_input_observations (
    version_id TEXT NOT NULL,
    market TEXT NOT NULL,
    series TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    source TEXT NOT NULL,
    received_at TEXT NOT NULL,
    published_at TEXT,
    is_final INTEGER CHECK (is_final IN (0, 1)),
    availability_policy TEXT NOT NULL,
    PRIMARY KEY (version_id, received_at)
)""",
    """CREATE INDEX IF NOT EXISTS idx_session_inputs_observed
       ON session_input_observations (received_at, market, series, observation_date)""",
    """CREATE TRIGGER IF NOT EXISTS session_inputs_no_update BEFORE UPDATE ON session_input_observations
       BEGIN SELECT RAISE(ABORT, 'append-only observations'); END""",
    """CREATE TRIGGER IF NOT EXISTS session_inputs_no_delete BEFORE DELETE ON session_input_observations
       BEGIN SELECT RAISE(ABORT, 'append-only observations'); END""",
)


def _utc(value) -> str:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("Explicit timezone required for receipt, publication and cutoff")
    stamp = stamp.tz_convert("UTC")
    return stamp.isoformat(timespec="microseconds")


def _text(value) -> str:
    return "" if pd.isna(value) else str(value).strip()


def _validated(row: dict) -> tuple:
    market, series, unit, source = (_text(row[column]) for column in ("market", "series", "unit", "source"))
    if series not in YIELDS | FLOWS | {MARGIN}:
        raise ValueError(f"Unsupported series: {series}")
    allowed = {"GLOBAL"} if series in YIELDS else MARKETS | {"GLOBAL"} if series == MARGIN else MARKETS
    if market not in allowed:
        raise ValueError(f"Invalid market scope for {series}: {market}")
    if unit != ("percent" if series in YIELDS else "KRW"):
        raise ValueError(f"Invalid unit for {series}: {unit}")
    policy = _text(row.get("availability_policy")) or "observed"
    allowed_policies = {"observed"}
    if series in YIELDS:
        allowed_policies.add("historical_dated_yield")
    if series == MARGIN and source == KOFIA_CREDIT_SOURCE:
        allowed_policies.add("historical_dated_credit")
    if policy not in allowed_policies:
        raise ValueError(f"Invalid availability policy for {series}")
    observed_day = _text(row["observation_date"])
    if date.fromisoformat(observed_day).isoformat() != observed_day or not source:
        raise ValueError("A canonical observation date and source are required")
    value = float(row["value"])
    if not math.isfinite(value) or (series in {MARGIN, "MARKET_TURNOVER"} and value < 0):
        raise ValueError(f"Invalid value for {series}")
    received = _utc(row["received_at"])
    # A foreign observation date can precede its Korean receipt date.
    if pd.Timestamp(received).tz_convert("Asia/Seoul").date().isoformat() < observed_day:
        raise ValueError("Receipt cannot precede the observation date")
    publication = _text(row["published_at"])
    published = _utc(publication) if publication else None
    if published and published > received:
        raise ValueError("Publication must not be after receipt")
    final_text = _text(row["is_final"]).lower()
    if final_text not in {"", "true", "false", "1", "0"}:
        raise ValueError("is_final must be true, false, or unknown (empty)")
    final = int(final_text in {"true", "1"}) if final_text else None
    # Match identical versions across downloads. A newer receipt adds no
    # evidence, while a newly discovered earlier receipt is appended.
    identity = (market, series, observed_day, value, unit, source, published, final, policy)
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return (digest, market, series, observed_day, value, unit, source, received, published, final, policy)


def append_inputs(path: Path, observations: pd.DataFrame) -> int:
    missing = set(COLUMNS[:-1]) - set(observations.columns)
    if missing:
        raise ValueError(f"Missing observation columns: {', '.join(sorted(missing))}")
    validated = [_validated(row) for row in observations.to_dict("records")]
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
        if set(tables) - {("session_input_observations",)}:
            raise ValueError("Use a dedicated session inputs SQLite database")
        for statement in SCHEMA:
            conn.execute(statement)
        cursor = conn.executemany(
            """INSERT INTO session_input_observations
               SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
               WHERE NOT EXISTS (
                   SELECT 1 FROM session_input_observations WHERE version_id = ? AND received_at <= ?
               )""",
            (row + (row[0], row[7]) for row in sorted(validated, key=lambda row: row[7])),
        )
        return cursor.rowcount


def read_inputs(path: Path, *, as_of) -> pd.DataFrame:
    cutoff = _utc(as_of)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = pd.read_sql_query(
            f"""SELECT {', '.join(COLUMNS)} FROM session_input_observations
                WHERE received_at <= ? ORDER BY market, series, observation_date, received_at, version_id""",
            conn,
            params=(cutoff,),
        )
    rows["is_final"] = pd.Series(
        [None if pd.isna(value) else bool(value) for value in rows["is_final"]],
        index=rows.index,
        dtype=object,
    )
    return rows
