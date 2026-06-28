from __future__ import annotations

import pandas as pd

from roboquant.db import connect_database
from roboquant.registry.model_registry import (
    load_model_predictions,
    load_model_registry,
    record_backtest_run,
    register_feature_set,
    register_model,
    update_model_status,
    upsert_model_predictions,
)


def test_model_registry_records_status_and_predictions(tmp_path) -> None:
    conn = connect_database(tmp_path / "registry.duckdb")

    register_feature_set(conn, "feature_set_v1", ["momentum_score"], status="production")
    register_model(
        conn,
        model_name="patchtst_test",
        model_type="patchtst",
        feature_set_name="feature_set_v1",
        label_name="is_top20pct",
        horizons=["3M"],
        artifact_path="models/dnn/patchtst_test/model.pt",
    )
    update_model_status(
        conn,
        "patchtst_test",
        status="rejected",
        production_weight=0.0,
        fail_reason="test rejection",
    )

    registry = load_model_registry(conn)
    assert registry.iloc[0]["status"] == "rejected"
    assert registry.iloc[0]["production_weight"] == 0.0
    assert registry.iloc[0]["shadow_mode"]

    predictions = pd.DataFrame(
        {
            "date": ["2024-01-31", "2024-01-31"],
            "symbol": ["1", "2"],
            "horizon": ["3M", "3M"],
            "pred_score": [0.7, 0.2],
            "pred_prob": [0.7, 0.2],
        }
    )
    upsert_model_predictions(conn, predictions, "patchtst_test")
    stored = load_model_predictions(conn, "patchtst_test", "3M")

    assert stored.iloc[0]["symbol"] == "000001"
    assert stored.iloc[0]["rank"] == 1

    run_id = record_backtest_run(
        conn,
        model_name="patchtst_test",
        baseline_model_name="lightgbm",
        horizon="3M",
        metrics={"avg_excess_return": 0.01, "hit_ratio": 0.5, "mdd": -0.1, "avg_turnover": 0.3},
        accepted=False,
        fail_reason="failed",
        top_k=20,
    )
    assert run_id
    assert conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 1
