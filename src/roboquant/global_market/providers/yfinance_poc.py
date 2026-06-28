from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

from roboquant.global_market.providers.base import (
    DailyMarketBar,
    GlobalMarketProvider,
    MarketSnapshot,
)
from roboquant.global_market.providers.registry import (
    YFINANCE_DAILY_SYMBOLS,
    YFINANCE_INTRADAY_SYMBOLS,
)


class YFinancePocProvider(GlobalMarketProvider):
    provider_name = "yfinance_poc"
    is_poc = True

    def __init__(self, yf_module: Any | None = None) -> None:
        self._yf = yf_module

    def get_daily_bars(
        self,
        symbols: Iterable[str],
        start_date: date,
        end_date: date,
    ) -> list[DailyMarketBar]:
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")
        yf = self._load_yfinance()
        bars: list[DailyMarketBar] = []
        for raw_symbol in symbols:
            symbol = str(raw_symbol).strip()
            if symbol not in YFINANCE_DAILY_SYMBOLS:
                raise ValueError(f"Unsupported yfinance daily symbol: {symbol}")
            metadata = YFINANCE_DAILY_SYMBOLS[symbol]
            frame = yf.download(
                symbol,
                start=start_date.isoformat(),
                end=(end_date + timedelta(days=1)).isoformat(),
                progress=False,
                auto_adjust=False,
                group_by="column",
                threads=False,
            )
            if frame is None or frame.empty:
                continue
            bars.extend(_daily_frame_to_bars(frame, symbol, metadata, self.provider_name))
        return bars

    def get_intraday_snapshots(
        self,
        symbols: Iterable[str],
        snapshot_at: datetime,
    ) -> list[MarketSnapshot]:
        yf = self._load_yfinance()
        cutoff_utc = _to_utc(snapshot_at)
        snapshots: list[MarketSnapshot] = []
        for raw_symbol in symbols:
            symbol = str(raw_symbol).strip()
            if symbol not in YFINANCE_INTRADAY_SYMBOLS:
                raise ValueError(f"Unsupported yfinance intraday symbol: {symbol}")
            metadata = YFINANCE_INTRADAY_SYMBOLS[symbol]
            ticker = yf.Ticker(symbol)
            frame = ticker.history(period="5d", interval="1m", prepost=True, auto_adjust=False)
            if frame is None or frame.empty:
                continue
            point = _latest_intraday_point_before(frame, cutoff_utc)
            if point is None:
                continue
            source_timestamp, price = point
            previous_close = _previous_daily_close(ticker, source_timestamp)
            change_rate = None
            if previous_close and previous_close > 0:
                change_rate = price / previous_close - Decimal("1")
            freshness = max(0, int((cutoff_utc - source_timestamp).total_seconds()))
            snapshots.append(
                MarketSnapshot(
                    snapshot_at=snapshot_at,
                    symbol=symbol,
                    market_group=metadata.market_group,
                    price=price,
                    change_rate=change_rate,
                    source_name=self.provider_name,
                    source_timestamp=source_timestamp,
                    freshness_seconds=freshness,
                )
            )
        return snapshots

    def _load_yfinance(self):
        if self._yf is not None:
            return self._yf
        try:
            import yfinance as yf
        except Exception as exc:
            raise RuntimeError('yfinance is required. Install it with: pip install -e ".[global]"') from exc
        self._yf = yf
        return yf


def _daily_frame_to_bars(frame, symbol: str, metadata, source_name: str) -> list[DailyMarketBar]:
    normalized = _flatten_yfinance_columns(frame)
    bars: list[DailyMarketBar] = []
    for index, row in normalized.iterrows():
        close = _decimal_or_none(row.get("Close"))
        if close is None:
            continue
        trade_date = pd.Timestamp(index).date()
        source_timestamp = datetime.combine(trade_date, time.min, tzinfo=UTC)
        bars.append(
            DailyMarketBar(
                trade_date=trade_date,
                symbol=symbol,
                market_group=metadata.market_group,
                display_name=metadata.display_name,
                open=_decimal_or_none(row.get("Open")),
                high=_decimal_or_none(row.get("High")),
                low=_decimal_or_none(row.get("Low")),
                close=close,
                volume=_decimal_or_none(row.get("Volume")),
                source_name=source_name,
                source_timestamp=source_timestamp,
            )
        )
    return bars


def _latest_intraday_point_before(frame, cutoff_utc: datetime) -> tuple[datetime, Decimal] | None:
    normalized = _flatten_yfinance_columns(frame)
    if normalized.empty or "Close" not in normalized.columns:
        return None
    index = pd.to_datetime(normalized.index)
    if index.tz is None:
        index = index.tz_localize(UTC)
    else:
        index = index.tz_convert(UTC)
    work = normalized.copy()
    work.index = index
    work = work[work.index <= pd.Timestamp(cutoff_utc)]
    work = work[pd.to_numeric(work["Close"], errors="coerce").notna()]
    if work.empty:
        return None
    latest_time = work.index.max()
    price = _decimal_or_none(work.loc[latest_time, "Close"])
    if price is None:
        return None
    return latest_time.to_pydatetime(), price


def _previous_daily_close(ticker, source_timestamp: datetime) -> Decimal | None:
    try:
        daily = ticker.history(period="7d", interval="1d", prepost=True, auto_adjust=False)
    except Exception:
        return None
    normalized = _flatten_yfinance_columns(daily)
    if normalized.empty or "Close" not in normalized.columns:
        return None
    index = pd.to_datetime(normalized.index)
    if index.tz is None:
        index = index.tz_localize(UTC)
    else:
        index = index.tz_convert(UTC)
    work = normalized.copy()
    work.index = index
    previous = work[work.index.date < source_timestamp.date()]
    if previous.empty:
        return None
    return _decimal_or_none(previous.iloc[-1].get("Close"))


def _flatten_yfinance_columns(frame) -> pd.DataFrame:
    normalized = frame.copy()
    if isinstance(normalized.columns, pd.MultiIndex):
        normalized.columns = [str(col[-1]) if str(col[-1]) in _YF_COLUMNS else str(col[0]) for col in normalized.columns]
    return normalized


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


_YF_COLUMNS = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
