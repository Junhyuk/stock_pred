from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class MarketCapItem:
    symbol: str
    name: str
    market: str
    market_cap: float | None
    security_type: str
    raw_rank: int
    is_suspended: bool = False
    listing_date: date | None = None


class MarketDataProvider(ABC):
    provider_name: str
    is_poc: bool = False

    @abstractmethod
    def get_market_cap_ranking(
        self,
        trade_date: date,
        market: str,
        fetch_limit: int,
    ) -> list[MarketCapItem]:
        """Return market-cap-ranked candidates for one KRX market."""

    @abstractmethod
    def get_price_history(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Return normalized daily OHLCV rows for eligibility checks."""

