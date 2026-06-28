from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from roboquant.dashboard.dashboard_service import get_market_move_explanations
from roboquant.db import append_dedup_table, connect_database
from roboquant.signals.market_move_explanations import (
    build_market_move_explanations,
    refresh_market_move_explanations,
)
from roboquant.signals.telegram_signals import normalize_telegram_message


def test_market_move_explanations_trigger_and_flat_rows(tmp_path) -> None:
    conn = connect_database(tmp_path / "moves.duckdb")
    _seed_symbols_and_prices(conn)
    _seed_explanation_context(conn)
    _seed_prediction_context(conn)

    frame = build_market_move_explanations(conn, {"market_move_explanations": {"threshold": 0.02}})

    samsung = frame[(frame["scope"] == "top50") & (frame["symbol"] == "005930")].iloc[0]
    hynix = frame[(frame["scope"] == "top50") & (frame["symbol"] == "000660")].iloc[0]
    prediction_context = json.loads(samsung["prediction_context_json"])
    assert bool(samsung["triggered"]) is True
    assert samsung["direction"] == "DOWN"
    assert "반도체" in samsung["primary_reason"] or "기술주" in samsung["primary_reason"]
    samsung_evidence = json.loads(samsung["evidence_json"])
    assert any(item["kind"] == "us_sector" for item in samsung_evidence)
    assert prediction_context["horizon"] == "2M"
    assert prediction_context["side"] == "DOWN"
    assert prediction_context["gate_status"] == "accepted"
    assert bool(hynix["triggered"]) is False
    assert hynix["primary_reason"] == "2% 이상 변동 없음"

    payload = get_market_move_explanations(conn, config={"market_move_explanations": {"threshold": 0.02}})
    assert payload["summary"]["triggered_count"] >= 2
    samsung_payload = next(item for item in payload["items"] if item["symbol"] == "005930")
    assert samsung_payload["evidence"]
    assert samsung_payload["prediction_context"]["side"] == "DOWN"


def test_market_move_explanations_handles_missing_evidence(tmp_path) -> None:
    conn = connect_database(tmp_path / "missing.duckdb")
    _seed_symbols_and_prices(conn, include_flat=False)

    frame = build_market_move_explanations(conn, {"market_move_explanations": {"threshold": 0.02}})
    samsung = frame[(frame["scope"] == "top50") & (frame["symbol"] == "005930")].iloc[0]
    quality = json.loads(samsung["data_quality_json"])

    assert bool(samsung["triggered"]) is True
    assert "가격 기반 변동" in samsung["primary_reason"]
    assert quality["status"] == "partial_ready"
    assert "investor_flows" in quality["components"]


def test_market_shock_trigger_uses_kospi_kosdaq_index_returns(tmp_path) -> None:
    conn = connect_database(tmp_path / "index_shock.duckdb")
    _seed_symbols_and_prices(conn, include_flat=True)
    _seed_benchmark_indices(conn, kospi_latest=97.9, kosdaq_latest=100.5)
    _seed_telegram_context(conn)

    frame = build_market_move_explanations(conn, {"market_move_explanations": {"threshold": 0.02}})
    kospi = frame[(frame["scope"] == "market") & (frame["symbol"] == "KOSPI")].iloc[0]
    trigger = json.loads(kospi["market_index_trigger_json"])
    evidence = json.loads(kospi["evidence_json"])

    assert bool(kospi["triggered"]) is True
    assert kospi["move_pct"] <= -0.02
    assert trigger["markets"]["KOSPI"]["triggered"] is True
    assert any(item["kind"] == "telegram" for item in evidence)


def test_top50_drop_alone_does_not_create_market_shock(tmp_path) -> None:
    conn = connect_database(tmp_path / "index_not_shock.duckdb")
    _seed_symbols_and_prices(conn, include_flat=True)
    _seed_benchmark_indices(conn, kospi_latest=100.5, kosdaq_latest=100.2)

    frame = build_market_move_explanations(conn, {"market_move_explanations": {"threshold": 0.02}})
    kospi = frame[(frame["scope"] == "market") & (frame["symbol"] == "KOSPI")].iloc[0]
    samsung = frame[(frame["scope"] == "top50") & (frame["symbol"] == "005930")].iloc[0]

    assert bool(samsung["triggered"]) is True
    assert bool(kospi["triggered"]) is False


