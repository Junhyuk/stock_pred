from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from os import environ
from typing import Any

import pandas as pd


class YahooUnofficialOptInError(RuntimeError):
    """Raised when the unofficial Yahoo provider is used without explicit local opt-in."""


@dataclass(frozen=True)
class YahooSymbol:
    yahoo_symbol: str
    symbol: str
    asset_type: str = "stock"
    currency: str | None = None


class YahooUnofficialProvider:
    provider_name = "yahoo_unofficial"
    is_official = False

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        yf_module: Any | None = None,
        require_opt_in: bool = True,
    ) -> None:
        values = environ if env is None else env
        if require_opt_in and str(values.get("ALLOW_UNOFFICIAL_YAHOO", "")).lower() != "true":
            raise YahooUnofficialOptInError(
                "Unofficial Yahoo/yfinance provider requires ALLOW_UNOFFICIAL_YAHOO=true "
                'and is intended only for local PoC use. Install with: pip install -e ".[global]"'
            )
        self._yf = yf_module

    def get_price_history(
        self,
        symbols: Sequence[YahooSymbol],
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")
        yf = self._load_yfinance()
        frames = []
        for spec in symbols:
            raw = yf.download(
                spec.yahoo_symbol,
                start=start_date.isoformat(),
                end=(end_date + timedelta(days=1)).isoformat(),
                progress=False,
                auto_adjust=False,
                group_by="column",
                threads=False,
            )
            normalized = normalize_price_frame(raw, spec)
            if not normalized.empty:
                frames.append(normalized)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def get_fundamentals(self, symbols: Sequence[YahooSymbol], asof_date: date) -> pd.DataFrame:
        yf = self._load_yfinance()
        rows = []
        now = datetime.now(UTC).replace(tzinfo=None)
        for spec in symbols:
            info = _safe_ticker_info(yf, spec.yahoo_symbol)
            if not info:
                continue
            rows.append(
                {
                    "asof_date": asof_date,
                    "symbol": spec.symbol,
                    "yahoo_symbol": spec.yahoo_symbol,
                    "asset_type": spec.asset_type,
                    "market_cap": _float_or_none(info.get("marketCap")),
                    "trailing_pe": _float_or_none(info.get("trailingPE")),
                    "forward_pe": _float_or_none(info.get("forwardPE")),
                    "price_to_book": _float_or_none(info.get("priceToBook")),
                    "beta": _float_or_none(info.get("beta")),
                    "dividend_yield": _float_or_none(info.get("dividendYield")),
                    "currency": info.get("currency") or spec.currency,
                    "raw_info_json": json.dumps(_json_safe_info(info), ensure_ascii=False, allow_nan=False),
                    "collected_at": now,
                }
            )
        return pd.DataFrame(rows)

    def _load_yfinance(self):
        if self._yf is not None:
            return self._yf
        try:
            import yfinance as yf
        except Exception as exc:
            raise RuntimeError('yfinance is required. Install it with: pip install -e ".[global]"') from exc
        self._yf = yf
        return yf


def normalize_price_frame(frame, spec: YahooSymbol) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    normalized = _flatten_yfinance_columns(frame)
    if "Close" not in normalized.columns:
        return pd.DataFrame()
    now = datetime.now(UTC).replace(tzinfo=None)
    records = []
    for index, row in normalized.iterrows():
        close = _float_or_none(row.get("Close"))
        if close is None:
            continue
        price_date = pd.Timestamp(index).date()
        records.append(
            {
                "date": price_date,
                "symbol": spec.symbol,
                "yahoo_symbol": spec.yahoo_symbol,
                "asset_type": spec.asset_type,
                "open": _float_or_none(row.get("Open")),
                "high": _float_or_none(row.get("High")),
                "low": _float_or_none(row.get("Low")),
                "close": close,
                "adj_close": _float_or_none(row.get("Adj Close")),
                "volume": _float_or_none(row.get("Volume")),
                "currency": spec.currency,
                "source_timestamp": datetime.combine(price_date, time.min, tzinfo=UTC).replace(tzinfo=None),
                "collected_at": now,
            }
        )
    return pd.DataFrame(records)


def symbols_from_config(items: Sequence[dict[str, Any] | str]) -> list[YahooSymbol]:
    symbols: list[YahooSymbol] = []
    for item in items:
        if isinstance(item, str):
            symbols.append(YahooSymbol(yahoo_symbol=item, symbol=item, asset_type=_infer_asset_type(item)))
            continue
        yahoo_symbol = str(item["yahoo_symbol"])
        symbols.append(
            YahooSymbol(
                yahoo_symbol=yahoo_symbol,
                symbol=str(item.get("symbol") or yahoo_symbol),
                asset_type=str(item.get("asset_type") or _infer_asset_type(yahoo_symbol)),
                currency=item.get("currency"),
            )
        )
    return symbols


def _safe_ticker_info(yf, yahoo_symbol: str) -> dict[str, Any]:
    try:
        info = yf.Ticker(yahoo_symbol).info
    except Exception:
        return {}
    return info if isinstance(info, dict) else {}


def _flatten_yfinance_columns(frame) -> pd.DataFrame:
    normalized = frame.copy()
    if isinstance(normalized.columns, pd.MultiIndex):
        normalized.columns = [
            str(col[-1]) if str(col[-1]) in _YF_COLUMNS else str(col[0]) for col in normalized.columns
        ]
    return normalized


def _infer_asset_type(yahoo_symbol: str) -> str:
    if yahoo_symbol.startswith("^"):
        return "index"
    if yahoo_symbol.upper() in {"SPY", "QQQ", "DIA", "IWM"}:
        return "etf"
    return "stock"


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_safe_info(info: dict[str, Any]) -> dict[str, Any]:
    safe = {}
    for key, value in info.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, float) and pd.isna(value):
                continue
            safe[str(key)] = value
    return safe


_YF_COLUMNS = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
