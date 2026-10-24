"""Copy point-in-time DuckDB history into the SQLite research store."""

from __future__ import annotations

from typing import Any

import pandas as pd

from roboquant.db import table_exists
from roboquant.index_forecast.sqlite_store import upsert_frame, utcnow_text

INDEX_MAP = {
    "KOSPI": "KOSPI",
    "1001": "KOSPI",
    "KOSDAQ": "KOSDAQ",
    "2001": "KOSDAQ",
    "KOSPI100": "KOSPI100",
    "1034": "KOSPI100",
    "KOSDAQ100": "KOSDAQ100",
}
MACRO_SYMBOLS = {
    "^IXIC": "US_NASDAQ",
    "^SOX": "US_SOX",
    "^VIX": "US_VIX",
    "^GSPC": "US_SPX",
    "USDKRW=X": "USDKRW",
    "EWY": "EWY",
    "KORU": "KORU",
}


def export_duckdb_history_to_sqlite(duckdb_conn, sqlite_conn) -> dict[str, Any]:
    """Copy KOSPI/KOSDAQ/KOSPI100/KOSDAQ100 closes, overnight macros, and cash flows when present."""

    ingested_at = utcnow_text()
    index_rows = _export_index_eod(duckdb_conn, sqlite_conn, ingested_at)
    macro_rows = _export_macro_series(duckdb_conn, sqlite_conn, ingested_at)
    flow_rows = _export_investor_flow(duckdb_conn, sqlite_conn, ingested_at)
    session_rows = _export_market_sessions(sqlite_conn)
    return {
        "index_eod_rows": index_rows,
        "macro_series_rows": macro_rows,
        "investor_flow_rows": flow_rows,
        "market_session_rows": session_rows,
        "kospi100_status": _index_status(sqlite_conn, "KOSPI100"),
        "kospi_status": _index_status(sqlite_conn, "KOSPI"),
        "kosdaq_status": _index_status(sqlite_conn, "KOSDAQ"),
        "kosdaq100_status": _index_status(sqlite_conn, "KOSDAQ100"),
    }


def _export_index_eod(duckdb_conn, sqlite_conn, ingested_at: str) -> int:
    if not table_exists(duckdb_conn, "benchmark_daily"):
        return 0
    raw = duckdb_conn.execute(
        """
        SELECT date, benchmark, open, high, low, close, volume, trading_value, collected_at
        FROM benchmark_daily
        WHERE benchmark IN ('KOSPI', 'KOSDAQ', 'KOSPI100', 'KOSDAQ100', '1001', '2001', '1034')
        ORDER BY date, benchmark
        """
    ).fetchdf()
    if raw.empty:
        return 0
    rows: list[dict[str, Any]] = []
    for item in raw.itertuples(index=False):
        code = INDEX_MAP.get(str(item.benchmark).upper())
        if code is None:
            continue
        rows.append(
            {
                "index_code": code,
                "session_date": _date_text(item.date),
                "open": _number(item.open),
                "high": _number(item.high),
                "low": _number(item.low),
                "close": _number(item.close),
                "volume": _number(item.volume),
                "turnover": _number(item.trading_value),
                "source": "duckdb_benchmark_daily",
                "published_at": _date_text(item.date),
                "ingested_at": ingested_at,
            }
        )
    if not rows:
        return 0
    return upsert_frame(
        sqlite_conn,
        "index_eod",
        pd.DataFrame(rows),
        ["index_code", "session_date"],
    )


