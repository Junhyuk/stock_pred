from __future__ import annotations

import pandas as pd

from roboquant.dashboard.backtest_service import calc_precision_at_k, run_backtest_job
from roboquant.dashboard.dashboard_service import (
    build_dashboard_snapshot,
    get_backtest_by_model,
    get_latest_dashboard_snapshot,
    get_sector_backtest,
    get_top20_backtest,
    get_top20_upside_recommendations,
)
from roboquant.dashboard.gatekeeper_service import decide_model_gate, run_model_gatekeeper
from roboquant.dashboard.portfolio_service import build_portfolio_from_recommendations
from roboquant.db import append_dedup_table, connect_database


def test_prediction_backtest_persists_results_and_query_helpers(tmp_path) -> None:
    conn = connect_database(tmp_path / "v7_backtest.duckdb")
    _seed_predictions_and_labels(conn)

    results, performance = run_backtest_job(conn, horizon_days=60)

    assert len(results) == 30
    assert len(performance) == 1
    assert performance.iloc[0]["model_name"] == "lightgbm"
    assert performance.iloc[0]["sample_count"] == 30
    assert performance.iloc[0]["precision_top20"] == 1.0
    assert performance.iloc[0]["rank_ic"] > 0.7
    assert conn.execute("SELECT COUNT(*) FROM backtest_results").fetchone()[0] == 30
    assert conn.execute("SELECT COUNT(*) FROM model_performance_daily").fetchone()[0] == 1

    top20 = get_top20_backtest(conn, horizon=60)
    by_model = get_backtest_by_model(conn, model="lightgbm", version="v1", horizon=60)
    sector = get_sector_backtest(conn, sector="반도체", horizon=60)

    assert len(top20) == 20
    assert len(by_model) == 30
    assert sector["summary"]["sample_count"] == 15
    assert calc_precision_at_k(results, k=20) == 1.0


def test_dashboard_snapshot_builds_from_real_db_tables(tmp_path) -> None:
    conn = connect_database(tmp_path / "v7_dashboard.duckdb")
    _seed_predictions_and_labels(conn)
    _seed_recommendations_and_features(conn)
    run_backtest_job(conn, horizon_days=60)

    snapshot = build_dashboard_snapshot(conn, horizon="3M")
    stored = get_latest_dashboard_snapshot(conn)

    assert snapshot["snapshot_date"] == "2024-01-02"
    assert len(snapshot["ai_recommendations"]) == 20
    assert len(snapshot["core_portfolio"]) == 8
    assert snapshot["backtest_summary"]["sample_count"] == 30
    assert stored["snapshot_date"] == snapshot["snapshot_date"]
    assert conn.execute("SELECT COUNT(*) FROM dashboard_snapshot").fetchone()[0] == 1


def test_empty_dashboard_snapshot_does_not_write_to_db(tmp_path) -> None:
    conn = connect_database(tmp_path / "empty_dashboard.duckdb")

    snapshot = get_latest_dashboard_snapshot(conn)

    assert snapshot["ai_recommendations"] == []
    assert snapshot["model_accuracy"] == []
    assert conn.execute("SELECT COUNT(*) FROM dashboard_snapshot").fetchone()[0] == 0


def test_portfolio_weight_caps_leave_extra_cash() -> None:
    recommendations = pd.DataFrame(
        {
            "symbol": [f"{idx:06d}" for idx in range(1, 6)],
            "name": [f"종목{idx}" for idx in range(1, 6)],
            "sector": ["반도체"] * 5,
            "final_score": [0.9, 0.8, 0.7, 0.6, 0.5],
            "target_upside_score": [0.6] * 5,
            "risk_score": [0.4] * 5,
        }
    )

    portfolio = build_portfolio_from_recommendations(recommendations, profile="neutral", limit=5)
    total_weight = sum(item["weight"] for item in portfolio["items"])

    assert total_weight <= 0.30
    assert portfolio["cash_ratio"] >= 0.70
    assert max(item["weight"] for item in portfolio["items"]) <= 0.15


