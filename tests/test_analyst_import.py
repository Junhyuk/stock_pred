from __future__ import annotations

import pandas as pd
import pytest

from roboquant.data.collectors.analyst.importer import (
    build_import_failures,
    normalize_analyst_reports,
)


def test_analyst_csv_rows_are_normalized_and_matched_by_name() -> None:
    raw = pd.DataFrame(
        {
            "일자": ["2024-01-02"],
            "종목명": ["삼성전자"],
            "증권사": ["Test Securities"],
            "애널리스트": ["Kim"],
            "제목": ["신규 커버리지"],
            "투자의견": ["매수"],
            "목표가": ["90,000"],
            "이전목표가": ["80,000"],
            "현재가": ["75,000"],
        }
    )
    symbols = pd.DataFrame(
        {
            "symbol": ["005930"],
            "name": ["삼성전자"],
            "market": ["KOSPI"],
            "sector": ["Tech"],
        }
    )
    config = {
        "import": {"default_source_name": "fixture", "default_source_url": "manual://fixture"},
        "column_mapping": {
            "report_date": ["일자"],
            "stock_name": ["종목명"],
            "broker_name": ["증권사"],
            "analyst_name": ["애널리스트"],
            "report_title": ["제목"],
            "investment_rating": ["투자의견"],
            "target_price": ["목표가"],
            "previous_target_price": ["이전목표가"],
            "current_price_at_report": ["현재가"],
        },
    }

    normalized = normalize_analyst_reports(raw, config, symbols)

    assert normalized.iloc[0]["symbol"] == "005930"
    assert normalized.iloc[0]["market"] == "KOSPI"
    assert normalized.iloc[0]["target_price"] == 90_000
    assert normalized.iloc[0]["target_change_pct"] == pytest.approx(12.5)
    assert normalized.iloc[0]["upside_pct_at_report"] == pytest.approx(20.0)
    assert normalized.iloc[0]["source_name"] == "fixture"
    assert normalized.iloc[0]["source_url"] == "manual://fixture"
    assert pd.notna(normalized.iloc[0]["report_id"])


def test_unmatched_analyst_row_is_recorded_as_collection_failure() -> None:
    raw = pd.DataFrame(
        {
            "report_date": ["2024-01-02"],
            "stock_name": ["Unknown Co"],
            "target_price": [10000],
            "current_price_at_report": [9000],
        }
    )
    symbols = pd.DataFrame({"symbol": ["005930"], "name": ["삼성전자"], "market": ["KOSPI"]})
    config = {
        "import": {"default_source_name": "fixture", "default_source_url": "manual://fixture"},
        "column_mapping": {},
    }

    normalized = normalize_analyst_reports(raw, config, symbols)
    failures = build_import_failures(normalized)

    assert normalized.iloc[0]["symbol"] is pd.NA or pd.isna(normalized.iloc[0]["symbol"])
    assert len(failures) == 1
    assert failures.iloc[0]["step"] == "import_analyst_reports"
    assert "could not match" in failures.iloc[0]["error_message"]
