from __future__ import annotations

import pandas as pd

from roboquant.data.collectors.investor_flows import _normalize_investor_frame
from roboquant.data.collectors.market_metrics import _reset_with_symbol


def test_investor_flow_standardizes_pykrx_like_columns() -> None:
    raw = pd.DataFrame(
        {
            "티커": ["5930"],
            "매수거래대금": [2000],
            "매도거래대금": [1200],
            "순매수거래대금": [800],
        }
    )

    normalized = _normalize_investor_frame(raw, "foreign", "20240131")

    assert normalized.iloc[0]["date"] == pd.Timestamp("2024-01-31").date()
    assert normalized.iloc[0]["symbol"] == "005930"
    assert normalized.iloc[0]["foreign_buy_value"] == 2000
    assert normalized.iloc[0]["foreign_sell_value"] == 1200
    assert normalized.iloc[0]["foreign_net_value"] == 800


def test_market_metrics_symbol_reset_zero_pads_symbol() -> None:
    raw = pd.DataFrame({"시가총액": [1000]}, index=pd.Index(["5930"], name="티커"))

    normalized = _reset_with_symbol(raw)

    assert normalized.iloc[0]["symbol"] == "005930"

