"""Official index ingest. Never reconstruct from current constituents."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from roboquant.db import append_dedup_table
from roboquant.index_forecast.indexes import get_official_index
from roboquant.index_forecast.sqlite_store import upsert_frame, utcnow_text

KOSPI100_CODE = "1034"
KOSPI100_NAME = "KOSPI100"


class OfficialIndexIngestError(RuntimeError):
    """Raised when an official index series cannot be fetched."""


Kospi100IndexIngestError = OfficialIndexIngestError


def ingest_kospi100_index(
    *,
    start_date: str,
    end_date: str | None = None,
    duckdb_conn=None,
    sqlite_conn=None,
    stock_api: Any | None = None,
) -> dict[str, Any]:
    """Fetch pykrx index 1034 (KOSPI 100). FDR KS11 and code 1028 are forbidden."""

    return ingest_official_index(
        "KOSPI100",
        start_date=start_date,
        end_date=end_date,
        duckdb_conn=duckdb_conn,
        sqlite_conn=sqlite_conn,
        stock_api=stock_api,
    )


def ingest_official_index(
    index_code: str,
    *,
    start_date: str,
    end_date: str | None = None,
    duckdb_conn=None,
    sqlite_conn=None,
    stock_api: Any | None = None,
) -> dict[str, Any]:
    spec = get_official_index(index_code)
    if spec.pykrx_code is None:
        return {
            "status": "not_ready",
            "reason": "official_kosdaq100_index_not_listed_no_kosdaq150_substitution",
            "rows": 0,
            "index_code": spec.code,
            "pykrx_code": None,
        }
    try:
        frame = fetch_official_index_ohlcv(
            spec.code,
            start_date,
            end_date,
            stock_api=stock_api,
        )
    except Exception as exc:
        return {
            "status": "not_ready",
            "reason": f"{spec.code.lower()}_index_fetch_failed: {exc}",
            "rows": 0,
            "index_code": spec.code,
            "pykrx_code": spec.pykrx_code,
        }
    if frame.empty:
        return {
            "status": "not_ready",
            "reason": f"{spec.code.lower()}_index_empty_no_constituent_reconstruction",
            "rows": 0,
            "index_code": spec.code,
            "pykrx_code": spec.pykrx_code,
        }
    if sqlite_conn is not None and spec.substitute_codes:
        sqlite_conn.execute(
            """
            DELETE FROM index_eod
            WHERE index_code = ?
              AND (
                source LIKE '%1028%'
                OR source LIKE '%2203%'
              )
            """,
            [spec.code],
        )
        sqlite_conn.commit()
    if duckdb_conn is not None:
        append_dedup_table(duckdb_conn, "benchmark_daily", frame, ["date", "benchmark"])
    sqlite_rows = 0
    if sqlite_conn is not None:
        sqlite_rows = upsert_frame(
            sqlite_conn,
            "index_eod",
            _to_index_eod(frame, spec.code, spec.pykrx_code),
            ["index_code", "session_date"],
        )
    return {
        "status": "ready",
        "reason": None,
        "rows": int(len(frame)),
        "sqlite_rows": sqlite_rows,
        "index_code": spec.code,
        "pykrx_code": spec.pykrx_code,
        "min_date": str(frame["date"].min()),
        "max_date": str(frame["date"].max()),
    }


def fetch_kospi100_index_ohlcv(
    start_date: str,
    end_date: str | None = None,
    *,
    stock_api: Any | None = None,
) -> pd.DataFrame:
    return fetch_official_index_ohlcv("KOSPI100", start_date, end_date, stock_api=stock_api)


def fetch_official_index_ohlcv(
    index_code: str,
    start_date: str,
    end_date: str | None = None,
    *,
    stock_api: Any | None = None,
) -> pd.DataFrame:
    spec = get_official_index(index_code)
    if spec.pykrx_code is None:
        return pd.DataFrame()
    client = stock_api
    if client is None:
        try:
            from pykrx import stock as client
        except Exception as exc:
            raise OfficialIndexIngestError(f"pykrx is required for {spec.code} index {spec.pykrx_code}") from exc
    raw = client.get_index_ohlcv_by_date(
        _yyyymmdd(start_date),
        _yyyymmdd(end_date or date.today().isoformat()),
        spec.pykrx_code,
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    frame = raw.reset_index().rename(
        columns={
            "날짜": "date",
            "시가": "open",
            "고가": "high",
            "저가": "low",
            "종가": "close",
            "거래량": "volume",
            "거래대금": "trading_value",
        }
    )
    if "date" not in frame.columns:
        frame = frame.rename(columns={frame.columns[0]: "date"})
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame["benchmark"] = spec.code
    frame["collected_at"] = datetime.now(UTC).replace(tzinfo=None)
    for column in ("open", "high", "low", "close", "volume", "trading_value"):
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame[
        ["date", "benchmark", "open", "high", "low", "close", "volume", "trading_value", "collected_at"]
    ].dropna(subset=["date", "close"]).sort_values("date")


def _to_index_eod(frame: pd.DataFrame, index_code: str, pykrx_code: str) -> pd.DataFrame:
    ingested_at = utcnow_text()
    return pd.DataFrame(
        {
            "index_code": index_code,
            "session_date": pd.to_datetime(frame["date"], errors="coerce").dt.date.astype(str),
            "open": pd.to_numeric(frame["open"], errors="coerce"),
            "high": pd.to_numeric(frame["high"], errors="coerce"),
            "low": pd.to_numeric(frame["low"], errors="coerce"),
            "close": pd.to_numeric(frame["close"], errors="coerce"),
            "volume": pd.to_numeric(frame["volume"], errors="coerce"),
            "turnover": pd.to_numeric(frame["trading_value"], errors="coerce"),
            "source": f"pykrx_{pykrx_code}",
            "published_at": pd.to_datetime(frame["date"], errors="coerce").dt.date.astype(str),
            "ingested_at": ingested_at,
        }
    )


def _yyyymmdd(value: str) -> str:
    return pd.to_datetime(value).strftime("%Y%m%d")