def test_v7_gatekeeper_accepts_or_rejects_against_baseline(tmp_path) -> None:
    baseline = {
        "sample_count": 250,
        "precision_top20": 0.60,
        "avg_excess_return": 0.02,
        "rank_ic": 0.10,
        "mdd": -0.10,
    }
    accepted = decide_model_gate(
        {
            "sample_count": 250,
            "precision_top20": 0.61,
            "avg_excess_return": 0.03,
            "rank_ic": 0.03,
            "mdd": -0.11,
        },
        baseline,
    )
    rejected = decide_model_gate(
        {
            "sample_count": 250,
            "precision_top20": 0.50,
            "avg_excess_return": 0.03,
            "rank_ic": 0.03,
            "mdd": -0.11,
        },
        baseline,
    )

    assert accepted["gate_status"] == "accepted"
    assert accepted["production_weight"] == 0.1
    assert rejected["gate_status"] == "rejected"
    assert rejected["production_weight"] == 0.0

    conn = connect_database(tmp_path / "v7_gate.duckdb")
    _seed_model_performance(conn)
    decisions = run_model_gatekeeper(conn, baseline_model="lightgbm", min_sample_count=200)

    patch = decisions[decisions["model_name"] == "patchtst_v7"].iloc[0]
    registry = conn.execute("SELECT status, production_weight FROM model_registry WHERE model_name = 'patchtst_v7'").fetchdf()
    assert patch["gate_status"] == "rejected"
    assert patch["production_weight"] == 0.0
    assert registry.iloc[0]["status"] == "rejected"
    assert registry.iloc[0]["production_weight"] == 0.0


def test_fastapi_health_and_local_pages() -> None:
    import sys
    from pathlib import Path

    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.main import app

    client = TestClient(app)

    assert client.get("/health").json() == {"status": "ok"}
    assert "AI Robo Quant" in client.get("/dashboard").text
    assert "Backtest 검증" in client.get("/backtest").text
    demo_page = client.get("/demo/two-stocks").text
    assert "삼성전자 · 에스엘 예측 데모" in demo_page
    assert 'id="twoStockDemoPage"' in demo_page
    focus_demo_page = client.get("/demo/focus-stocks").text
    assert "삼성전자 · SK하이닉스 · 에스엘 글로벌 보정 데모" in focus_demo_page
    assert 'id="focusStocksDemoPage"' in focus_demo_page
    four_demo_page = client.get("/demo/four-stocks").text
    assert "삼성전자 · SK하이닉스 · LG전자 · 에스엘 예측 데모" in four_demo_page
    assert 'id="fourStocksDemoPage"' in four_demo_page
    upside_page = client.get("/recommendations/top20-upside").text
    assert "3개월 상승확률·상승여력 Top20" in upside_page
    assert 'id="top20UpsidePage"' in upside_page
    assert 'id="top20PriceForecastTable"' in upside_page
    assert "3M/6M/9M/1Y 예상 상승·하락 가격" in upside_page
    stock_page = client.get("/stock/005930").text
    assert 'id="stockPage" data-symbol="005930"' in stock_page
    assert "Promise.allSettled" in stock_page
    assert "수집된 애널리스트 리포트가 없습니다." in stock_page
    assert stock_page.index("async function bootStock") < stock_page.index(
        'document.addEventListener("DOMContentLoaded"'
    )


def test_two_stock_demo_api_returns_focus_symbols(tmp_path, monkeypatch) -> None:
    import sys
    from pathlib import Path

    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import main as app_main

    db_path = tmp_path / "two_stock_api.duckdb"
    conn = connect_database(db_path)
    _seed_two_stock_demo(conn)
    conn.close()

    def _test_conn():
        return connect_database(db_path, read_only=True, initialize_schema=False)

    monkeypatch.setattr(app_main, "_conn", _test_conn)
    client = TestClient(app_main.app)

    response = client.get("/api/demo/two-stocks?horizon=3M")

    assert response.status_code == 200
    payload = response.json()
    assert payload["horizon"] == "3M"
    assert [item["symbol"] for item in payload["items"]] == ["005930", "005850"]
    sl = next(item for item in payload["items"] if item["symbol"] == "005850")
    assert sl["name"] == "에스엘"
    assert sl["prediction"]["pred_prob_top20"] == 0.4
    assert sl["latest_price"]["close"] == 35_000
    assert sl["cluster"]["cluster_label"] == "중립 혼합"


