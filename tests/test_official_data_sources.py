from __future__ import annotations

import json
from datetime import date

import pytest

from roboquant.data.providers.official_sources import (
    OfficialDataConfigurationError,
    collect_bigkinds_issues,
    collect_dart_disclosures,
    collect_gdelt_news,
    collect_official_market_prices,
)


def test_gdelt_provider_normalizes_mock_articles() -> None:
    def opener(request):
        return json.dumps(
            {
                "articles": [
                    {
                        "title": "Korea chip stocks fall",
                        "url": "https://example.com/chips",
                        "domain": "example.com",
                        "sourcecountry": "US",
                        "language": "English",
                        "seendate": "20260708010000",
                        "tone": "-2.5",
                    }
                ]
            }
        )

    frame = collect_gdelt_news(
        config={"gdelt": {"queries": ["Korea semiconductor"], "max_records": 1}},
        query_date=date(2026, 7, 8),
        opener=opener,
    )

    assert len(frame) == 1
    assert frame.iloc[0]["query"] == "Korea semiconductor"
    assert frame.iloc[0]["source"] == "gdelt_doc_api"


def test_bigkinds_requires_endpoint_and_key() -> None:
    with pytest.raises(OfficialDataConfigurationError):
        collect_bigkinds_issues(config={"bigkinds": {"endpoint": ""}}, target_date=date(2026, 7, 8), env={})


def test_dart_provider_filters_top50_symbols() -> None:
    def opener(request):
        return json.dumps(
            {
                "list": [
                    {
                        "stock_code": "005930",
                        "corp_code": "00126380",
                        "corp_name": "삼성전자",
                        "report_nm": "주요사항보고서",
                        "rcept_no": "20260708000001",
                        "rcept_dt": "20260708",
                        "flr_nm": "삼성전자",
                        "corp_cls": "Y",
                    },
                    {"stock_code": "999999", "rcept_no": "skip"},
                ]
            }
        )

    frame = collect_dart_disclosures(
        config={"dart": {"endpoint": "https://dart.example/list", "api_key_env": "DART_API_KEY"}},
        symbols=["005930"],
        days=7,
        target_date=date(2026, 7, 8),
        env={"DART_API_KEY": "key"},
        opener=opener,
    )

    assert len(frame) == 1
    assert frame.iloc[0]["symbol"] == "005930"
    assert "20260708000001" in frame.iloc[0]["disclosure_url"]


def test_dart_provider_reads_pages_until_total_page() -> None:
    seen_urls = []

    def opener(request):
        seen_urls.append(request.full_url)
        if "page_no=1" in request.full_url:
            return json.dumps(
                {
                    "status": "000",
                    "total_page": 2,
                    "list": [
                        {
                            "stock_code": "005930",
                            "corp_code": "00126380",
                            "corp_name": "삼성전자",
                            "report_nm": "분기보고서",
                            "rcept_no": "20260708000001",
                            "rcept_dt": "20260708",
                        }
                    ],
                }
            )
        return json.dumps(
            {
                "status": "000",
                "total_page": 2,
                "list": [
                    {
                        "stock_code": "005930",
                        "corp_code": "00126380",
                        "corp_name": "삼성전자",
                        "report_nm": "주요사항보고서",
                        "rcept_no": "20260709000001",
                        "rcept_dt": "20260709",
                    }
                ],
            }
        )

    frame = collect_dart_disclosures(
        config={"dart": {"endpoint": "https://dart.example/list", "api_key_env": "DART_API_KEY"}},
        symbols=["005930"],
        days=7,
        target_date=date(2026, 7, 9),
        env={"DART_API_KEY": "key"},
        opener=opener,
    )

    assert len(frame) == 2
    assert len(seen_urls) == 2


def test_dart_provider_raises_on_error_status() -> None:
    def opener(request):
        return json.dumps({"status": "010", "message": "등록되지 않은 키입니다."})

    with pytest.raises(RuntimeError, match="opendart status 010"):
        collect_dart_disclosures(
            config={"dart": {"endpoint": "https://dart.example/list", "api_key_env": "DART_API_KEY"}},
            symbols=["005930"],
            days=7,
            target_date=date(2026, 7, 9),
            env={"DART_API_KEY": "key"},
            opener=opener,
        )


def test_official_market_price_provider_normalizes_public_schema() -> None:
    def opener(request):
        return json.dumps({"response": {"body": {"items": {"item": [{"basDt": "20260708", "srtnCd": "005930", "clpr": "90000", "trqu": "100"}]}}}})

    frame = collect_official_market_prices(
        config={"data_go_kr_prices": {"endpoint": "https://data.example/price", "service_key_env": "DATA_GO_KR_SERVICE_KEY"}},
        target_date=date(2026, 7, 8),
        source="data_go_kr_prices",
        env={"DATA_GO_KR_SERVICE_KEY": "key"},
        opener=opener,
    )

    assert len(frame) == 1
    assert frame.iloc[0]["symbol"] == "005930"
    assert frame.iloc[0]["close"] == 90000.0