def _export_macro_series(duckdb_conn, sqlite_conn, ingested_at: str) -> int:
    rows: list[dict[str, Any]] = []
    if table_exists(duckdb_conn, "global_market_daily"):
        symbols = list(MACRO_SYMBOLS)
        placeholders = ", ".join("?" for _ in symbols)
        raw = duckdb_conn.execute(
            f"""
            SELECT trade_date, symbol, close, return_1d, source_name, source_timestamp, ingested_at
            FROM global_market_daily
            WHERE symbol IN ({placeholders})
            ORDER BY trade_date, symbol
            """,
            symbols,
        ).fetchdf()
        for item in raw.itertuples(index=False):
            series = MACRO_SYMBOLS.get(str(item.symbol))
            if series is None:
                continue
            value = _number(item.return_1d)
            if value is None:
                continue
            rows.append(
                {
                    "series_code": series,
                    "observation_date": _date_text(item.trade_date),
                    "value": value,
                    "published_at": _timestamp_text(item.source_timestamp) or _date_text(item.trade_date),
                    "revision_no": 0,
                    "source": str(item.source_name or "global_market_daily"),
                    "ingested_at": ingested_at,
                }
            )
    if table_exists(duckdb_conn, "koru_korea_linkage"):
        fx = duckdb_conn.execute(
            """
            SELECT trade_date, usdkrw_change_pct
            FROM koru_korea_linkage
            WHERE usdkrw_change_pct IS NOT NULL
            ORDER BY trade_date
            """
        ).fetchdf()
        for item in fx.itertuples(index=False):
            rows.append(
                {
                    "series_code": "USDKRW",
                    "observation_date": _date_text(item.trade_date),
                    "value": _number(item.usdkrw_change_pct),
                    "published_at": _date_text(item.trade_date),
                    "revision_no": 0,
                    "source": "koru_korea_linkage",
                    "ingested_at": ingested_at,
                }
            )
    if not rows:
        return 0
    frame = pd.DataFrame(rows).drop_duplicates(["series_code", "observation_date", "revision_no"], keep="last")
    return upsert_frame(
        sqlite_conn,
        "macro_series",
        frame,
        ["series_code", "observation_date", "revision_no"],
    )


def _export_investor_flow(duckdb_conn, sqlite_conn, ingested_at: str) -> int:
    rows: list[dict[str, Any]] = []
    if table_exists(duckdb_conn, "market_investor_flows_daily"):
        raw = duckdb_conn.execute(
            """
            SELECT trade_date, market, investor, buy_value, sell_value, net_value, source
            FROM market_investor_flows_daily
            WHERE is_final = TRUE
              AND market IN ('KOSPI', 'KOSDAQ')
              AND investor IN ('외국인', '기관합계', '개인')
            ORDER BY trade_date, investor
            """
        ).fetchdf()
        for item in raw.itertuples(index=False):
            rows.append(
                {
                    "market": str(item.market),
                    "session_date": _date_text(item.trade_date),
                    "investor_type": str(item.investor),
                    "buy_value": _number(item.buy_value),
                    "sell_value": _number(item.sell_value),
                    "net_value": _number(item.net_value),
                    "source": str(item.source or "pykrx"),
                    "published_at": _date_text(item.trade_date),
                    "ingested_at": ingested_at,
                }
            )
    if not rows:
        return 0
    return upsert_frame(
        sqlite_conn,
        "investor_flow",
        pd.DataFrame(rows),
        ["market", "session_date", "investor_type"],
    )


def _export_market_sessions(sqlite_conn) -> int:
    dates = pd.read_sql_query(
        "SELECT DISTINCT session_date FROM index_eod WHERE index_code = 'KOSPI' ORDER BY session_date",
        sqlite_conn,
    )
    if dates.empty:
        return 0
    frame = pd.DataFrame(
        {
            "session_date": dates["session_date"],
            "market": "KRX",
            "is_open": 1,
            "open_at": None,
            "close_at": None,
        }
    )
    return upsert_frame(sqlite_conn, "market_session", frame, ["session_date"])


def _index_status(sqlite_conn, index_code: str) -> str:
    row = sqlite_conn.execute(
        "SELECT COUNT(*) AS n, MIN(session_date) AS mn, MAX(session_date) AS mx FROM index_eod WHERE index_code = ?",
        [index_code],
    ).fetchone()
    if row is None or int(row["n"] or 0) == 0:
        return "not_ready"
    return "ready"


def _date_text(value: Any) -> str | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def _timestamp_text(value: Any) -> str | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.isoformat(timespec="seconds")


def _number(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not pd.notna(number):
        return None
    return number