def test_focus_stocks_demo_api_returns_global_adjustment(tmp_path, monkeypatch) -> None:
    import sys
    from pathlib import Path

    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import main as app_main

    db_path = tmp_path / "focus_stock_api.duckdb"
    conn = connect_database(db_path)
    _seed_two_stock_demo(conn)
    conn.execute(
        """
        INSERT INTO market_regime_daily (
            prediction_date, prediction_cutoff, us_equity_score, semiconductor_score,
            asia_score, volatility_score, rate_score, fx_score, commodity_score, global_risk_score,
            regime, recommended_cash_ratio, signals_json, reasons_json, feature_version
        )
        VALUES (
            DATE '2026-06-08', TIMESTAMP '2026-06-08 08:00:00',
            45, 25, 0, 15, 0, 10, 0, 95,
            'panic', 0.50,
            '{"sox_return_1d": -0.05}',
            '["SOX 급락", "Nasdaq 급락", "USD/KRW 상승"]',
            'domestic_plus_global_regime_v1'
        )
        """
    )
    conn.close()

    def _test_conn():
        return connect_database(db_path, read_only=True, initialize_schema=False)

    monkeypatch.setattr(app_main, "_conn", _test_conn)
    client = TestClient(app_main.app)

    response = client.get("/api/demo/focus-stocks?horizon=3M")

    assert response.status_code == 200
    payload = response.json()
    assert payload["regime"]["status"] == "ready"
    assert payload["regime"]["regime"] == "panic"
    assert [item["symbol"] for item in payload["items"]] == ["005930", "000660", "005850"]
    samsung = next(item for item in payload["items"] if item["symbol"] == "005930")
    hynix = next(item for item in payload["items"] if item["symbol"] == "000660")
    sl = next(item for item in payload["items"] if item["symbol"] == "005850")
    assert samsung["global_adjustment"]["status"] == "ready"
    assert samsung["global_adjustment"]["regime_adjusted_score"] < samsung["display_score"]
    assert hynix["global_sensitivity"] == ["SOX", "Nasdaq", "TSM", "USD/KRW"]
    assert "Dow" in sl["global_sensitivity"]

    assert client.get("/api/market-regime/current").json()["regime"] == "panic"
    assert client.get("/api/global-markets/latest").json()["status"] == "not_collected"


def test_four_stock_demo_api_returns_lg_prediction_fallback_and_price_gap(tmp_path, monkeypatch) -> None:
    import sys
    from pathlib import Path

    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import main as app_main

    db_path = tmp_path / "four_stock_api.duckdb"
    conn = connect_database(db_path)
    _seed_two_stock_demo(conn)
    conn.close()

    def _test_conn():
        return connect_database(db_path, read_only=True, initialize_schema=False)

    monkeypatch.setattr(app_main, "_conn", _test_conn)
    client = TestClient(app_main.app)

    response = client.get("/api/demo/four-stocks?horizon=3M")

    assert response.status_code == 200
    payload = response.json()
    assert payload["horizon"] == "3M"
    assert [item["symbol"] for item in payload["items"]] == ["005930", "000660", "066570", "005850"]
    lg = next(item for item in payload["items"] if item["symbol"] == "066570")
    assert lg["name"] == "LG전자"
    assert lg["score_source"] == "prediction_probability"
    assert lg["display_score"] == lg["prediction"]["pred_prob_top20"]
    assert lg["top20_status"] == "Top20 밖 / 예측값 있음"
    assert lg["global_sensitivity"] == ["SOX", "Nasdaq", "TSM", "USD/KRW"]
    assert lg["price_gap"]["symbol"] == "066570"
    assert payload["price_gap_summary"]["sample_count"] == 4


