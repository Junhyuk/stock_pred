from __future__ import annotations

from datetime import date

import pandas as pd

from roboquant.features.build_feature_matrix import build_feature_matrix
from roboquant.signals.dart_disclosures import attach_dart_disclosure_features


def test_attach_dart_disclosure_features_uses_lagged_windows_without_lookahead() -> None:
    features = pd.DataFrame(
        [
            {"date": date(2026, 7, 8), "symbol": "005930", "horizon": "3M"},
            {"date": date(2026, 7, 9), "symbol": "005930", "horizon": "3M"},
            {"date": date(2026, 7, 9), "symbol": "000660", "horizon": "3M"},
        ]
    )
    disclosures = pd.DataFrame(
        [
            {
                "symbol": "005930",
                "receipt_date": date(2026, 7, 3),
                "receipt_no": "20260703000001",
                "report_name": "분기보고서",
            },
            {
                "symbol": "005930",
                "receipt_date": date(2026, 7, 8),
                "receipt_no": "20260708000001",
                "report_name": "주요사항보고서(유상증자결정)",
            },
            {
                "symbol": "005930",
                "receipt_date": date(2026, 7, 10),
                "receipt_no": "future",
                "report_name": "주요사항보고서(합병결정)",
            },
            {
                "symbol": "000660",
                "receipt_date": date(2026, 6, 1),
                "receipt_no": "old",
                "report_name": "분기보고서",
            },
        ]
    )

    attached = attach_dart_disclosure_features(features, disclosures)

    samsung_0708 = attached[attached["date"].eq(date(2026, 7, 8)) & attached["symbol"].eq("005930")].iloc[0]
    samsung_0709 = attached[attached["date"].eq(date(2026, 7, 9)) & attached["symbol"].eq("005930")].iloc[0]
    hynix_0709 = attached[attached["symbol"].eq("000660")].iloc[0]

    assert samsung_0708["dart_disclosure_count_1d"] == 1.0
    assert samsung_0708["dart_disclosure_count_7d"] == 2.0
    assert samsung_0708["dart_material_count_7d"] == 1.0
    assert samsung_0708["dart_report_diversity_30d"] == 2.0
    assert samsung_0709["dart_disclosure_count_1d"] == 0.0
    assert samsung_0709["dart_disclosure_count_7d"] == 2.0
    assert samsung_0709["dart_material_count_30d"] == 1.0
    assert hynix_0709["dart_disclosure_count_30d"] == 0.0


def test_attach_dart_disclosure_features_defaults_without_disclosures() -> None:
    features = pd.DataFrame(
        [{"date": date(2026, 7, 9), "symbol": "005930", "horizon": "3M"}]
    )

    attached = attach_dart_disclosure_features(features, pd.DataFrame())

    assert attached["dart_disclosure_count_1d"].iloc[0] == 0.0
    assert attached["dart_material_count_30d"].iloc[0] == 0.0
    assert attached["dart_report_diversity_30d"].iloc[0] == 0.0


def test_feature_matrix_contains_dart_disclosure_columns() -> None:
    prices = pd.DataFrame(
        [
            _price_row("2026-01-01", "005930", 100.0),
            _price_row("2026-02-01", "005930", 110.0),
            _price_row("2026-03-05", "005930", 120.0),
        ]
    )
    disclosures = pd.DataFrame(
        [
            {
                "symbol": "005930",
                "receipt_date": date(2026, 2, 1),
                "receipt_no": "20260201000001",
                "report_name": "주요사항보고서(자기주식취득결정)",
            }
        ]
    )

    features = build_feature_matrix(prices, {"3M": 63}, dart_disclosures=disclosures)

    assert "dart_disclosure_count_30d" in features.columns
    assert "dart_material_count_30d" in features.columns
    latest = features.sort_values("date").iloc[-1]
    assert latest["dart_disclosure_count_30d"] == 0.0
    event_day = features[features["date"].eq(date(2026, 2, 1))].iloc[0]
    assert event_day["dart_material_count_30d"] == 1.0


def _price_row(date_value: str, symbol: str, close: float) -> dict:
    return {
        "date": date.fromisoformat(date_value),
        "symbol": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "adj_close": close,
        "volume": 1000,
        "trading_value": close * 1000,
    }