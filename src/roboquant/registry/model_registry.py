from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pandas as pd

from roboquant.db import append_dedup_table


def register_feature_set(
    conn,
    feature_set_name: str,
    feature_list: list[str],
    status: str = "experimental",
    description: str | None = None,
) -> None:
    now = _utcnow()
    row = pd.DataFrame(
        [
            {
                "feature_set_name": feature_set_name,
                "feature_list_json": json.dumps(feature_list, ensure_ascii=False),
                "status": status,
                "description": description,
                "created_at": now,
                "updated_at": now,
            }
        ]
    )
    append_dedup_table(conn, "feature_set_registry", row, ["feature_set_name"])


def register_model(
    conn,
    model_name: str,
    model_type: str,
    feature_set_name: str,
    label_name: str,
    horizons: list[str],
    artifact_path: str | None = None,
    metrics: dict | None = None,
    split: dict | None = None,
    status: str = "experimental",
    production_weight: float = 0.0,
    shadow_mode: bool = True,
    fail_reason: str | None = None,
) -> None:
    split = split or {}
    now = _utcnow()
    row = pd.DataFrame(
        [
            {
                "model_name": model_name,
                "model_type": model_type,
                "feature_set_name": feature_set_name,
                "label_name": label_name,
                "horizons": ",".join(horizons),
                "train_start": _date_or_none(split.get("train_start")),
                "train_end": _date_or_none(split.get("train_end")),
                "valid_start": _date_or_none(split.get("valid_start")),
                "valid_end": _date_or_none(split.get("valid_end")),
                "test_start": _date_or_none(split.get("test_start")),
                "test_end": _date_or_none(split.get("test_end")),
                "status": status,
                "production_weight": float(production_weight),
                "shadow_mode": bool(shadow_mode),
                "artifact_path": artifact_path,
                "metrics_json": json.dumps(metrics or {}, ensure_ascii=False),
                "fail_reason": fail_reason,
                "created_at": now,
                "updated_at": now,
            }
        ]
    )
    append_dedup_table(conn, "model_registry", row, ["model_name"])


def update_model_status(
    conn,
    model_name: str,
    status: str,
    production_weight: float,
    fail_reason: str | None = None,
    metrics: dict | None = None,
) -> None:
    conn.execute(
        """
        UPDATE model_registry
        SET status = ?,
            production_weight = ?,
            shadow_mode = ?,
            fail_reason = ?,
            metrics_json = CASE WHEN ? IS NULL THEN metrics_json ELSE ? END,
            updated_at = ?
        WHERE model_name = ?
        """,
        [
            status,
            float(production_weight),
            status != "accepted",
            fail_reason,
            None if metrics is None else "set",
            None if metrics is None else json.dumps(metrics, ensure_ascii=False),
            _utcnow(),
            model_name,
        ],
    )


def record_backtest_run(
    conn,
    model_name: str,
    baseline_model_name: str,
    horizon: str,
    metrics: dict,
    accepted: bool,
    fail_reason: str,
    top_k: int,
    start_date=None,
    end_date=None,
) -> str:
    run_id = uuid4().hex
    row = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "model_name": model_name,
                "baseline_model_name": baseline_model_name,
                "horizon": horizon,
                "start_date": _date_or_none(start_date),
                "end_date": _date_or_none(end_date),
                "top_k": int(top_k),
                "top20_return": _float_or_none(metrics.get("top20_return")),
                "excess_return": _float_or_none(metrics.get("avg_excess_return")),
                "hit_ratio": _float_or_none(metrics.get("hit_ratio")),
                "mdd": _float_or_none(metrics.get("mdd")),
                "turnover": _float_or_none(metrics.get("avg_turnover")),
                "sharpe": _float_or_none(metrics.get("sharpe")),
                "transaction_cost_adjusted_return": _float_or_none(
                    metrics.get("transaction_cost_adjusted_return")
                ),
                "accepted": bool(accepted),
                "fail_reason": fail_reason,
                "metrics_json": json.dumps(metrics, ensure_ascii=False),
                "created_at": _utcnow(),
            }
        ]
    )
    append_dedup_table(conn, "backtest_runs", row, ["run_id"])
    return run_id


def upsert_model_predictions(conn, predictions: pd.DataFrame, model_name: str) -> pd.DataFrame:
    if predictions.empty:
        return predictions
    frame = predictions.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    frame["model_name"] = model_name
    frame["pred_score"] = pd.to_numeric(frame["pred_score"], errors="coerce")
    frame["pred_prob"] = pd.to_numeric(frame["pred_prob"], errors="coerce")
    frame["rank"] = frame.groupby(["date", "horizon", "model_name"])["pred_prob"].rank(
        ascending=False,
        method="first",
    )
    frame["rank"] = frame["rank"].astype("Int64")
    frame["created_at"] = _utcnow()
    columns = ["date", "symbol", "model_name", "horizon", "pred_score", "pred_prob", "rank", "created_at"]
    frame = frame[columns].dropna(subset=["date", "symbol", "horizon", "pred_prob"])
    append_dedup_table(conn, "model_predictions", frame, ["date", "symbol", "model_name", "horizon"])
    return frame


def load_model_registry(conn) -> pd.DataFrame:
    return conn.execute("SELECT * FROM model_registry ORDER BY updated_at DESC").fetchdf()


def load_backtest_runs(conn) -> pd.DataFrame:
    return conn.execute("SELECT * FROM backtest_runs ORDER BY created_at DESC").fetchdf()


def load_model_predictions(conn, model_name: str | None = None, horizon: str | None = None) -> pd.DataFrame:
    query = "SELECT * FROM model_predictions"
    params: list[object] = []
    where = []
    if model_name:
        where.append("model_name = ?")
        params.append(model_name)
    if horizon:
        where.append("horizon = ?")
        params.append(horizon)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY date, rank"
    return conn.execute(query, params).fetchdf()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _date_or_none(value):
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date()


def _float_or_none(value):
    if value is None or pd.isna(value):
        return None
    return float(value)