def test_top20_upside_recommendations_builder_and_page(tmp_path) -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.pages import top20_upside_html, top50_universe_html

    db_path = tmp_path / "top20_upside.duckdb"
    conn = connect_database(db_path)
    _seed_top20_upside_fixture(conn)

    payload = get_top20_upside_recommendations(conn, horizon="3M", limit=3)
    assert [item["symbol"] for item in payload["items"]] == ["000002", "000003", "000001"]
    assert payload["items"][0]["upside_return"] == 0.50
    assert payload["summary"]["count"] == 3
    conn.close()

    page = top20_upside_html().body.decode("utf-8")
    assert "3개월 상승확률·상승여력 Top20" in page
    assert 'id="top20UpsidePage"' in page

    top50_page = top50_universe_html().body.decode("utf-8")
    assert "Top50 예측 유니버스" in top50_page
    assert 'id="top50Page"' in top50_page


def _seed_top20_upside_fixture(conn) -> None:
    symbols = pd.DataFrame(
        {
            "symbol": ["000001", "000002", "000003"],
            "name": ["확률우위", "여력우위", "균형형"],
            "market": ["KOSPI", "KOSPI", "KOSDAQ"],
            "sector": ["반도체", "자동차", "바이오"],
            "is_active": [True, True, True],
        }
    )
    recommendations = pd.DataFrame(
        {
            "asof_date": ["2026-06-09"] * 3,
            "horizon": ["3M"] * 3,
            "symbol": symbols["symbol"],
            "final_score": [0.91, 0.72, 0.83],
            "rank": [1, 2, 3],
            "reason_json": ["[]"] * 3,
            "risk_flags_json": ["[]"] * 3,
            "model_version": ["v1"] * 3,
        }
    )
    predictions = pd.DataFrame(
        {
            "asof_date": ["2026-06-09"] * 3,
            "symbol": symbols["symbol"],
            "horizon": ["3M"] * 3,
            "pred_return": [0.01, 0.50, 0.20],
            "pred_prob_top20": [0.90, 0.70, 0.80],
            "pred_risk": [0.30, 0.40, 0.35],
            "confidence": [0.90, 0.70, 0.80],
            "model_version": ["v1"] * 3,
        }
    )
    features = pd.DataFrame(
        {
            "date": ["2026-06-09"] * 3,
            "symbol": symbols["symbol"],
            "horizon": ["3M"] * 3,
            "horizon_days": [60] * 3,
            "risk_score": [0.30, 0.40, 0.35],
            "target_upside_score": [0.5, 0.5, 0.5],
            "trading_value_ma20": [1_000_000_000] * 3,
        }
    )
    append_dedup_table(conn, "symbols", symbols, ["symbol"])
    append_dedup_table(conn, "recommendations", recommendations, ["asof_date", "horizon", "symbol", "model_version"])
    append_dedup_table(conn, "predictions", predictions, ["asof_date", "symbol", "horizon", "model_version"])
    append_dedup_table(conn, "features_daily", features, ["date", "symbol", "horizon"])


def _seed_predictions_and_labels(conn, n: int = 30) -> None:
    symbols = pd.DataFrame(
        {
            "symbol": [f"{idx:06d}" for idx in range(1, n + 1)],
            "name": [f"종목{idx}" for idx in range(1, n + 1)],
            "market": ["KOSPI"] * n,
            "sector": ["반도체" if idx <= 15 else "바이오" for idx in range(1, n + 1)],
            "is_active": [True] * n,
        }
    )
    predictions = pd.DataFrame(
        {
            "asof_date": ["2024-01-02"] * n,
            "symbol": symbols["symbol"],
            "horizon": ["3M"] * n,
            "pred_return": [(n - idx) / 100 for idx in range(n)],
            "pred_prob_top20": [(n - idx) / n for idx in range(n)],
            "pred_risk": [0.3] * n,
            "confidence": [0.7] * n,
            "model_version": ["v1"] * n,
        }
    )
    labels = pd.DataFrame(
        {
            "asof_date": ["2024-01-02"] * n,
            "symbol": symbols["symbol"],
            "horizon": ["3M"] * n,
            "horizon_days": [60] * n,
            "future_return": [0.05 if idx <= 20 else -0.03 for idx in range(1, n + 1)],
            "benchmark_return": [0.01] * n,
            "excess_return": [0.04 if idx <= 20 else -0.04 for idx in range(1, n + 1)],
            "rank_quantile": [idx / n for idx in range(1, n + 1)],
            "is_top20pct": [idx <= 6 for idx in range(1, n + 1)],
            "max_drawdown_forward": [-0.05] * n,
        }
    )
    append_dedup_table(conn, "symbols", symbols, ["symbol"])
    append_dedup_table(conn, "predictions", predictions, ["asof_date", "symbol", "horizon", "model_version"])
    append_dedup_table(conn, "labels", labels, ["asof_date", "symbol", "horizon"])


