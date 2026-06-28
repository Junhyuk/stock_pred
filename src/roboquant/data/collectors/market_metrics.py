from __future__ import annotations

from datetime import datetime
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
        cap = _reset_with_symbol(stock.get_market_cap_by_ticker(date_string, market=market))
        fundamental = _reset_with_symbol(stock.get_market_fundamental_by_ticker(date_string, market=market))
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
                "collected_at": datetime.utcnow(),
            }
        )
        frames.append(frame)

    if not frames:
        return _empty_market_metrics()
    return pd.concat(frames, ignore_index=True)[MARKET_METRICS_COLUMNS].drop_duplicates(
        ["date", "symbol"]
    )


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

