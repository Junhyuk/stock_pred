from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from time import sleep

import pandas as pd

from roboquant.utils import today_string, yyyymmdd


def fetch_symbols(markets: Iterable[str], asof_date: str | None = None) -> pd.DataFrame:
    """Fetch KRX symbols through pykrx, with FinanceDataReader as a fallback."""
    markets = list(markets)
    asof = yyyymmdd(asof_date or today_string())

    try:
        from pykrx import stock

        rows: list[dict[str, object]] = []
        for market in markets:
            tickers = stock.get_market_ticker_list(asof, market=market)
            for ticker in tickers:
                rows.append(
                    {
                        "symbol": str(ticker).zfill(6),
                        "name": stock.get_market_ticker_name(ticker),
                        "market": market,
                        "sector": None,
                        "listing_date": None,
                        "delisting_date": None,
                        "is_active": True,
                        "collected_at": datetime.utcnow(),
                    }
                )
        frame = pd.DataFrame(rows)
        if frame.empty or "symbol" not in frame.columns:
            raise ValueError("pykrx returned no symbol rows")
        return frame.drop_duplicates("symbol").reset_index(drop=True)
    except Exception:
        return _fetch_symbols_fdr(markets)


def _fetch_symbols_fdr(markets: list[str]) -> pd.DataFrame:
    try:
        import FinanceDataReader as fdr
    except Exception as exc:
        raise RuntimeError(
            "Could not import pykrx or FinanceDataReader. Install project dependencies first."
        ) from exc

    listing = fdr.StockListing("KRX")
    market_col = "Market" if "Market" in listing.columns else "market"
    symbol_col = "Code" if "Code" in listing.columns else "Symbol"
    name_col = "Name" if "Name" in listing.columns else "Name"
    listing = listing[listing[market_col].isin(markets)].copy()
    listing["symbol"] = listing[symbol_col].astype(str).str.zfill(6)
    listing["name"] = listing[name_col].astype(str)
    listing["market"] = listing[market_col].astype(str)
    listing["sector"] = listing.get("Sector")
    listing["listing_date"] = None
    listing["delisting_date"] = None
    listing["is_active"] = True
    listing["collected_at"] = datetime.utcnow()
    return listing[
        [
            "symbol",
            "name",
            "market",
            "sector",
            "listing_date",
            "delisting_date",
            "is_active",
            "collected_at",
        ]
    ].drop_duplicates("symbol")