def test_market_move_api_and_today_page_render(tmp_path, monkeypatch) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import main as app_main

    db_path = tmp_path / "api.duckdb"
    conn = connect_database(db_path)
    _seed_symbols_and_prices(conn)
    _seed_explanation_context(conn)
    _seed_prediction_context(conn)
    refresh_market_move_explanations(conn, {"market_move_explanations": {"threshold": 0.02}})
    conn.close()

    def _test_conn():
        return connect_database(db_path, read_only=True, initialize_schema=False)

    monkeypatch.setattr(app_main, "_conn", _test_conn)
    response = app_main.market_move_explanations(scope="top50")
    assert response["summary"]["triggered_count"] >= 2
    assert response["items"][0].get("prediction_context") is not None
    snapshot = app_main.today_update_snapshot()
    assert snapshot["move_explanations"]["top50"]
    koru = app_main.koru_linkage()
    assert "leverage_warning" in koru
    page = app_main.today_market_page()
    assert page.status_code == 200
    assert "오늘 급등락 원인 분석" in page.body.decode("utf-8")


def _seed_symbols_and_prices(conn, *, include_flat: bool = True) -> None:
    symbols = [
        {"symbol": "005930", "name": "삼성전자", "market": "KOSPI", "sector": "반도체", "is_active": True},
        {"symbol": "005850", "name": "에스엘", "market": "KOSPI", "sector": "자동차", "is_active": True},
    ]
    if include_flat:
        symbols.append({"symbol": "000660", "name": "SK하이닉스", "market": "KOSPI", "sector": "반도체", "is_active": True})
    append_dedup_table(conn, "symbols", pd.DataFrame(symbols), ["symbol"])

    rows = [
        _price("2026-06-22", "005930", 100.0),
        _price("2026-06-23", "005930", 96.5),
        _price("2026-06-22", "005850", 100.0),
        _price("2026-06-23", "005850", 103.5),
    ]
    if include_flat:
        rows.extend([_price("2026-06-22", "000660", 100.0), _price("2026-06-23", "000660", 101.0)])
    append_dedup_table(conn, "prices_daily", pd.DataFrame(rows), ["date", "symbol", "source"])


