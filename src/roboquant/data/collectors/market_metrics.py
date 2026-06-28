from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd

from roboquant.utils import today_string, yyyymmdd

MARKET_METRICS_COLUMNS = [
    "date",
    "symbol",
    "market_cap",
    "per",
    "pbr",
    "eps",
    "bps",
    "dividend_yield",
    "source",
    "collected_at",
]


def fetch_market_metrics_by_date(
    target_date: str | None = None,
    markets: list[str] | None = None,
    errors: list[str] | None = None,
) -> pd.DataFrame:
    """Fetch market cap and valuation snapshots from pykrx."""
    markets = markets or ["KOSPI", "KOSDAQ"]
    date_string = yyyymmdd(target_date or today_string())
    frames: list[pd.DataFrame] = []

    try:
        from pykrx import stock
    except Exception as exc:
        raise RuntimeError("pykrx is required for market metric collection") from exc

    for market in markets:
        try:
            cap = _reset_with_symbol(stock.get_market_cap_by_ticker(date_string, market=market))
            fundamental = _reset_with_symbol(stock.get_market_fundamental_by_ticker(date_string, market=market))
        except Exception as exc:
            if errors is not None:
                errors.append(f"{market}: {exc}")
                continue
            raise
        if cap.empty and fundamental.empty:
            continue
        merged = cap.merge(fundamental, on="symbol", how="outer", suffixes=("", "_fund"))
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(date_string).date(),
                "symbol": merged["symbol"].astype(str).str.zfill(6),
                "market_cap": _coerce_first_available(merged, ["시가총액", "market_cap", "MarketCap"]),
                "per": _coerce_first_available(merged, ["PER", "per"]),
                "pbr": _coerce_first_available(merged, ["PBR", "pbr"]),
                "eps": _coerce_first_available(merged, ["EPS", "eps"]),
                "bps": _coerce_first_available(merged, ["BPS", "bps"]),
                "dividend_yield": _coerce_first_available(merged, ["DIV", "dividend_yield", "Dividend"]),
                "source": "pykrx",
                "collected_at": _utcnow(),
            }
        )
        frames.append(frame)

    if not frames:
        return _empty_market_metrics()
    return pd.concat(frames, ignore_index=True)[MARKET_METRICS_COLUMNS].drop_duplicates(
        ["date", "symbol"]
    )


def fetch_market_metrics_from_universe(
    conn,
    target_date: str | None = None,
    markets: list[str] | None = None,
) -> pd.DataFrame:
    """Build a minimal market metrics snapshot from the active Top50 universe."""
    if not _relation_exists(conn, "current_prediction_universe") and not _relation_exists(conn, "prediction_universe_snapshot"):
        return _empty_market_metrics()
    target = pd.to_datetime(target_date or today_string()).date()
    normalized_markets = [str(market).upper() for market in (markets or ["KOSPI", "KOSDAQ"])]
    frame = _current_universe_market_caps(conn, normalized_markets)
    if frame.empty:
        frame = _snapshot_universe_market_caps(conn, target, normalized_markets)
    if frame.empty:
        return _empty_market_metrics()
    return pd.DataFrame(
        {
            "date": target,
            "symbol": frame["symbol"].astype(str).str.zfill(6),
            "market_cap": pd.to_numeric(frame["market_cap"], errors="coerce"),
            "per": pd.NA,
            "pbr": pd.NA,
            "eps": pd.NA,
            "bps": pd.NA,
            "dividend_yield": pd.NA,
            "source": "universe_market_cap_fallback",
            "collected_at": _utcnow(),
        },
        columns=MARKET_METRICS_COLUMNS,
    ).drop_duplicates(["date", "symbol"])


def _current_universe_market_caps(conn, markets: list[str]) -> pd.DataFrame:
    if not _relation_exists(conn, "current_prediction_universe"):
        return pd.DataFrame()
    placeholders = ", ".join(["?"] * len(markets))
    return conn.execute(
        f"""
        SELECT symbol, market_cap
        FROM current_prediction_universe
        WHERE is_enabled = TRUE
          AND market IN ({placeholders})
          AND market_cap IS NOT NULL
        ORDER BY market, prediction_rank, symbol
        """,
        markets,
    ).fetchdf()


def _snapshot_universe_market_caps(conn, target: Any, markets: list[str]) -> pd.DataFrame:
    if not _relation_exists(conn, "prediction_universe_snapshot"):
        return pd.DataFrame()
    placeholders = ", ".join(["?"] * len(markets))
    return conn.execute(
        f"""
        WITH latest AS (
          SELECT MAX(snapshot_date) AS snapshot_date
          FROM prediction_universe_snapshot
          WHERE snapshot_date <= ?
            AND is_enabled = TRUE
            AND market IN ({placeholders})
            AND market_cap IS NOT NULL
        )
        SELECT symbol, market_cap
        FROM prediction_universe_snapshot
        WHERE snapshot_date = (SELECT snapshot_date FROM latest)
          AND is_enabled = TRUE
          AND market IN ({placeholders})
          AND market_cap IS NOT NULL
        ORDER BY market, prediction_rank, symbol
        """,
        [target, *markets, *markets],
    ).fetchdf()


def _reset_with_symbol(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["symbol"])
    frame = raw.reset_index()
    symbol_column = _find_symbol_column(frame)
    frame = frame.rename(columns={symbol_column: "symbol"})
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    return frame


def _find_symbol_column(frame: pd.DataFrame) -> Any:
    for column in ("티커", "종목코드", "Symbol", "Code", "index"):
        if column in frame.columns:
            return column
    return frame.columns[0]


def _coerce_first_available(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    for column in columns:
        if column in frame.columns:
            return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(pd.NA, index=frame.index, dtype="Float64")


def _empty_market_metrics() -> pd.DataFrame:
    return pd.DataFrame(columns=MARKET_METRICS_COLUMNS)


def _relation_exists(conn, name: str) -> bool:
    result = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?)
          + (SELECT COUNT(*) FROM information_schema.views WHERE table_name = ?)
        """,
        [name, name],
    ).fetchone()[0]
    return bool(result)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
