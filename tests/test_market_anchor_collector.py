from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from roboquant.data.collectors.market_anchor import (
    KOSDAQ_SELECTION_RULE,
    KOSPI_SELECTION_RULE,
    MarketAnchorCollectionError,
    fetch_market_anchor_constituents_by_date,
)


class _FakeStock:
    def __init__(self) -> None:
        self.names = {
            "005930": "삼성전자",
            "000660": "SK하이닉스",
            "005380": "현대차",
            "100001": "알파우",
            "100002": "KODEX ETF",
            "100003": "코스닥일반A",
            "100004": "테스트스팩",
            "100005": "코스닥일반B",
            "100006": "코스닥일반C",
        }

    def get_market_cap_by_ticker(self, _date: str, *, market: str) -> pd.DataFrame:
        if market == "KOSPI":
            return pd.DataFrame({"시가총액": [1000, 900, 800]}, index=["005930", "000660", "005380"])
        return pd.DataFrame(
            {"시가총액": [900, 800, 700, 600, 500, 400]},
            index=["100001", "100002", "100003", "100004", "100005", "100006"],
        )

    def get_market_ticker_name(self, symbol: str) -> str:
        return self.names[symbol]


def _settings() -> dict:
    return {
        "kospi_anchors": ["005930", "000660", "005380"],
        "kosdaq_top_n": 3,
        "source": "pykrx",
    }


def test_market_anchor_collector_uses_fixed_kospi_and_point_in_time_kosdaq_top3() -> None:
    frame = fetch_market_anchor_constituents_by_date(
        date(2026, 7, 10),
        _settings(),
        stock_api=_FakeStock(),
        now=datetime(2026, 7, 10, 19, 0),
    )

    kospi = frame[frame["market"].eq("KOSPI")]
    kosdaq = frame[frame["market"].eq("KOSDAQ")]
    assert kospi["symbol"].tolist() == ["005930", "000660", "005380"]
    assert kospi["selection_rule"].eq(KOSPI_SELECTION_RULE).all()
    assert kosdaq["symbol"].tolist() == ["100003", "100005", "100006"]
    assert kosdaq["selection_rule"].eq(KOSDAQ_SELECTION_RULE).all()
    assert kosdaq["market_cap_rank"].tolist() == [3, 5, 6]
    assert kospi["weight"].sum() == pytest.approx(1.0)
    assert kosdaq["weight"].sum() == pytest.approx(1.0)
    assert frame["is_final"].all()


def test_market_anchor_collector_rejects_missing_fixed_kospi_anchor() -> None:
    class MissingAnchor(_FakeStock):
        def get_market_cap_by_ticker(self, _date: str, *, market: str) -> pd.DataFrame:
            frame = super().get_market_cap_by_ticker(_date, market=market)
            return frame.drop(index="005380") if market == "KOSPI" else frame

    with pytest.raises(MarketAnchorCollectionError, match="missing fixed anchor"):
        fetch_market_anchor_constituents_by_date(
            "2026-07-10",
            _settings(),
            stock_api=MissingAnchor(),
        )