def fetch_kospi_top_symbols(
    limit: int = 100,
    focus_symbol: str = "005930",
    extra_symbols: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Build a market-cap-ranked KOSPI universe with descriptive metadata."""
    try:
        import FinanceDataReader as fdr
    except Exception as exc:
        raise RuntimeError("FinanceDataReader is required for the KOSPI demo universe") from exc

    listing = fdr.StockListing("KRX")
    description = fdr.StockListing("KRX-DESC")
    listing = listing[listing["Market"].eq("KOSPI")].copy()
    listing["symbol"] = listing["Code"].astype(str).str.zfill(6)
    listing["market_cap"] = pd.to_numeric(listing.get("Marcap"), errors="coerce")
    listing = listing.sort_values("market_cap", ascending=False)

    description = description.copy()
    description["symbol"] = description["Code"].astype(str).str.zfill(6)
    description = description.rename(
        columns={
            "Name": "description_name",
            "Industry": "industry",
            "Sector": "sector_name",
            "ListingDate": "listing_date",
        }
    )
    selected = listing.head(int(limit)).copy()
    focus_symbol = str(focus_symbol).zfill(6)
    if focus_symbol not in set(selected["symbol"]):
        focus = listing[listing["symbol"].eq(focus_symbol)]
        selected = pd.concat([selected.iloc[: max(0, int(limit) - 1)], focus], ignore_index=True)

    extra_set = {str(symbol).zfill(6) for symbol in (extra_symbols or [])}
    missing_extra = sorted(extra_set.difference(set(selected["symbol"])))
    if missing_extra:
        extra = listing[listing["symbol"].isin(missing_extra)]
        selected = pd.concat([selected, extra], ignore_index=True)
    selected = selected.merge(
        description[["symbol", "description_name", "industry", "sector_name", "listing_date"]],
        on="symbol",
        how="left",
    )
    selected["name"] = selected["Name"].fillna(selected["description_name"])
    selected["market"] = "KOSPI"
    selected["sector"] = selected["industry"].fillna(selected["sector_name"]).fillna("기타")
    selected["listing_date"] = pd.to_datetime(selected["listing_date"], errors="coerce").dt.date
    selected["delisting_date"] = None
    selected["is_active"] = True
    selected["collected_at"] = datetime.utcnow()
    return selected[
        [
            "symbol",
            "name",
            "market",
            "sector",
            "listing_date",
            "delisting_date",
            "is_active",
            "collected_at",
            "market_cap",
        ]
    ].drop_duplicates("symbol")


def fetch_prices(symbol: str, start_date: str, end_date: str | None = None) -> pd.DataFrame:
    """Fetch daily OHLCV for one KRX symbol."""
    end = end_date or today_string()
    start_krx = yyyymmdd(start_date)
    end_krx = yyyymmdd(end)
    symbol = str(symbol).zfill(6)

    source = "pykrx"
    try:
        from pykrx import stock

        raw = stock.get_market_ohlcv_by_date(start_krx, end_krx, symbol)
        if raw.empty:
            raise ValueError("pykrx returned no price rows")
        frame = raw.reset_index().rename(
            columns={
                "날짜": "date",
                "시가": "open",
                "고가": "high",
                "저가": "low",
                "종가": "close",
                "거래량": "volume",
                "거래대금": "trading_value",
                "등락률": "change_rate",
            }
        )
        required = {"date", "open", "high", "low", "close", "volume", "trading_value"}
        if not required.issubset(frame.columns):
            raise ValueError("pykrx returned an unexpected OHLCV schema")
    except Exception:
        frame = _fetch_prices_fdr(symbol, start_date, end)
        source = "finance_data_reader"

    if frame.empty:
        return _empty_prices()

    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["symbol"] = symbol
    frame["adj_close"] = frame["close"].astype(float)
    frame["market_cap"] = frame.get("market_cap", pd.NA)
    frame["source"] = source
    frame["collected_at"] = datetime.utcnow()
    return frame[
        [
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
    ].sort_values(["symbol", "date"])


def _fetch_prices_fdr(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    try:
        import FinanceDataReader as fdr
    except Exception as exc:
        raise RuntimeError(
            "Could not import pykrx or FinanceDataReader. Install project dependencies first."
        ) from exc

    raw = fdr.DataReader(symbol, start_date, end_date)
    if raw.empty:
        return _empty_prices()
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
    frame["trading_value"] = frame["close"].astype(float) * frame["volume"].astype(float)
    return frame


def fetch_benchmark(
    benchmark_code: str,
    benchmark_name: str,
    start_date: str,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Fetch KRX index OHLCV. KOSPI is usually code 1001."""
    end = end_date or today_string()
    try:
        from pykrx import stock

        raw = stock.get_index_ohlcv_by_date(yyyymmdd(start_date), yyyymmdd(end), benchmark_code)
        if raw.empty:
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
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        frame["benchmark"] = benchmark_name
        frame["collected_at"] = datetime.utcnow()
        return frame[
            [
                "date",
                "benchmark",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "trading_value",
                "collected_at",
            ]
        ].sort_values("date")
    except Exception:
        try:
            import FinanceDataReader as fdr

            fdr_symbol = "KQ11" if str(benchmark_code) == "2001" or str(benchmark_name).upper() == "KOSDAQ" else "KS11"
            raw = fdr.DataReader(fdr_symbol, start_date, end)
            if raw.empty:
                return pd.DataFrame()
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
            frame["date"] = pd.to_datetime(frame["date"]).dt.date
            frame["benchmark"] = benchmark_name
            frame["trading_value"] = pd.NA
            frame["collected_at"] = datetime.utcnow()
            return frame[
                [
                    "date",
                    "benchmark",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "trading_value",
                    "collected_at",
                ]
            ].sort_values("date")
        except Exception:
            return pd.DataFrame()


def collect_price_panel(
    symbols: Iterable[str],
    start_date: str,
    end_date: str | None,
    sleep_seconds: float = 0.05,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for symbol in symbols:
        frame = fetch_prices(str(symbol).zfill(6), start_date, end_date)
        if not frame.empty:
            frames.append(frame)
        if sleep_seconds > 0:
            sleep(sleep_seconds)
    if not frames:
        return _empty_prices()
    return pd.concat(frames, ignore_index=True)


def _empty_prices() -> pd.DataFrame:
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
