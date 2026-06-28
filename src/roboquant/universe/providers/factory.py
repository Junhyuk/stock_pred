from __future__ import annotations

from collections.abc import Mapping
from os import environ

from roboquant.universe.providers.approved_api import (
    KisBrokerMarketDataProvider,
    KrxOpenApiMarketDataProvider,
)
from roboquant.universe.providers.base import MarketDataProvider
from roboquant.universe.providers.fdr_poc import FdrPocMarketDataProvider


def get_market_data_provider(
    name: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> MarketDataProvider:
    values = environ if env is None else env
    provider_name = str(name or values.get("MARKET_DATA_PROVIDER") or "fdr_poc").strip().lower()
    if provider_name == "fdr_poc":
        return FdrPocMarketDataProvider()
    if provider_name == "krx_openapi":
        return KrxOpenApiMarketDataProvider(values)
    if provider_name in {"broker", "kis"}:
        return KisBrokerMarketDataProvider(values)
    raise ValueError(f"Unknown market data provider: {provider_name}")

