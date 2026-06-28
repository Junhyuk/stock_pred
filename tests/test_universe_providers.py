from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from roboquant.universe.providers.approved_api import (
    KisBrokerMarketDataProvider,
    KrxOpenApiMarketDataProvider,
)
from roboquant.universe.providers.errors import ProviderConfigurationError
from roboquant.universe.providers.factory import get_market_data_provider
from roboquant.universe.providers.fdr_poc import FdrPocMarketDataProvider


class FakeFdr:
    @staticmethod
    def StockListing(name: str) -> pd.DataFrame:
        if name == "KRX-DESC":
            return pd.DataFrame(
                {
                    "Code": ["005930", "005935", "069500", "000150", "196170"],
                    "ListingDate": [
                        "1975-06-11",
                        "1989-09-25",
                        "2002-10-14",
                        "1973-06-29",
                        "2014-12-12",
                    ],
                }
            )
        return pd.DataFrame(
            {
                "Code": ["005930", "005935", "069500", "000150", "196170"],
                "Name": ["삼성전자", "삼성전자우", "KODEX 200", "두산", "알테오젠"],
                "Market": ["KOSPI", "KOSPI", "KOSPI", "KOSPI", "KOSDAQ"],
                "Marcap": [500, 300, 400, 100, 250],
                "Type": [pd.NA, pd.NA, pd.NA, pd.NA, pd.NA],
                "Status": [pd.NA, pd.NA, pd.NA, pd.NA, pd.NA],
            }
        )

    @staticmethod
    def DataReader(symbol: str, start: str, end: str) -> pd.DataFrame:
        assert symbol == "005930"
        assert start == "2026-06-01"
        assert end == "2026-06-05"
        return pd.DataFrame(
            {
                "Open": [100.0, 105.0],
                "High": [110.0, 112.0],
                "Low": [95.0, 101.0],
                "Close": [108.0, 110.0],
                "Volume": [1_000.0, 2_000.0],
            },
            index=pd.to_datetime(["2026-06-04", "2026-06-05"]).rename("Date"),
        )


def test_fdr_poc_normalizes_rankings_and_security_types() -> None:
    provider = FdrPocMarketDataProvider(FakeFdr)

    items = provider.get_market_cap_ranking(date(2026, 6, 6), "KOSPI", 10)

    assert [item.symbol for item in items] == ["005930", "069500", "005935", "000150"]
    assert [item.raw_rank for item in items] == [1, 2, 3, 4]
    assert items[0].listing_date == date(1975, 6, 11)
    assert items[1].security_type == "ETF"
    assert items[2].security_type == "PREFERRED"
    assert items[3].security_type == "COMMON"
    assert provider.is_poc


def test_fdr_poc_normalizes_price_history() -> None:
    provider = FdrPocMarketDataProvider(FakeFdr)

    frame = provider.get_price_history(
        "5930",
        date(2026, 6, 1),
        date(2026, 6, 5),
    )

    assert frame["symbol"].unique().tolist() == ["005930"]
    assert frame.iloc[-1]["trading_value"] == 220_000
    assert frame.iloc[-1]["source"] == "fdr_poc"
    assert frame["date"].max() == date(2026, 6, 5)


def test_fdr_poc_validates_arguments() -> None:
    provider = FdrPocMarketDataProvider(FakeFdr)

    with pytest.raises(ValueError, match="Unsupported KRX market"):
        provider.get_market_cap_ranking(date(2026, 6, 6), "NYSE", 10)
    with pytest.raises(ValueError, match="fetch_limit"):
        provider.get_market_cap_ranking(date(2026, 6, 6), "KOSPI", 0)
    with pytest.raises(ValueError, match="start_date"):
        provider.get_price_history("005930", date(2026, 6, 6), date(2026, 6, 5))


def test_approved_providers_require_environment_variables() -> None:
    with pytest.raises(ProviderConfigurationError, match="KRX_OPENAPI_KEY"):
        KrxOpenApiMarketDataProvider({})
    with pytest.raises(ProviderConfigurationError, match="KIS_APP_KEY"):
        KisBrokerMarketDataProvider({})


def test_approved_providers_do_not_guess_endpoints() -> None:
    krx = KrxOpenApiMarketDataProvider(
        {"KRX_OPENAPI_KEY": "key", "KRX_OPENAPI_SERVICE_ID": "service"}
    )
    broker = KisBrokerMarketDataProvider({"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret"})

    with pytest.raises(NotImplementedError, match="approved service"):
        krx.get_market_cap_ranking(date(2026, 6, 6), "KOSPI", 100)
    with pytest.raises(NotImplementedError, match="approved broker"):
        broker.get_price_history("005930", date(2026, 6, 1), date(2026, 6, 5))


def test_provider_factory_defaults_to_local_poc() -> None:
    assert isinstance(get_market_data_provider(env={}), FdrPocMarketDataProvider)
    assert isinstance(
        get_market_data_provider(
            "krx_openapi",
            env={"KRX_OPENAPI_KEY": "key", "KRX_OPENAPI_SERVICE_ID": "service"},
        ),
        KrxOpenApiMarketDataProvider,
    )
    with pytest.raises(ValueError, match="Unknown market data provider"):
        get_market_data_provider("unknown", env={})
