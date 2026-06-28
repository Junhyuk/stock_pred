from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from roboquant.db import append_dedup_table, connect_database
from roboquant.data.loaders import load_prediction_dataset
from roboquant.long_short import run_long_short_backtest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_long_short_backtest_uses_rank_spread_formula() -> None:
    frame = pd.DataFrame(
        {
            "asof_date": ["2024-01-31"] * 4 + ["2024-02-29"] * 4,
            "symbol": ["000001", "000002", "000003", "000004"] * 2,
            "horizon": ["2M"] * 8,
            "pred_return": [0.2, 0.1, -0.1, -0.2, 0.1, 0.0, -0.1, -0.2],
            "pred_prob_top20": [0.9, 0.7, 0.3, 0.1, 0.9, 0.7, 0.3, 0.1],
            "pred_prob_bottom20": [0.1, 0.3, 0.7, 0.9, 0.1, 0.3, 0.7, 0.9],
            "long_score": [0.9, 0.7, 0.3, 0.1, 0.9, 0.7, 0.3, 0.1],
            "short_score": [0.1, 0.3, 0.7, 0.9, 0.1, 0.3, 0.7, 0.9],
            "confidence": [0.9] * 8,
            "model_version": ["test"] * 8,
            "future_return": [0.10, 0.03, 0.00, -0.04, 0.04, 0.02, 0.00, 0.02],
        }
    )

    curve, summary = run_long_short_backtest(
        frame,
        "2M",
        config={
            "long_short": {
                "long_count": 1,
                "short_count": 1,
                "gross_long": 0.5,
                "gross_short": 0.5,
                "rebalance_frequency": {"2M": "M"},
                "transaction_cost_bps": 0,
            }
        },
    )

    assert len(curve) == 2
    assert np.isclose(curve.iloc[0]["net_return"], 0.5 * 0.10 - 0.5 * -0.04)
    assert np.isclose(curve.iloc[1]["net_return"], 0.5 * 0.04 - 0.5 * 0.02)
    assert np.isclose(curve.iloc[-1]["equity"], 1.07 * 1.01)
    assert summary["long_count"] == 1
    assert summary["short_count"] == 1


def test_long_short_backtest_respects_quarterly_rebalance() -> None:
    rows = []
    for asof in ["2024-01-31", "2024-02-29", "2024-03-29", "2024-04-30"]:
        rows.extend(
            [
                _prediction(asof, "000001", "KOSPI", "1Y", long_score=0.9, short_score=0.1, future_return=0.03),
                _prediction(asof, "000002", "KOSPI", "1Y", long_score=0.1, short_score=0.9, future_return=-0.02),
            ]
        )

    curve, _ = run_long_short_backtest(
        pd.DataFrame(rows),
        "1Y",
        config={
            "long_short": {
                "long_count": 1,
                "short_count": 1,
                "rebalance_frequency": {"1Y": "Q"},
                "transaction_cost_bps": 0,
            }
        },
    )

    assert curve["asof_date"].astype(str).tolist() == ["2024-03-29", "2024-04-30"]


def test_long_short_backtest_splits_by_market() -> None:
    rows = []
    for asof in ["2024-01-31", "2024-02-29"]:
        rows.extend(
            [
                _prediction(asof, "000001", "KOSPI", "6M", long_score=0.9, short_score=0.1, future_return=0.05),
                _prediction(asof, "000002", "KOSPI", "6M", long_score=0.1, short_score=0.9, future_return=-0.03),
                _prediction(asof, "200001", "KOSDAQ", "6M", long_score=0.8, short_score=0.2, future_return=0.04),
                _prediction(asof, "200002", "KOSDAQ", "6M", long_score=0.2, short_score=0.8, future_return=-0.02),
            ]
        )

    curve, summary = run_long_short_backtest(
        pd.DataFrame(rows),
        "6M",
        config={
            "long_short": {
                "long_count": 2,
                "short_count": 2,
                "gross_long": 0.5,
                "gross_short": 0.5,
                "rebalance_frequency": {"6M": "M"},
                "transaction_cost_bps": 0,
                "market_split": {"enabled": True, "kospi_target": 30, "kosdaq_target": 20},
            }
        },
    )

    assert len(curve) == 4
    assert set(curve["market"].dropna()) == {"KOSPI", "KOSDAQ"}
    assert summary["market_split"] is True


