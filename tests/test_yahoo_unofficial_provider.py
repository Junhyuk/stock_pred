from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from roboquant.data.providers.yahoo_unofficial import (
    YahooSymbol,
    YahooUnofficialOptInError,
    YahooUnofficialProvider,
    normalize_price_frame,
    symbols_from_config,
)
from roboquant.db import append_dedup_table, connect_database


def test_yahoo_provider_requires_explicit_opt_in() -> None:
    with pytest.raises(YahooUnofficialOptInError, match="ALLOW_UNOFFICIAL_YAHOO"):
        YahooUnofficialProvider(env={})


def test_yahoo_price_frame_normalization() -> None:
    spec = YahooSymbol("005930.KS", "005930", "stock", "KRW")
    raw = pd.DataFrame(
        {
            "Open": [70000.0],
            "High": [71000.0],
            "Low": [69000.0],
            "Close": [70500.0],
            "Adj Close": [70400.0],
            "Volume": [1000000],
        },
        index=pd.DatetimeIndex([date(2026, 6, 5)]),
    )

    frame = normalize_price_frame(raw, spec)

    assert frame.iloc[0]["date"] == date(2026, 6, 5)
    assert frame.iloc[0]["symbol"] == "005930"
    assert frame.iloc[0]["yahoo_symbol"] == "005930.KS"
    assert frame.iloc[0]["asset_type"] == "stock"
    assert frame.iloc[0]["close"] == 70500.0
    assert frame.iloc[0]["adj_close"] == 70400.0
    assert frame.iloc[0]["currency"] == "KRW"


def test_yahoo_provider_collects_fundamentals_best_effort() -> None:
    provider = YahooUnofficialProvider(
        env={"ALLOW_UNOFFICIAL_YAHOO": "true"},
        yf_module=FakeYFinance({"005930.KS": {"marketCap": 1000, "trailingPE": 12.5, "currency": "KRW"}}),
    )

    frame = provider.get_fundamentals([YahooSymbol("005930.KS", "005930", "stock")], date(2026, 6, 5))

    assert len(frame) == 1
    assert frame.iloc[0]["market_cap"] == 1000.0
    assert frame.iloc[0]["trailing_pe"] == 12.5
    assert json.loads(frame.iloc[0]["raw_info_json"])["currency"] == "KRW"


def test_yahoo_provider_skips_empty_fundamentals_for_etf_or_index() -> None:
    provider = YahooUnofficialProvider(
        env={"ALLOW_UNOFFICIAL_YAHOO": "true"},
        yf_module=FakeYFinance({"SPY": {}}),
    )

    frame = provider.get_fundamentals([YahooSymbol("SPY", "SPY", "etf")], date(2026, 6, 5))

    assert frame.empty


def test_yahoo_tables_accept_separate_data_without_touching_predictions(tmp_path) -> None:
    conn = connect_database(tmp_path / "yahoo.duckdb")
    prices = normalize_price_frame(
        pd.DataFrame({"Close": [100.0]}, index=pd.DatetimeIndex([date(2026, 6, 5)])),
        YahooSymbol("SPY", "SPY", "etf", "USD"),
    )

    append_dedup_table(conn, "yahoo_prices_daily", prices, ["date", "yahoo_symbol"])

    assert conn.execute("SELECT COUNT(*) FROM yahoo_prices_daily").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0


def test_yahoo_symbols_from_config_infers_types() -> None:
    symbols = symbols_from_config(
        [
            {"yahoo_symbol": "005930.KS", "symbol": "005930", "asset_type": "stock"},
            "SPY",
            "^IXIC",
        ]
    )

    assert [(item.yahoo_symbol, item.symbol, item.asset_type) for item in symbols] == [
        ("005930.KS", "005930", "stock"),
        ("SPY", "SPY", "etf"),
        ("^IXIC", "^IXIC", "index"),
    ]


class FakeTicker:
    def __init__(self, info):
        self.info = info


class FakeYFinance:
    def __init__(self, info_by_symbol):
        self.info_by_symbol = info_by_symbol

    def Ticker(self, symbol):
        return FakeTicker(self.info_by_symbol.get(symbol, {}))
