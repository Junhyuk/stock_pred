from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from os import environ

import pandas as pd

from roboquant.universe.providers.base import MarketCapItem, MarketDataProvider
from roboquant.universe.providers.errors import ProviderConfigurationError


class KrxOpenApiMarketDataProvider(MarketDataProvider):
    provider_name = "krx_openapi"

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        values = environ if env is None else env
        self.api_key = _required(values, "KRX_OPENAPI_KEY")
        self.service_id = _required(values, "KRX_OPENAPI_SERVICE_ID")

    def get_market_cap_ranking(
        self,
        trade_date: date,
        market: str,
        fetch_limit: int,
    ) -> list[MarketCapItem]:
        raise NotImplementedError(
            "KRX Open API endpoint mapping is not configured. "
            "Connect an approved service before using krx_openapi."
        )

    def get_price_history(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        raise NotImplementedError(
            "KRX Open API price endpoint mapping is not configured. "
            "Connect an approved service before using krx_openapi."
        )


class KisBrokerMarketDataProvider(MarketDataProvider):
    provider_name = "broker"

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        values = environ if env is None else env
        self.app_key = _required(values, "KIS_APP_KEY")
        self.app_secret = _required(values, "KIS_APP_SECRET")

    def get_market_cap_ranking(
        self,
        trade_date: date,
        market: str,
        fetch_limit: int,
    ) -> list[MarketCapItem]:
        raise NotImplementedError(
            "KIS ranking endpoint mapping is not configured. "
            "Connect an approved broker API before using broker."
        )

    def get_price_history(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        raise NotImplementedError(
            "KIS price endpoint mapping is not configured. "
            "Connect an approved broker API before using broker."
        )


def _required(values: Mapping[str, str], name: str) -> str:
    value = str(values.get(name, "")).strip()
    if not value or value == "change_me":
        raise ProviderConfigurationError(f"Missing required environment variable: {name}")
    return value