def test_prediction_dataset_loads_market_context_from_symbols(tmp_path) -> None:
    conn = connect_database(tmp_path / "prediction_dataset.duckdb")
    append_dedup_table(
        conn,
        "symbols",
        pd.DataFrame(
            {
                "symbol": ["000001"],
                "name": ["Alpha"],
                "market": ["KOSPI"],
                "sector": ["반도체"],
            }
        ),
        ["symbol"],
    )
    append_dedup_table(
        conn,
        "features_daily",
        pd.DataFrame(
            {
                "date": ["2024-01-31"],
                "symbol": ["000001"],
                "horizon": ["2M"],
                "horizon_days": [42],
                "trading_value_ma20": [2_000_000_000.0],
            }
        ),
        ["date", "symbol", "horizon"],
    )
    append_dedup_table(
        conn,
        "labels",
        pd.DataFrame(
            {
                "asof_date": ["2024-01-31"],
                "symbol": ["000001"],
                "horizon": ["2M"],
                "future_return": [0.12],
            }
        ),
        ["asof_date", "symbol", "horizon"],
    )
    append_dedup_table(
        conn,
        "predictions",
        pd.DataFrame(
            {
                "asof_date": ["2024-01-31"],
                "symbol": ["000001"],
                "horizon": ["2M"],
                "pred_return": [0.1],
                "pred_prob_top20": [0.8],
                "pred_prob_bottom20": [0.2],
                "long_score": [0.8],
                "short_score": [0.2],
                "confidence": [0.7],
                "model_version": ["test"],
            }
        ),
        ["asof_date", "symbol", "horizon"],
    )

    dataset = load_prediction_dataset(conn, "2M")

    assert dataset.iloc[0]["market"] == "KOSPI"
    assert dataset.iloc[0]["name"] == "Alpha"
    assert dataset.iloc[0]["sector"] == "반도체"


def test_long_short_api_smoke(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "long_short_api.duckdb"
    conn = connect_database(db_path)
    append_dedup_table(
        conn,
        "symbols",
        pd.DataFrame(
            {
                "symbol": ["000001", "000002"],
                "name": ["Alpha", "Beta"],
                "market": ["KOSPI", "KOSDAQ"],
            }
        ),
        ["symbol"],
    )
    append_dedup_table(
        conn,
        "long_short_recommendations",
        pd.DataFrame(
            {
                "asof_date": ["2024-05-31", "2024-05-31"],
                "horizon": ["2M", "2M"],
                "market": ["KOSPI", "KOSDAQ"],
                "symbol": ["000001", "000002"],
                "side": ["LONG", "SHORT"],
                "leg_rank": [1, 1],
                "long_score": [0.9, 0.1],
                "short_score": [0.1, 0.9],
                "pred_return": [0.08, -0.04],
                "pred_prob_top20": [0.8, 0.2],
                "pred_prob_bottom20": [0.2, 0.8],
                "risk_score": [0.2, 0.6],
                "confidence": [0.8, 0.8],
                "weight": [0.5, -0.5],
                "reason_json": ['["LONG 관심도 0.90"]', '["SHORT 관심도 0.90"]'],
                "risk_flags_json": ["[]", '["모의 숏 레그"]'],
                "model_version": ["test", "test"],
                "created_at": [pd.Timestamp("2024-05-31")] * 2,
            }
        ),
        ["asof_date", "horizon", "market", "symbol", "side", "model_version"],
    )
    append_dedup_table(
        conn,
        "long_short_backtest_results",
        pd.DataFrame(
            {
                "asof_date": ["2024-03-29"],
                "horizon": ["1Y"],
                "market": ["KOSPI"],
                "long_symbols": ["000001"],
                "short_symbols": ["000002"],
                "long_return": [0.10],
                "short_return": [-0.02],
                "gross_spread_return": [0.06],
                "transaction_cost": [0.0],
                "net_return": [0.06],
                "turnover": [1.0],
                "equity": [1.06],
                "metrics_json": ['{"periods": 1, "final_equity": 1.06}'],
                "model_version": ["test"],
                "created_at": [pd.Timestamp("2024-03-29")],
            }
        ),
        ["asof_date", "horizon", "market", "model_version"],
    )
    conn.close()

    import app.main as main

    monkeypatch.setattr(
        main,
        "_conn",
        lambda: connect_database(db_path, read_only=True, initialize_schema=False),
    )
    latest = main.long_short_latest(horizon="2M")
    backtest = main.long_short_backtest(horizon="1Y", market="KOSPI")

    assert latest["markets"]["KOSPI"]["long_leg"][0]["symbol"] == "000001"
    assert latest["markets"]["KOSDAQ"]["short_leg"][0]["symbol"] == "000002"
    assert latest["long_leg"][0]["symbol"] == "000001"
    assert latest["short_leg"][0]["symbol"] == "000002"
    assert backtest["summary"]["final_equity"] == 1.06


def test_long_short_page_renders() -> None:
    from app.pages import long_short_html

    page = long_short_html().body.decode("utf-8")
    assert "Top50 시장별 롱·숏 추천" in page
    assert 'id="longShortPage"' in page


def _prediction(
    asof_date: str,
    symbol: str,
    market: str,
    horizon: str,
    long_score: float,
    short_score: float,
    future_return: float,
) -> dict[str, object]:
    return {
        "asof_date": asof_date,
        "symbol": symbol,
        "market": market,
        "horizon": horizon,
        "pred_return": long_score - short_score,
        "pred_prob_top20": long_score,
        "pred_prob_bottom20": short_score,
        "long_score": long_score,
        "short_score": short_score,
        "confidence": max(long_score, short_score),
        "model_version": "test",
        "future_return": future_return,
    }
