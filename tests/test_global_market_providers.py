from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from roboquant.global_market.providers.errors import GlobalProviderConfigurationError
from roboquant.global_market.providers.factory import get_global_market_provider
from roboquant.global_market.providers.fred import FredProvider
from roboquant.global_market.providers.registry import (
    FRED_DAILY_SERIES,
    YFINANCE_DAILY_SYMBOLS,
    YFINANCE_INTRADAY_SYMBOLS,
)
from roboquant.global_market.providers.yfinance_poc import YFinancePocProvider


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self):
        self.requests = []

    def get(self, url, params, timeout):
        self.requests.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(
            {
                "observations": [
                    {"date": "2026-06-05", "value": "28.5"},
                    {"date": "2026-06-06", "value": "."},
                    {"date": "2026-06-07", "value": "27.1"},
                ]
            }
        )


def test_global_symbol_registries_include_required_indicators() -> None:
    assert {"^GSPC", "^IXIC", "^DJI", "^SOX", "^VIX", "TSM", "KORU", "EWY", "SPY", "QQQ", "USDKRW=X"}.issubset(
        YFINANCE_DAILY_SYMBOLS
    )
    assert {"ES=F", "NQ=F", "KORU", "EWY", "SPY", "QQQ", "USDKRW=X"}.issubset(YFINANCE_INTRADAY_SYMBOLS)
    assert {"VIXCLS", "DGS10", "DGS2", "T10Y3M", "DCOILWTICO", "DCOILBRENTEU"}.issubset(
        FRED_DAILY_SERIES
    )


def test_fred_provider_requires_api_key() -> None:
    with pytest.raises(GlobalProviderConfigurationError, match="FRED_API_KEY"):
        FredProvider(env={})


def test_fred_provider_normalizes_daily_bars_and_skips_missing_values() -> None:
    client = FakeClient()
    provider = FredProvider(env={"FRED_API_KEY": "secret"}, client=client)

    bars = provider.get_daily_bars(["VIXCLS"], date(2026, 6, 5), date(2026, 6, 7))

    assert [bar.trade_date for bar in bars] == [date(2026, 6, 5), date(2026, 6, 7)]
    assert [bar.close for bar in bars] == [Decimal("28.5"), Decimal("27.1")]
    assert bars[0].symbol == "VIXCLS"
    assert bars[0].market_group == "VOLATILITY"
    assert bars[0].display_name == "CBOE VIX"
    assert bars[0].source_name == "fred"
    assert bars[0].source_timestamp == datetime(2026, 6, 5, tzinfo=UTC)
    assert client.requests[0]["params"]["api_key"] == "secret"


def test_fred_provider_validates_series_and_dates() -> None:
    provider = FredProvider(env={"FRED_API_KEY": "secret"}, client=FakeClient())

    with pytest.raises(ValueError, match="Unsupported FRED series"):
        provider.get_daily_bars(["UNKNOWN"], date(2026, 6, 5), date(2026, 6, 7))
    with pytest.raises(ValueError, match="start_date"):
        provider.get_daily_bars(["VIXCLS"], date(2026, 6, 8), date(2026, 6, 7))
    assert provider.get_intraday_snapshots(["VIXCLS"], datetime(2026, 6, 8, 8, tzinfo=UTC)) == []


def test_global_provider_factory_defaults_to_fred() -> None:
    provider = get_global_market_provider(env={"FRED_API_KEY": "secret"})
    assert isinstance(provider, FredProvider)
    assert isinstance(get_global_market_provider("yfinance_poc", env={}), YFinancePocProvider)
    with pytest.raises(ValueError, match="Unknown global market provider"):
        get_global_market_provider("unknown", env={})


def test_yfinance_provider_normalizes_daily_bars() -> None:
    provider = YFinancePocProvider(FakeYFinanceModule())

    bars = provider.get_daily_bars(["^IXIC"], date(2026, 6, 5), date(2026, 6, 6))

    assert [bar.trade_date for bar in bars] == [date(2026, 6, 5), date(2026, 6, 6)]
    assert bars[0].symbol == "^IXIC"
    assert bars[0].market_group == "US_INDEX"
    assert bars[0].display_name == "Nasdaq Composite"
    assert bars[0].close == Decimal("100.0")
    assert bars[0].source_name == "yfinance_poc"


def test_yfinance_provider_normalizes_intraday_snapshot_before_cutoff() -> None:
    provider = YFinancePocProvider(FakeYFinanceModule())
    cutoff = datetime(2026, 6, 8, 8, tzinfo=UTC)

    snapshots = provider.get_intraday_snapshots(["NQ=F"], cutoff)

    assert len(snapshots) == 1
    assert snapshots[0].symbol == "NQ=F"
    assert snapshots[0].price == Decimal("98.0")
    assert snapshots[0].change_rate == Decimal("-0.02")
    assert snapshots[0].freshness_seconds == 3600


class FakeTicker:
    def history(self, period, interval, prepost, auto_adjust):
        del period, prepost, auto_adjust
        if interval == "1m":
            return pd.DataFrame(
                {"Close": [98.0, 99.0]},
                index=pd.DatetimeIndex(
                    [
                        datetime(2026, 6, 8, 7, tzinfo=UTC),
                        datetime(2026, 6, 8, 9, tzinfo=UTC),
                    ]
                ),
            )
        return pd.DataFrame(
            {"Close": [100.0]},
            index=pd.DatetimeIndex([datetime(2026, 6, 7, tzinfo=UTC)]),
        )


class FakeYFinanceModule:
    def download(self, symbol, start, end, progress, auto_adjust, group_by, threads):
        del symbol, start, end, progress, auto_adjust, group_by, threads
        return pd.DataFrame(
            {
                "Open": [99.0, 101.0],
                "High": [101.0, 103.0],
                "Low": [98.0, 100.0],
                "Close": [100.0, 102.0],
                "Volume": [1000, 1100],
            },
            index=pd.DatetimeIndex([date(2026, 6, 5), date(2026, 6, 6)]),
        )

    def Ticker(self, symbol):
        del symbol
        return FakeTicker()
