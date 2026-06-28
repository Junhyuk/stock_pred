from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class DailyMarketBar:
    trade_date: date
    symbol: str
    market_group: str
    display_name: str
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal
    volume: Decimal | None
    source_name: str
    source_timestamp: datetime | None


@dataclass(frozen=True)
class MarketSnapshot:
    snapshot_at: datetime
    symbol: str
    market_group: str
    price: Decimal
    change_rate: Decimal | None
    source_name: str
    source_timestamp: datetime | None
    freshness_seconds: int | None = None


class GlobalMarketProvider(ABC):
    provider_name: str
    is_poc: bool = False

    @abstractmethod
    def get_daily_bars(
        self,
        symbols: Iterable[str],
        start_date: date,
        end_date: date,
    ) -> list[DailyMarketBar]:
        """Return normalized daily bars for global indicators."""

    @abstractmethod
    def get_intraday_snapshots(
        self,
        symbols: Iterable[str],
        snapshot_at: datetime,
    ) -> list[MarketSnapshot]:
        """Return normalized point-in-time market snapshots."""