def _seed_explanation_context(conn) -> None:
    append_dedup_table(
        conn,
        "investor_flows_daily",
        pd.DataFrame(
            [
                {
                    "date": date(2026, 6, 23),
                    "symbol": "005930",
                    "foreign_net_value": -1_000_000_000.0,
                    "institution_net_value": -200_000_000.0,
                    "retail_net_value": 1_200_000_000.0,
                    "pension_net_value": -50_000_000.0,
                    "source": "fixture",
                    "collected_at": datetime(2026, 6, 23, 15),
                },
                {
                    "date": date(2026, 6, 23),
                    "symbol": "005850",
                    "foreign_net_value": 300_000_000.0,
                    "institution_net_value": 50_000_000.0,
                    "retail_net_value": -350_000_000.0,
                    "source": "fixture",
                    "collected_at": datetime(2026, 6, 23, 15),
                },
            ]
        ),
        ["date", "symbol"],
    )
    append_dedup_table(
        conn,
        "global_market_daily",
        pd.DataFrame(
            [
                {
                    "trade_date": date(2026, 6, 23),
                    "symbol": "^SOX",
                    "market_group": "US_INDEX",
                    "display_name": "SOX",
                    "close": 100.0,
                    "return_1d": -0.031,
                    "return_5d": -0.05,
                    "source_name": "fixture",
                }
            ]
        ),
        ["trade_date", "symbol", "source_name"],
    )
    append_dedup_table(
        conn,
        "us_sector_linkage_daily",
        pd.DataFrame(
            [
                {
                    "trade_date": date(2026, 6, 23),
                    "domestic_sector": "semiconductor",
                    "primary_proxy": "SOXX",
                    "proxy_symbols_json": json.dumps(["SOXX", "^SOX"]),
                    "us_sector_return_1d": -0.025,
                    "us_sector_return_5d": -0.04,
                    "us_sector_zscore_20d": -1.4,
                    "us_sector_beta_60d": 0.8,
                    "us_sector_corr_60d": 0.55,
                    "us_sector_impact_score": 0.72,
                    "us_sector_direction_agreement": 0.68,
                    "sample_count_60d": 60,
                    "data_quality_json": json.dumps({"status": "ready"}),
                    "created_at": datetime(2026, 6, 23, 16),
                },
                {
                    "trade_date": date(2026, 6, 23),
                    "domestic_sector": "auto",
                    "primary_proxy": "DRIV",
                    "proxy_symbols_json": json.dumps(["DRIV", "XLY"]),
                    "us_sector_return_1d": 0.01,
                    "us_sector_return_5d": 0.02,
                    "us_sector_zscore_20d": 0.4,
                    "us_sector_beta_60d": 0.6,
                    "us_sector_corr_60d": 0.35,
                    "us_sector_impact_score": 0.58,
                    "us_sector_direction_agreement": 0.55,
                    "sample_count_60d": 60,
                    "data_quality_json": json.dumps({"status": "ready"}),
                    "created_at": datetime(2026, 6, 23, 16),
                },
                {
                    "trade_date": date(2026, 6, 23),
                    "domestic_sector": "broad",
                    "primary_proxy": "SPY",
                    "proxy_symbols_json": json.dumps(["SPY", "QQQ", "EWY"]),
                    "us_sector_return_1d": -0.01,
                    "us_sector_return_5d": -0.02,
                    "us_sector_zscore_20d": -0.5,
                    "us_sector_beta_60d": 0.5,
                    "us_sector_corr_60d": 0.25,
                    "us_sector_impact_score": 0.52,
                    "us_sector_direction_agreement": 0.5,
                    "sample_count_60d": 60,
                    "data_quality_json": json.dumps({"status": "ready"}),
                    "created_at": datetime(2026, 6, 23, 16),
                },
            ]
        ),
        ["trade_date", "domestic_sector"],
    )
    conn.execute(
        """
        INSERT INTO market_regime_daily (
            prediction_date, prediction_cutoff, us_equity_score, semiconductor_score,
            asia_score, volatility_score, rate_score, fx_score, commodity_score, global_risk_score,
            regime, recommended_cash_ratio, signals_json, reasons_json, feature_version, futures_score
        )
        VALUES (
            DATE '2026-06-23', TIMESTAMP '2026-06-23 08:00:00',
            20, 30, 0, 0, 0, 0, 0, 55,
            'risk_off', 0.20,
            '{"sox_return_1d": -0.031}',
            '["미국 반도체 지수 급락", "원화 약세"]',
            'domestic_plus_global_regime_v1',
            0
        )
        """
    )
    append_dedup_table(
        conn,
        "market_news_feed",
        pd.DataFrame(
            [
                {
                    "article_id": "tech-selloff",
                    "source": "fixture",
                    "category": "sector",
                    "title": "미국 기술주 반도체 selloff와 외국인 차익실현",
                    "summary": "레버리지 ETF 경계와 원화 약세가 함께 언급됐다.",
                    "link": "https://example.com/tech",
                    "pub_date": datetime(2026, 6, 23, 8),
                    "themes_json": json.dumps(["SEMICONDUCTOR", "FLOW"], ensure_ascii=False),
                    "sentiment_score": 0.2,
                    "raw_json": "{}",
                    "collected_at": datetime(2026, 6, 23, 8),
                }
            ]
        ),
        ["article_id"],
    )


