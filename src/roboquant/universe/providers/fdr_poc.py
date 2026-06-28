from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from roboquant.universe.providers.base import MarketCapItem, MarketDataProvider

SUPPORTED_MARKETS = {"KOSPI", "KOSDAQ"}
ETF_PREFIXES = (
    "ACE ",
    "ARIRANG ",
    "HANARO ",
    "KBSTAR ",
    "KIWOOM ",
    "KODEX ",
    "KOSEF ",
    "PLUS ",
    "RISE ",
    "SOL ",
    "TIGER ",
    "TIMEFOLIO ",
)


class FdrPocMarketDataProvider(MarketDataProvider):
    """FinanceDataReader adapter for local validation only."""

    provider_name = "fdr_poc"
    is_poc = True

    def __init__(self, fdr_module: Any | None = None) -> None:
        self._fdr_module = fdr_module

    def get_market_cap_ranking(
        self,
        trade_date: date,
        market: str,
        fetch_limit: int,
    ) -> list[MarketCapItem]:
        del trade_date  # FDR KRX listing exposes the current ranking, not historical snapshots.
        market = market.upper()
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported KRX market: {market}")
        if fetch_limit <= 0:
            raise ValueError("fetch_limit must be positive")

        fdr = self._fdr()
        listing = fdr.StockListing("KRX").copy()
        market_column = _first_column(listing, "Market", "market")
        symbol_column = _first_column(listing, "Code", "Symbol", "symbol")
        name_column = _first_column(listing, "Name", "name")
        market_cap_column = _first_column(listing, "Marcap", "MarketCap", required=False)

        listing = listing[listing[market_column].astype(str).str.upper().eq(market)].copy()
        listing["symbol"] = listing[symbol_column].astype(str).str.zfill(6)
        listing["name"] = listing[name_column].astype(str)
        listing["market_cap"] = (
            pd.to_numeric(listing[market_cap_column], errors="coerce")
            if market_cap_column
            else pd.Series(float("nan"), index=listing.index)
        )
        listing = listing.drop_duplicates("symbol").sort_values(
            ["market_cap", "symbol"],
            ascending=[False, True],
            na_position="last",
        )

        description = _load_description(fdr)
        if not description.empty:
            listing = listing.merge(description, on="symbol", how="left")
        else:
            listing["listing_date"] = None

        rows: list[MarketCapItem] = []
        for raw_rank, (_, row) in enumerate(listing.head(fetch_limit).iterrows(), start=1):
            rows.append(
                MarketCapItem(
                    symbol=str(row["symbol"]).zfill(6),
                    name=str(row["name"]),
                    market=market,
                    market_cap=_optional_float(row.get("market_cap")),
                    security_type=_detect_security_type(row),
                    raw_rank=raw_rank,
                    is_suspended=_detect_suspended(row),
                    listing_date=_optional_date(row.get("listing_date")),
                )
            )
        return rows

    def get_price_history(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")
        symbol = str(symbol).zfill(6)
        raw = self._fdr().DataReader(symbol, start_date.isoformat(), end_date.isoformat())
        if raw.empty:
            return _empty_price_history()
        frame = raw.reset_index().rename(
            columns={
                "Date": "date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )
        required = {"date", "open", "high", "low", "close", "volume"}
        if not required.issubset(frame.columns):
            missing = sorted(required.difference(frame.columns))
            raise ValueError(f"FinanceDataReader returned an unexpected schema: missing={missing}")
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["symbol"] = symbol
        frame["adj_close"] = frame["close"]
        frame["trading_value"] = frame["close"] * frame["volume"]
        frame["market_cap"] = pd.NA
        frame["source"] = self.provider_name
        frame["collected_at"] = datetime.now(UTC)
        return frame[_empty_price_history().columns].sort_values("date").reset_index(drop=True)

    def _fdr(self):
        if self._fdr_module is not None:
            return self._fdr_module
        try:
            import FinanceDataReader as fdr
        except Exception as exc:
            raise RuntimeError("FinanceDataReader is required for the fdr_poc provider") from exc
        return fdr


def _load_description(fdr) -> pd.DataFrame:
    try:
        frame = fdr.StockListing("KRX-DESC").copy()
    except Exception:
        return pd.DataFrame()
    if frame.empty:
        return pd.DataFrame()
    symbol_column = _first_column(frame, "Code", "Symbol", "symbol")
    listing_date_column = _first_column(
        frame,
        "ListingDate",
        "listing_date",
        required=False,
    )
    frame["symbol"] = frame[symbol_column].astype(str).str.zfill(6)
    frame["listing_date"] = (
        pd.to_datetime(frame[listing_date_column], errors="coerce").dt.date
        if listing_date_column
        else None
    )
    return frame[["symbol", "listing_date"]].drop_duplicates("symbol")


def _detect_security_type(row: pd.Series) -> str:
    explicit = _row_value(row, "SecurityType", "security_type", "Type", "type")
    if explicit is not None and not pd.isna(explicit):
        normalized = str(explicit).strip().upper()
        if normalized in {"ETF", "ETN", "SPAC", "PREFERRED", "COMMON"}:
            return normalized
    name = str(row.get("name", "")).strip()
    upper_name = name.upper()
    if upper_name.endswith(" ETN") or " ETN " in upper_name:
        return "ETN"
    if name.startswith(ETF_PREFIXES):
        return "ETF"
    if "스팩" in name or "SPAC" in upper_name:
        return "SPAC"
    if "우선주" in name or re.search(r"우(?:B|C)?$", name):
        return "PREFERRED"
    return "COMMON"


def _detect_suspended(row: pd.Series) -> bool:
    explicit = _row_value(row, "IsSuspended", "is_suspended")
    if explicit is not None and not pd.isna(explicit):
        if isinstance(explicit, str):
            return explicit.strip().lower() in {"1", "true", "yes", "y"}
        return bool(explicit)
    status = _row_value(row, "Status", "status", "State", "state")
    if status is None or pd.isna(status):
        return False
    return "정지" in str(status) or "SUSPEND" in str(status).upper()


def _row_value(row: pd.Series, *names: str):
    for name in names:
        if name in row.index:
            return row.get(name)
    return None


def _first_column(
    frame: pd.DataFrame,
    *names: str,
    required: bool = True,
) -> str | None:
    for name in names:
        if name in frame.columns:
            return name
    if required:
        raise ValueError(f"Required column not found; expected one of {names}")
    return None


def _optional_float(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _optional_date(value) -> date | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date()


def _empty_price_history() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "date",
            "symbol",
            "open",
            "high",
            "low",
            "close",
            "adj_close",
            "volume",
            "trading_value",
            "market_cap",
            "source",
            "collected_at",
        ]
    )