def _seed_recommendations_and_features(conn, n: int = 30) -> None:
    symbols = [f"{idx:06d}" for idx in range(1, n + 1)]
    recommendations = pd.DataFrame(
        {
            "asof_date": ["2024-01-02"] * n,
            "horizon": ["3M"] * n,
            "symbol": symbols,
            "final_score": [(n - idx + 1) / n for idx in range(1, n + 1)],
            "rank": list(range(1, n + 1)),
            "reason_json": ['["모멘텀", "수급", "리스크"]'] * n,
            "risk_flags_json": ["[]"] * n,
            "model_version": ["v1"] * n,
        }
    )
    features = pd.DataFrame(
        {
            "date": ["2024-01-02"] * n,
            "symbol": symbols,
            "horizon": ["3M"] * n,
            "horizon_days": [60] * n,
            "momentum_score": [0.7] * n,
            "risk_score": [0.3] * n,
            "supply_demand_score": [0.6] * n,
            "liquidity_score": [0.8] * n,
            "trading_value_ma20": [1_000_000_000] * n,
            "target_upside_score": [0.55] * n,
        }
    )
    append_dedup_table(conn, "recommendations", recommendations, ["asof_date", "horizon", "symbol", "model_version"])
    append_dedup_table(conn, "features_daily", features, ["date", "symbol", "horizon"])


def _seed_model_performance(conn) -> None:
    now = pd.Timestamp("2024-06-01 00:00:00")
    performance = pd.DataFrame(
        [
            {
                "eval_date": "2024-06-01",
                "model_name": "lightgbm",
                "model_version": "v1",
                "horizon": "3M",
                "horizon_days": 60,
                "sample_count": 250,
                "precision_top20": 0.60,
                "avg_excess_return": 0.02,
                "hit_ratio": 0.58,
                "mdd": -0.10,
                "rank_ic": 0.10,
                "production_weight": 1.0,
                "gate_status": "production",
                "created_at": now,
            },
            {
                "eval_date": "2024-06-01",
                "model_name": "patchtst_v7",
                "model_version": "v1",
                "horizon": "3M",
                "horizon_days": 60,
                "sample_count": 250,
                "precision_top20": 0.55,
                "avg_excess_return": 0.03,
                "hit_ratio": 0.59,
                "mdd": -0.11,
                "rank_ic": 0.05,
                "production_weight": 0.0,
                "gate_status": "candidate",
                "created_at": now,
            },
        ]
    )
    registry = pd.DataFrame(
        [
            {
                "model_name": "patchtst_v7",
                "model_type": "patchtst",
                "feature_set_name": "feature_set_v1",
                "label_name": "is_top20pct",
                "horizons": "3M",
                "status": "experimental",
                "production_weight": 0.0,
                "shadow_mode": True,
                "created_at": now,
                "updated_at": now,
            }
        ]
    )
    append_dedup_table(conn, "model_performance_daily", performance, ["eval_date", "model_name", "model_version", "horizon"])
    append_dedup_table(conn, "model_registry", registry, ["model_name"])