def _seed_prediction_context(conn) -> None:
    asof = date(2026, 6, 23)
    append_dedup_table(
        conn,
        "predictions",
        pd.DataFrame(
            [
                {
                    "asof_date": asof,
                    "symbol": "005930",
                    "horizon": "2M",
                    "pred_return": -0.08,
                    "pred_prob_top20": 0.22,
                    "pred_prob_bottom20": 0.71,
                    "long_score": 0.2,
                    "short_score": 0.8,
                    "pred_risk": 0.65,
                    "confidence": 0.7,
                    "model_version": "fixture_v1",
                },
                {
                    "asof_date": asof,
                    "symbol": "005850",
                    "horizon": "2M",
                    "pred_return": 0.05,
                    "pred_prob_top20": 0.66,
                    "pred_prob_bottom20": 0.18,
                    "long_score": 0.7,
                    "short_score": 0.2,
                    "pred_risk": 0.35,
                    "confidence": 0.6,
                    "model_version": "fixture_v1",
                },
            ]
        ),
        ["asof_date", "symbol", "horizon", "model_version"],
    )
    append_dedup_table(
        conn,
        "recommendations",
        pd.DataFrame(
            [
                {
                    "asof_date": asof,
                    "horizon": "2M",
                    "symbol": "005930",
                    "final_score": 0.21,
                    "rank": 42,
                    "reason_json": "[]",
                    "risk_flags_json": "[]",
                    "model_version": "fixture_v1",
                }
            ]
        ),
        ["asof_date", "horizon", "symbol", "model_version"],
    )
    append_dedup_table(
        conn,
        "market_up_down_recommendations",
        pd.DataFrame(
            [
                {
                    "asof_date": asof,
                    "horizon": "2M",
                    "market": "KOSPI",
                    "symbol": "005930",
                    "side": "DOWN",
                    "rank": 1,
                    "pred_return": -0.08,
                    "pred_prob_top20": 0.22,
                    "pred_prob_bottom20": 0.71,
                    "risk_score": 0.65,
                    "confidence": 0.7,
                    "reason_json": "[]",
                    "risk_flags_json": "[]",
                    "model_version": "fixture_v1",
                    "created_at": datetime(2026, 6, 23, 16),
                }
            ]
        ),
        ["asof_date", "horizon", "symbol", "side", "model_version"],
    )
    append_dedup_table(
        conn,
        "long_short_recommendations",
        pd.DataFrame(
            [
                {
                    "asof_date": asof,
                    "horizon": "2M",
                    "market": "KOSPI",
                    "symbol": "005930",
                    "side": "SHORT",
                    "leg_rank": 1,
                    "long_score": 0.2,
                    "short_score": 0.8,
                    "pred_return": -0.08,
                    "pred_prob_top20": 0.22,
                    "pred_prob_bottom20": 0.71,
                    "risk_score": 0.65,
                    "confidence": 0.7,
                    "weight": -0.05,
                    "reason_json": "[]",
                    "risk_flags_json": "[]",
                    "model_version": "fixture_v1",
                    "created_at": datetime(2026, 6, 23, 16),
                }
            ]
        ),
        ["asof_date", "horizon", "symbol", "side", "model_version"],
    )
    append_dedup_table(
        conn,
        "model_performance_daily",
        pd.DataFrame(
            [
                {
                    "eval_date": asof,
                    "model_name": "lightgbm",
                    "model_version": "fixture_v1",
                    "horizon": "2M",
                    "horizon_days": 42,
                    "sample_count": 30,
                    "hit_ratio": 0.6,
                    "precision_top20": 0.55,
                    "avg_actual_return": 0.01,
                    "avg_benchmark_return": 0.0,
                    "avg_excess_return": 0.01,
                    "median_actual_return": 0.01,
                    "win_rate": 0.6,
                    "mdd": -0.1,
                    "sharpe": 0.8,
                    "rank_ic": 0.05,
                    "production_weight": 1.0,
                    "gate_status": "accepted",
                    "created_at": datetime(2026, 6, 23, 16),
                }
            ]
        ),
        ["eval_date", "model_name", "model_version", "horizon"],
    )


def _price(day: str, symbol: str, close: float) -> dict:
    return {
        "date": date.fromisoformat(day),
        "symbol": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000.0,
        "trading_value": close * 1000.0,
        "source": "fixture",
        "collected_at": datetime.fromisoformat(f"{day}T15:30:00"),
    }


def _seed_benchmark_indices(conn, *, kospi_latest: float, kosdaq_latest: float) -> None:
    rows = [
        _benchmark("2026-06-22", "KOSPI", 100.0),
        _benchmark("2026-06-23", "KOSPI", kospi_latest),
        _benchmark("2026-06-22", "KOSDAQ", 100.0),
        _benchmark("2026-06-23", "KOSDAQ", kosdaq_latest),
    ]
    append_dedup_table(conn, "benchmark_daily", pd.DataFrame(rows), ["date", "benchmark"])


def _seed_telegram_context(conn) -> None:
    post, mentions = normalize_telegram_message(
        channel="sypark_strategy",
        message_id=777,
        message_date=datetime(2026, 6, 23, 10),
        text="반도체 급락 외국인 차익실현 환율 부담",
        source_weight=0.9,
    )
    append_dedup_table(conn, "telegram_posts", pd.DataFrame([post]), ["channel", "message_id"])
    append_dedup_table(conn, "telegram_ticker_mentions", pd.DataFrame(mentions), ["mention_id"])


def _benchmark(day: str, benchmark: str, close: float) -> dict:
    return {
        "date": date.fromisoformat(day),
        "benchmark": benchmark,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000.0,
        "trading_value": close * 1000.0,
        "collected_at": datetime.fromisoformat(f"{day}T15:30:00"),
    }
