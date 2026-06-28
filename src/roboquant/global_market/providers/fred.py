from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from os import environ
from typing import Any

from roboquant.global_market.providers.base import (
    DailyMarketBar,
    GlobalMarketProvider,
    MarketSnapshot,
)
from roboquant.global_market.providers.errors import GlobalProviderConfigurationError
from roboquant.global_market.providers.registry import FRED_DAILY_SERIES

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"


class FredProvider(GlobalMarketProvider):
    provider_name = "fred"
    is_poc = False

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        values = environ if env is None else env
        self.api_key = api_key or values.get("FRED_API_KEY")
        if not self.api_key:
            raise GlobalProviderConfigurationError("FRED_API_KEY is required for FredProvider")
        self._client = client

    def get_daily_bars(
        self,
        symbols: Iterable[str],
        start_date: date,
        end_date: date,
    ) -> list[DailyMarketBar]:
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")

        bars: list[DailyMarketBar] = []
        for raw_symbol in symbols:
            symbol = str(raw_symbol).strip().upper()
            if symbol not in FRED_DAILY_SERIES:
                raise ValueError(f"Unsupported FRED series: {symbol}")
            metadata = FRED_DAILY_SERIES[symbol]
            payload = self._fetch_observations(symbol, start_date, end_date)
            for item in payload.get("observations", []):
                value = _decimal_or_none(item.get("value"))
                if value is None:
                    continue
                trade_date = date.fromisoformat(str(item["date"]))
                source_timestamp = datetime.combine(trade_date, time.min, tzinfo=UTC)
                bars.append(
                    DailyMarketBar(
                        trade_date=trade_date,
                        symbol=symbol,
                        market_group=metadata.market_group,
                        display_name=metadata.display_name,
                        open=None,
                        high=None,
                        low=None,
                        close=value,
                        volume=None,
                        source_name=self.provider_name,
                        source_timestamp=source_timestamp,
                    )
                )
        return bars

    def get_intraday_snapshots(
        self,
        symbols: Iterable[str],
        snapshot_at: datetime,
    ) -> list[MarketSnapshot]:
        del symbols, snapshot_at
        return []

    def _fetch_observations(self, symbol: str, start_date: date, end_date: date) -> dict[str, Any]:
        client = self._client or _default_httpx_client()
        params = {
            "series_id": symbol,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": start_date.isoformat(),
            "observation_end": end_date.isoformat(),
        }
        response = client.get(FRED_OBSERVATIONS_URL, params=params, timeout=30.0)
        response.raise_for_status()
        return response.json()


def _default_httpx_client():
    try:
        import httpx
    except Exception as exc:
        raise RuntimeError("httpx is required for FredProvider") from exc
    return httpx.Client()


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or str(value).strip() in {"", "."}:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None