def _seed_two_stock_demo(conn) -> None:
    symbols = pd.DataFrame(
        {
            "symbol": ["005930", "000660", "066570", "005850"],
            "name": ["삼성전자", "SK하이닉스", "LG전자", "에스엘"],
            "market": ["KOSPI", "KOSPI", "KOSPI", "KOSPI"],
            "sector": ["반도체", "반도체", "전자제품", "자동차부품"],
            "is_active": [True, True, True, True],
        }
    )
    prices = pd.DataFrame(
        {
            "date": ["2026-06-05", "2026-06-05", "2026-06-05", "2026-06-05"],
            "symbol": ["005930", "000660", "066570", "005850"],
            "open": [60_000, 210_000, 92_000, 34_000],
            "high": [61_000, 214_000, 94_000, 35_500],
            "low": [59_000, 208_000, 91_000, 33_800],
            "close": [60_500, 212_000, 93_000, 35_000],
            "adj_close": [60_500, 212_000, 93_000, 35_000],
            "volume": [1_000_000, 800_000, 500_000, 200_000],
            "trading_value": [60_500_000_000, 169_600_000_000, 46_500_000_000, 7_000_000_000],
            "source": ["fixture", "fixture", "fixture", "fixture"],
        }
    )
    features = pd.DataFrame(
        {
            "date": ["2026-06-05", "2026-06-05", "2026-06-05", "2026-06-05"],
            "symbol": ["005930", "000660", "066570", "005850"],
            "horizon": ["3M", "3M", "3M", "3M"],
            "horizon_days": [63, 63, 63, 63],
            "momentum_score": [0.7, 0.68, 0.52, 0.55],
            "risk_score": [0.3, 0.35, 0.42, 0.45],
            "supply_demand_score": [0.6, 0.58, 0.51, 0.5],
            "liquidity_score": [0.9, 0.88, 0.82, 0.7],
            "trading_value_ma20": [50_000_000_000, 130_000_000_000, 40_000_000_000, 5_000_000_000],
        }
    )
    predictions = pd.DataFrame(
        {
            "asof_date": ["2026-06-05", "2026-06-05", "2026-06-05", "2026-06-05"],
            "symbol": ["005930", "000660", "066570", "005850"],
            "horizon": ["3M", "3M", "3M", "3M"],
            "pred_return": [0.08, 0.07, 0.025, 0.03],
            "pred_prob_top20": [0.7, 0.65, 0.38, 0.4],
            "pred_risk": [0.3, 0.35, 0.42, 0.45],
            "confidence": [0.7, 0.68, 0.58, 0.6],
            "model_version": ["demo", "demo", "demo", "demo"],
        }
    )
    recommendations = pd.DataFrame(
        {
            "asof_date": ["2026-06-05"],
            "horizon": ["3M"],
            "symbol": ["005930"],
            "final_score": [0.72],
            "rank": [1],
            "reason_json": ['["Top20 진입 확률이 상대적으로 높음"]'],
            "risk_flags_json": ["[]"],
            "model_version": ["demo"],
        }
    )
    clusters = pd.DataFrame(
        {
            "asof_date": ["2026-06-05", "2026-06-05", "2026-06-05", "2026-06-05"],
            "horizon": ["3M", "3M", "3M", "3M"],
            "symbol": ["005930", "000660", "066570", "005850"],
            "cluster_id": [1, 1, 1, 1],
            "cluster_label": ["중립 혼합", "중립 혼합", "중립 혼합", "중립 혼합"],
            "distance_to_centroid": [0.1, 0.15, 0.18, 0.2],
            "feature_values_json": ["{}", "{}", "{}", "{}"],
            "model_version": ["fixture", "fixture", "fixture", "fixture"],
        }
    )
    append_dedup_table(conn, "symbols", symbols, ["symbol"])
    append_dedup_table(conn, "prices_daily", prices, ["date", "symbol"])
    append_dedup_table(conn, "features_daily", features, ["date", "symbol", "horizon"])
    append_dedup_table(conn, "predictions", predictions, ["asof_date", "symbol", "horizon", "model_version"])
    append_dedup_table(conn, "recommendations", recommendations, ["asof_date", "horizon", "symbol", "model_version"])
    append_dedup_table(conn, "stock_clusters", clusters, ["asof_date", "horizon", "symbol", "model_version"])
