from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

import pandas as pd

from roboquant.global_market.providers.base import DailyMarketBar, MarketSnapshot


def daily_bars_to_frame(bars: Iterable[DailyMarketBar]) -> pd.DataFrame:
    records = [
        {
            "trade_date": bar.trade_date,
            "symbol": bar.symbol,
            "market_group": bar.market_group,
            "display_name": bar.display_name,
            "open": _float_or_none(bar.open),
            "high": _float_or_none(bar.high),
            "low": _float_or_none(bar.low),
            "close": _float_or_none(bar.close),
            "volume": _float_or_none(bar.volume),
            "source_name": bar.source_name,
            "source_timestamp": _timestamp_for_storage(bar.source_timestamp),
        }
        for bar in bars
    ]
    frame = pd.DataFrame(records)
    if frame.empty:
        return frame
    frame = frame.sort_values(["source_name", "symbol", "trade_date"]).reset_index(drop=True)
    grouped = frame.groupby(["source_name", "symbol"], sort=False)["close"]
    frame["return_1d"] = grouped.pct_change(1)
    frame["return_5d"] = grouped.pct_change(5)
    frame["return_20d"] = grouped.pct_change(20)
    frame["volatility_20d"] = frame.groupby(["source_name", "symbol"], sort=False)["return_1d"].transform(
        lambda series: series.rolling(20, min_periods=5).std()
    )
    frame["ingested_at"] = datetime.now(UTC).replace(tzinfo=None)
    return frame


def snapshots_to_frame(snapshots: Iterable[MarketSnapshot]) -> pd.DataFrame:
    records = [
        {
            "snapshot_at": _timestamp_for_storage(snapshot.snapshot_at),
            "symbol": snapshot.symbol,
            "market_group": snapshot.market_group,
            "price": _float_or_none(snapshot.price),
            "change_rate": _float_or_none(snapshot.change_rate),
            "source_name": snapshot.source_name,
            "source_timestamp": _timestamp_for_storage(snapshot.source_timestamp),
            "freshness_seconds": snapshot.freshness_seconds,
            "ingested_at": datetime.now(UTC).replace(tzinfo=None),
        }
        for snapshot in snapshots
    ]
    return pd.DataFrame(records)


def _float_or_none(value) -> float | None:
    return None if value is None else float(value)


def _timestamp_for_storage(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value
