"""Nasdaq/SOX incremental market-outlook shadow and close-loop feedback.

The production ``market-outlook-v2`` Ridge model remains untouched.  This
module reconstructs its direct-index baseline chronologically, fits a
Nasdaq/SOX-only candidate per market, and stores an experimental blended
forecast.  The blend weight is calculated from mature prior out-of-sample
observations only, so a later Korea close cannot influence an earlier signal.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import HuberRegressor, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from roboquant.db import align_to_table_columns, append_dedup_table, table_exists
from roboquant.market_outlook import (
    _fit_market_horizon_spec,
    _predict_subset,
    build_market_outlook_dataset,
    market_outlook_input_status,
)
from roboquant.registry.model_registry import register_feature_set, register_model

MODEL_NAME = "market-us-linkage-shadow-v1"
MODEL_VERSION = f"{MODEL_NAME}-v1"
FEATURE_VERSION = "market_us_linkage_v1"
FEATURE_SET_NAME = "market_us_linkage_shadow_v1"
MARKETS = ("KOSPI", "KOSDAQ")
ALPHA = 0.20

FEATURE_COLUMNS = [
    "us_nasdaq_return_1d",
    "us_nasdaq_return_5d",
    "us_sox_return_1d",
    "us_sox_return_5d",
    "sox_nasdaq_spread_1d",
    "sox_nasdaq_spread_5d",
    "us_nasdaq_volatility_20d",
    "us_sox_volatility_20d",
    "nasdaq_shock_flag",
    "sox_shock_flag",
]
MODEL_FEATURE_COLUMNS = [
    "us_nasdaq_return_1d",
    "us_nasdaq_return_5d",
    "sox_nasdaq_spread_1d",
    "sox_nasdaq_spread_5d",
    "us_nasdaq_volatility_20d",
    "us_sox_volatility_20d",
    "nasdaq_shock_flag",
    "sox_shock_flag",
]
FEATURE_STORE_COLUMNS = [
    "prediction_date",
    "asof_date",
    "market",
    "prediction_cutoff",
    "global_trade_date",
    "global_available_at",
    *FEATURE_COLUMNS,
    "label_return",
    "data_quality_json",
    "feature_version",
    "created_at",
]
FORECAST_COLUMNS = [
    "prediction_date",
    "asof_date",
    "market",
    "prediction_cutoff",
    "global_trade_date",
    "model_name",
    "model_version",
    "baseline_expected_return",
    "global_expected_return",
    "global_delta",
    "dynamic_overlay_weight",
    "candidate_expected_return",
    "baseline_range_low",
    "baseline_range_high",
    "control_range_low",
    "control_range_high",
    "candidate_range_low",
    "candidate_range_high",
    "actual_return",
    "calibration_count",
    "feedback_count",
    "relative_mae_improvement",
    "attribution_json",
    "status",
    "production_weight",
    "is_backtest",
    "metrics_json",
    "data_quality_json",
    "created_at",
]
FEEDBACK_COLUMNS = [
    "prediction_date",
    "market",
    "model_name",
    "model_version",
    "is_backtest",
    "actual_return",
    "baseline_abs_error",
    "candidate_abs_error",
    "global_abs_error",
    "baseline_direction_hit",
    "candidate_direction_hit",
    "baseline_covered",
    "candidate_covered",
    "baseline_low_breach",
    "candidate_low_breach",
    "baseline_interval_score",
    "candidate_interval_score",
    "dynamic_overlay_weight",
    "relative_mae_improvement",
    "status",
    "created_at",
]


def market_us_linkage_settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "enabled": True,
        "model_name": MODEL_NAME,
        "feature_version": FEATURE_VERSION,
        "initial_train_days": 504,
        "purge_days": 1,
        "retrain_step_days": 21,
        "calibration_window": 252,
        "min_calibration_rows": 60,
        "feedback_window": 60,
        "min_feedback_rows": 60,
        "max_overlay_weight": 0.20,
        "skill_improvement_for_max_weight": 0.10,
        "min_effective_weight": 0.005,
        "min_oos_rows": 252,
        "min_live_observations": 60,
        "target_coverage": 0.80,
        "coverage_lower_bound": 0.75,
        "coverage_upper_bound": 0.85,
        "max_average_width_ratio": 1.10,
        "max_low_breach_worsening": 0.02,
        "nasdaq_shock_threshold": -0.03,
        "sox_shock_threshold": -0.04,
        "huber_epsilon": 1.35,
        "huber_alpha": 0.0005,
        "huber_max_iter": 300,
    }
    values = _deep_merge(defaults, dict((config or {}).get("market_us_linkage_shadow") or {}))
    for key, minimum in {
        "initial_train_days": 30,
        "purge_days": 1,
        "retrain_step_days": 1,
        "calibration_window": 20,
        "min_calibration_rows": 10,
        "feedback_window": 10,
        "min_feedback_rows": 10,
        "min_oos_rows": 30,
        "min_live_observations": 1,
    }.items():
        values[key] = max(minimum, int(values[key]))
    for key in (
        "max_overlay_weight",
        "skill_improvement_for_max_weight",
        "min_effective_weight",
        "target_coverage",
        "coverage_lower_bound",
        "coverage_upper_bound",
        "max_average_width_ratio",
        "max_low_breach_worsening",
        "nasdaq_shock_threshold",
        "sox_shock_threshold",
        "huber_epsilon",
        "huber_alpha",
    ):
        values[key] = float(values[key])
    values["max_overlay_weight"] = min(0.20, max(0.0, values["max_overlay_weight"]))
    values["model_name"] = str(values["model_name"])
    values["feature_version"] = str(values["feature_version"])
    return values


def build_market_us_linkage_features(
    conn,
    config: Mapping[str, Any] | None = None,
    *,
    asof_date: str | date = "latest",
) -> pd.DataFrame:
    """Materialize cutoff-safe U.S. market inputs for next KRX sessions."""
    settings = market_us_linkage_settings(config)
    premarket = _load_final_premarket_rows(conn, asof_date=asof_date)
    if premarket.empty:
        return _empty_feature_frame()
    globals_frame = _load_global_features(conn)
    # DuckDB can return DATE keys as either Python dates or pandas datetime
    # values. Normalize both sides before joining so the next KRX session is
    # always matched to the already available U.S. trading session.
    premarket["global_trade_date"] = pd.to_datetime(
        premarket["global_trade_date"], errors="coerce"
    ).dt.date
    globals_frame["global_trade_date"] = pd.to_datetime(
        globals_frame["global_trade_date"], errors="coerce"
    ).dt.date
    output = premarket.merge(globals_frame, on="global_trade_date", how="left", suffixes=("", "_global"))
    output["prediction_date"] = pd.to_datetime(output["prediction_date"], errors="coerce").dt.date
    output["asof_date"] = pd.to_datetime(output["asof_date"], errors="coerce").dt.date
    output["global_trade_date"] = pd.to_datetime(output["global_trade_date"], errors="coerce").dt.date
    for column in FEATURE_COLUMNS:
        if column not in output.columns:
            output[column] = np.nan
        output[column] = pd.to_numeric(output[column], errors="coerce")
    output["us_nasdaq_return_1d"] = output["us_nasdaq_return_1d"].combine_first(
        _column_or_nan(output, "nasdaq_return_1d")
    )
    output["us_sox_return_1d"] = output["us_sox_return_1d"].combine_first(
        _column_or_nan(output, "sox_return_1d")
    )
    output["us_nasdaq_return_5d"] = _column_or_nan(output, "nasdaq_return_5d")
    output["us_sox_return_5d"] = _column_or_nan(output, "sox_return_5d")
    output["us_nasdaq_volatility_20d"] = _column_or_nan(output, "nasdaq_volatility_20d")
    output["us_sox_volatility_20d"] = _column_or_nan(output, "sox_volatility_20d")
    output["sox_nasdaq_spread_1d"] = output["us_sox_return_1d"] - output["us_nasdaq_return_1d"]
    output["sox_nasdaq_spread_5d"] = output["us_sox_return_5d"] - output["us_nasdaq_return_5d"]
    output["nasdaq_shock_flag"] = (
        output["us_nasdaq_return_1d"].le(float(settings["nasdaq_shock_threshold"]))
    ).astype(float)
    output["sox_shock_flag"] = output["us_sox_return_1d"].le(float(settings["sox_shock_threshold"])).astype(float)
    output.loc[output["us_nasdaq_return_1d"].isna(), "nasdaq_shock_flag"] = np.nan
    output.loc[output["us_sox_return_1d"].isna(), "sox_shock_flag"] = np.nan
    output["label_return"] = pd.to_numeric(output.get("label_return"), errors="coerce")
    output["global_available_at"] = pd.to_datetime(output.get("available_at"), errors="coerce")
    output["prediction_cutoff"] = pd.to_datetime(output.get("prediction_cutoff"), errors="coerce")
    readiness = output.apply(_feature_quality, axis=1)
    output["data_quality_json"] = readiness.map(lambda value: _json(value))
    output["feature_version"] = str(settings["feature_version"])
    output["created_at"] = _utcnow()
    for column in FEATURE_STORE_COLUMNS:
        if column not in output.columns:
            output[column] = None
    return output[FEATURE_STORE_COLUMNS].sort_values(["prediction_date", "market"]).reset_index(drop=True)


def refresh_market_us_linkage_features(conn, frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    append_dedup_table(
        conn,
        "market_us_linkage_features_daily",
        frame,
        ["prediction_date", "market", "feature_version"],
    )
    return frame


def run_market_us_linkage_shadow(
    conn,
    config: Mapping[str, Any] | None = None,
    *,
    asof_date: str | date = "latest",
    include_live: bool = True,
) -> dict[str, Any]:
    """Backtest the candidate and save only experimental forecast records."""
    settings = market_us_linkage_settings(config)
    features = refresh_market_us_linkage_features(
        conn,
        build_market_us_linkage_features(conn, config, asof_date=asof_date),
    )
    if features.empty:
        return _empty_run("premarket global feature history is unavailable")
    data = _join_baseline_dataset(conn, features, config)
    if data.empty:
        return _empty_run("market outlook baseline history is unavailable")

    all_forecasts: list[dict[str, Any]] = []
    all_feedback: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {}
    for market in MARKETS:
        market_data = data[data["market"].eq(market)].sort_values("prediction_date").reset_index(drop=True)
        backtest, artifact = _walk_forward_market(market_data, settings)
        feedback = [_feedback_row(item) for item in backtest]
        market_live_count = _live_feedback_count(conn, market=market)
        backtest_gate, fail_reason, metrics = _evaluate_gate(backtest, settings, live_count=market_live_count)
        live_rows: list[dict[str, Any]] = []
        live_message = None
        if include_live:
            live_rows, live_message, live_artifact = _build_live_forecast(
                conn,
                market_data,
                backtest,
                settings,
                config,
            )
            if live_artifact is not None:
                artifact["live"] = live_artifact
        all_forecasts.extend(backtest)
        all_forecasts.extend(live_rows)
        all_feedback.extend(feedback)
        artifacts[market] = artifact
        run_rows.append(
            {
                "run_id": uuid4().hex,
                "model_name": settings["model_name"],
                "model_version": MODEL_VERSION,
                "market": market,
                "start_date": _date_or_none(market_data["prediction_date"].min() if not market_data.empty else None),
                "end_date": _date_or_none(market_data["prediction_date"].max() if not market_data.empty else None),
                "oos_count": int(len(backtest)),
                "live_observation_count": int(market_live_count),
                "gate_status": backtest_gate,
                "fail_reason": fail_reason or live_message,
                "metrics_json": _json(metrics),
                "created_at": _utcnow(),
            }
        )

    forecast_frame = pd.DataFrame(all_forecasts, columns=FORECAST_COLUMNS)
    if not forecast_frame.empty:
        _insert_immutable_rows(
            conn,
            "market_us_linkage_shadow_forecasts",
            forecast_frame,
            ["prediction_date", "market", "model_version", "is_backtest"],
        )
    feedback_frame = pd.DataFrame(all_feedback, columns=FEEDBACK_COLUMNS)
    if not feedback_frame.empty:
        _insert_immutable_rows(
            conn,
            "market_us_linkage_feedback_daily",
            feedback_frame,
            ["prediction_date", "market", "model_version", "is_backtest"],
        )
    run_frame = pd.DataFrame(run_rows)
    append_dedup_table(conn, "market_us_linkage_backtest_runs", run_frame, ["run_id"])

    _write_artifact(config, settings, artifacts)
    overall_status = _overall_status(run_rows)
    register_feature_set(
        conn,
        FEATURE_SET_NAME,
        MODEL_FEATURE_COLUMNS,
        status="experimental",
        description="Cutoff-safe Nasdaq/SOX incremental global overlay; news and LLM excluded.",
    )
    register_model(
        conn,
        model_name=settings["model_name"],
        model_type="nasdaq_sox_market_overlay_shadow",
        feature_set_name=FEATURE_SET_NAME,
        label_name="next_trading_day_index_return",
        horizons=["TODAY"],
        artifact_path=str(_artifact_path(config)),
        metrics={row["market"]: _loads(row["metrics_json"], {}) for row in run_rows},
        split={"mode": "expanding_walk_forward", "purge_days": settings["purge_days"]},
        status=overall_status,
        production_weight=0.0,
        shadow_mode=True,
        fail_reason="; ".join(str(row["fail_reason"]) for row in run_rows if row.get("fail_reason")) or None,
    )
    return {
        "status": overall_status,
        "model_name": settings["model_name"],
        "model_version": MODEL_VERSION,
        "feature_rows": int(len(features)),
        "markets": [_record(row) for row in run_rows],
        "production_weight": 0.0,
    }


def update_market_us_linkage_feedback(conn, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Close live rows only after the predicted KRX session has a close."""
    if not table_exists(conn, "market_us_linkage_shadow_forecasts"):
        return {"status": "not_ready", "updated": 0, "message": "shadow forecasts are unavailable"}
    returns = _market_returns(conn)
    if returns.empty:
        return {"status": "not_ready", "updated": 0, "message": "benchmark closes are unavailable"}
    pending = conn.execute(
        """
        SELECT *
        FROM market_us_linkage_shadow_forecasts
        WHERE is_backtest = FALSE AND actual_return IS NULL
        ORDER BY prediction_date, market
        """
    ).fetchdf()
    if pending.empty:
        return {"status": "ready", "updated": 0, "message": "no mature live forecasts"}
    target_returns = {
        (pd.to_datetime(row.date).date(), str(row.market)): _number(row.return_1d)
        for row in returns.itertuples(index=False)
    }
    feedback_rows: list[dict[str, Any]] = []
    updated = 0
    for row in pending.to_dict(orient="records"):
        key = (_date_or_none(row.get("prediction_date")), str(row.get("market") or ""))
        actual = target_returns.get(key)
        if actual is None:
            continue
        item = dict(row)
        item["actual_return"] = actual
        feedback_rows.append(_feedback_row(item))
        conn.execute(
            """
            UPDATE market_us_linkage_shadow_forecasts
            SET actual_return = ?
            WHERE prediction_date = ? AND market = ? AND model_version = ? AND is_backtest = FALSE
            """,
            [actual, key[0], key[1], str(row.get("model_version"))],
        )
        updated += 1
    if feedback_rows:
        _insert_immutable_rows(
            conn,
            "market_us_linkage_feedback_daily",
            pd.DataFrame(feedback_rows, columns=FEEDBACK_COLUMNS),
            ["prediction_date", "market", "model_version", "is_backtest"],
        )
    return {
        "status": "ready" if updated else "pending",
        "updated": updated,
        "message": "mature live KRX closes recorded" if updated else "target sessions have not closed yet",
    }


def get_market_us_linkage_shadow(conn, *, market: str = "all", limit: int = 20) -> dict[str, Any]:
    if not table_exists(conn, "market_us_linkage_backtest_runs"):
        return _empty_payload("Nasdaq/SOX linkage schema is unavailable")
    filter_sql, params = _market_filter(market)
    runs = conn.execute(
        f"""
        SELECT *
        FROM market_us_linkage_backtest_runs
        {filter_sql}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY market ORDER BY created_at DESC) = 1
        ORDER BY CASE market WHEN 'KOSPI' THEN 1 WHEN 'KOSDAQ' THEN 2 ELSE 3 END
        """,
        params,
    ).fetchdf()
    forecasts = pd.DataFrame()
    if table_exists(conn, "market_us_linkage_shadow_forecasts"):
        forecasts = conn.execute(
            f"""
            SELECT *
            FROM market_us_linkage_shadow_forecasts
            WHERE is_backtest = FALSE {"AND market = ?" if market != "all" else ""}
            QUALIFY ROW_NUMBER() OVER (PARTITION BY market ORDER BY prediction_date DESC, created_at DESC) = 1
            ORDER BY CASE market WHEN 'KOSPI' THEN 1 WHEN 'KOSDAQ' THEN 2 ELSE 3 END
            """,
            params,
        ).fetchdf()
    feedback = _feedback_summary(conn, market=market)
    correlations = _correlation_summary(conn, market=market)
    run_items = []
    for row in runs.to_dict(orient="records"):
        item = _record(row)
        item["metrics"] = _loads(item.pop("metrics_json", None), {})
        run_items.append(item)
    items = [_forecast_record(row) for row in forecasts.to_dict(orient="records")]
    statuses = [str(item.get("gate_status") or "not_ready") for item in run_items]
    status = "ready" if items else _overall_status([{"gate_status": value} for value in statuses])
    return {
        "status": status,
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "production_weight": 0.0,
        "items": items,
        "runs": run_items,
        "backtest_count": int(sum(int(item.get("oos_count") or 0) for item in run_items)),
        "feedback": feedback,
        "correlations": correlations,
        "message": "Nasdaq/SOX 후보는 close-loop 검증용 shadow이며 production 시장전망을 변경하지 않습니다.",
        "disclaimer": "연구·정보제공용 글로벌 연동 검증입니다. 매수·매도 지시나 수익 보장이 아닙니다.",
    }


def get_market_us_linkage_history(conn, *, market: str = "all", limit: int = 100) -> dict[str, Any]:
    if not table_exists(conn, "market_us_linkage_shadow_forecasts"):
        return {"status": "not_ready", "items": []}
    _, params = _market_filter(market)
    where = ["is_backtest = TRUE"]
    if market != "all":
        where.append("market = ?")
    frame = conn.execute(
        f"""
        SELECT *
        FROM market_us_linkage_shadow_forecasts
        WHERE {' AND '.join(where)}
        ORDER BY prediction_date DESC, market
        LIMIT ?
        """,
        [*params, int(max(1, min(limit, 500)))] if market != "all" else [int(max(1, min(limit, 500)))],
    ).fetchdf()
    return {"status": "ready" if not frame.empty else "not_ready", "items": [_forecast_record(row) for row in frame.to_dict(orient="records")]}


def get_market_us_linkage_feedback(conn, *, market: str = "all", limit: int = 100) -> dict[str, Any]:
    if not table_exists(conn, "market_us_linkage_feedback_daily"):
        return {"status": "not_ready", "items": [], "summary": {}}
    _, params = _market_filter(market)
    where = []
    if market != "all":
        where.append("market = ?")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    frame = conn.execute(
        f"""
        SELECT * FROM market_us_linkage_feedback_daily
        {clause}
        ORDER BY prediction_date DESC, market, is_backtest
        LIMIT ?
        """,
        [*params, int(max(1, min(limit, 500)))] if params else [int(max(1, min(limit, 500)))],
    ).fetchdf()
    return {"status": "ready" if not frame.empty else "not_ready", "items": [_record(row) for row in frame.to_dict(orient="records")], "summary": _feedback_summary(conn, market=market)}


def _load_final_premarket_rows(conn, *, asof_date: str | date) -> pd.DataFrame:
    if not table_exists(conn, "premarket_features_daily"):
        return pd.DataFrame()
    params: list[Any] = []
    date_filter = ""
    if str(asof_date).lower() != "latest":
        parsed = pd.to_datetime(asof_date, errors="coerce")
        if pd.isna(parsed):
            raise ValueError(f"Invalid asof_date: {asof_date}")
        date_filter = "AND asof_date <= ?"
        params.append(parsed.date())
    frame = conn.execute(
        f"""
        SELECT
          prediction_date, asof_date, market, prediction_cutoff, available_at,
          global_trade_date, is_final_close, us_nasdaq_return_1d,
          us_sox_return_1d, label_return
        FROM premarket_features_daily
        WHERE forecast_stage = 'final_premarket'
          AND COALESCE(is_final_close, FALSE) = TRUE
          {date_filter}
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY prediction_date, market
          ORDER BY created_at DESC
        ) = 1
        ORDER BY prediction_date, market
        """,
        params,
    ).fetchdf()
    return frame


def _load_global_features(conn) -> pd.DataFrame:
    if not table_exists(conn, "global_market_daily"):
        return pd.DataFrame(columns=["global_trade_date"])
    raw = conn.execute(
        """
        SELECT trade_date, symbol, return_1d, return_5d, volatility_20d, source_timestamp, ingested_at
        FROM global_market_daily
        WHERE symbol IN ('^IXIC', '^SOX')
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY trade_date, symbol
          ORDER BY source_timestamp DESC NULLS LAST, ingested_at DESC NULLS LAST
        ) = 1
        ORDER BY trade_date, symbol
        """
    ).fetchdf()
    if raw.empty:
        return pd.DataFrame(columns=["global_trade_date"])
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], errors="coerce").dt.date
    records: list[dict[str, Any]] = []
    for trade_date, group in raw.groupby("trade_date", sort=True):
        values: dict[str, Any] = {"global_trade_date": trade_date}
        for item in group.itertuples(index=False):
            prefix = "nasdaq" if str(item.symbol) == "^IXIC" else "sox"
            values[f"{prefix}_return_1d"] = _number(item.return_1d)
            values[f"{prefix}_return_5d"] = _number(item.return_5d)
            values[f"{prefix}_volatility_20d"] = _number(item.volatility_20d)
        records.append(values)
    frame = pd.DataFrame(records)
    for column in (
        "nasdaq_return_1d",
        "nasdaq_return_5d",
        "nasdaq_volatility_20d",
        "sox_return_1d",
        "sox_return_5d",
        "sox_volatility_20d",
    ):
        if column not in frame.columns:
            frame[column] = np.nan
    return frame


def _feature_quality(row: pd.Series) -> dict[str, Any]:
    missing = [column for column in FEATURE_COLUMNS if pd.isna(row.get(column))]
    available_at = pd.to_datetime(row.get("global_available_at"), errors="coerce")
    cutoff = pd.to_datetime(row.get("prediction_cutoff"), errors="coerce")
    cutoff_ok = not pd.isna(available_at) and not pd.isna(cutoff) and available_at <= cutoff
    final = bool(row.get("is_final_close"))
    ready = final and cutoff_ok and not missing
    return {
        "status": "ready" if ready else "partial_ready",
        "is_final_close": final,
        "global_trade_date": _date_string(row.get("global_trade_date")),
        "global_available_at": _timestamp_string(row.get("global_available_at")),
        "prediction_cutoff": _timestamp_string(row.get("prediction_cutoff")),
        "cutoff_safe": cutoff_ok,
        "missing_features": missing,
        "news_llm_policy": "excluded",
    }


def _join_baseline_dataset(conn, features: pd.DataFrame, config: Mapping[str, Any] | None) -> pd.DataFrame:
    baseline = build_market_outlook_dataset(conn, config=dict(config or {}), asof_date="latest")
    if baseline.empty:
        return pd.DataFrame()
    baseline = baseline[baseline["horizon"].astype(str).str.upper().eq("TODAY")].copy()
    baseline["asof_date"] = pd.to_datetime(baseline["asof_date"], errors="coerce").dt.date
    baseline["target_date"] = pd.to_datetime(baseline["target_date"], errors="coerce").dt.date
    features = features.copy()
    features["prediction_date"] = pd.to_datetime(features["prediction_date"], errors="coerce").dt.date
    features["asof_date"] = pd.to_datetime(features["asof_date"], errors="coerce").dt.date
    joined = baseline.merge(
        features,
        left_on=["asof_date", "target_date", "market"],
        right_on=["asof_date", "prediction_date", "market"],
        how="inner",
        suffixes=("", "_linkage"),
    )
    if joined.empty:
        return joined
    joined["prediction_date"] = pd.to_datetime(joined["prediction_date"], errors="coerce").dt.date
    joined["label_return"] = pd.to_numeric(joined["label_return"], errors="coerce")
    return joined.sort_values(["prediction_date", "market"]).drop_duplicates(["prediction_date", "market"], keep="last")


def _walk_forward_market(frame: pd.DataFrame, settings: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = frame.copy()
    data["prediction_date"] = pd.to_datetime(data["prediction_date"], errors="coerce").dt.date
    data["asof_date"] = pd.to_datetime(data["asof_date"], errors="coerce").dt.date
    data = data.dropna(subset=["prediction_date", "asof_date", "label_return"]).sort_values("prediction_date").reset_index(drop=True)
    initial = int(settings["initial_train_days"])
    purge = int(settings["purge_days"])
    if len(data) < initial + purge + 1:
        return [], {"status": "not_ready", "rows": int(len(data)), "required_rows": initial + purge + 1}
    rows: list[dict[str, Any]] = []
    baseline_spec: dict[str, Any] | None = None
    global_model: Pipeline | None = None
    model_metadata: dict[str, Any] = {}
    first_index = initial + purge
    for index in range(first_index, len(data)):
        train_end = index - purge
        if baseline_spec is None or (index - first_index) % int(settings["retrain_step_days"]) == 0:
            train = data.iloc[:train_end].copy()
            baseline_spec = _fit_market_horizon_spec(train)
            global_model, model_metadata = _fit_global_model(train, settings)
        if baseline_spec is None or global_model is None:
            continue
        item = data.iloc[index]
        baseline_expected = float(_predict_subset(item.to_frame().T, baseline_spec).iloc[0])
        global_expected, attribution = _predict_global_model(global_model, item)
        prior = rows[-int(settings["feedback_window"]) :]
        weight_info = _dynamic_overlay_weight(prior, settings)
        overlay_weight = float(weight_info["weight"])
        delta = global_expected - baseline_expected
        candidate_expected = baseline_expected + overlay_weight * delta
        baseline_sigma = _safe_sigma(baseline_spec.get("residual_std"))
        baseline_low = baseline_expected - baseline_sigma
        baseline_high = baseline_expected + baseline_sigma
        control_low, control_high, control_count = _residual_interval(
            prior,
            center=baseline_expected,
            expected_key="baseline_expected_return",
            fallback=(baseline_low, baseline_high),
            settings=settings,
        )
        candidate_low, candidate_high, calibration_count = _residual_interval(
            prior,
            center=candidate_expected,
            expected_key="candidate_expected_return",
            fallback=(control_low, control_high),
            settings=settings,
        )
        actual = _number(item.get("label_return"))
        rows.append(
            _forecast_row(
                item,
                baseline_expected=baseline_expected,
                global_expected=global_expected,
                overlay_weight=overlay_weight,
                candidate_expected=candidate_expected,
                baseline_range=(baseline_low, baseline_high),
                control_range=(control_low, control_high),
                candidate_range=(candidate_low, candidate_high),
                actual_return=actual,
                calibration_count=calibration_count,
                feedback_count=int(weight_info["count"]),
                relative_improvement=weight_info["relative_improvement"],
                attribution={**attribution, "baseline_residual_std": baseline_sigma, "control_calibration_count": control_count},
                status="backtest",
                is_backtest=True,
                data_quality=_loads(item.get("data_quality_json"), {}),
                metrics={"model": model_metadata, "weight_reason": weight_info["reason"]},
            )
        )
    return rows, {"status": "ready", "rows": len(rows), "metadata": model_metadata}


def _build_live_forecast(
    conn,
    data: pd.DataFrame,
    backtest: list[dict[str, Any]],
    settings: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    if data.empty:
        return [], "no linkage rows", None
    labelled = data[data["label_return"].notna()].copy()
    candidates = data[data["label_return"].isna()].copy()
    if candidates.empty:
        latest_label_date = labelled["prediction_date"].max() if not labelled.empty else None
        candidates = data[data["prediction_date"].gt(latest_label_date)].copy() if latest_label_date else data.tail(1).copy()
    if candidates.empty or len(labelled) < int(settings["initial_train_days"]):
        return [], "latest linkage target is not ready", None
    latest = candidates.sort_values("prediction_date").tail(1).iloc[0]
    context = market_outlook_input_status(
        conn,
        asof_date=_date_or_none(latest.get("asof_date")),
        target_date=_date_or_none(latest.get("prediction_date")),
        config=dict(config or {}),
        calendar_source="premarket_signed_global_v1",
        calendar_messages=[],
    )
    if context.get("forecast_status") != "confirmed":
        return [], "blocked_stale_input", None
    baseline_spec = _fit_market_horizon_spec(labelled)
    global_model, model_metadata = _fit_global_model(labelled, settings)
    baseline_expected = float(_predict_subset(latest.to_frame().T, baseline_spec).iloc[0])
    global_expected, attribution = _predict_global_model(global_model, latest)
    persisted_feedback = _mature_forecast_history(
        conn,
        market=str(latest.get("market") or ""),
        before_prediction_date=_date_or_none(latest.get("prediction_date")),
        limit=int(settings["feedback_window"]),
    )
    weight_history = persisted_feedback or backtest[-int(settings["feedback_window"]) :]
    weight_info = _dynamic_overlay_weight(weight_history, settings)
    overlay_weight = float(weight_info["weight"])
    candidate_expected = baseline_expected + overlay_weight * (global_expected - baseline_expected)
    baseline_sigma = _safe_sigma(baseline_spec.get("residual_std"))
    baseline_range = (baseline_expected - baseline_sigma, baseline_expected + baseline_sigma)
    control_range = _residual_interval(
        backtest[-int(settings["calibration_window"]) :],
        center=baseline_expected,
        expected_key="baseline_expected_return",
        fallback=baseline_range,
        settings=settings,
    )
    candidate_range = _residual_interval(
        backtest[-int(settings["calibration_window"]) :],
        center=candidate_expected,
        expected_key="candidate_expected_return",
        fallback=control_range[:2],
        settings=settings,
    )
    row = _forecast_row(
        latest,
        baseline_expected=baseline_expected,
        global_expected=global_expected,
        overlay_weight=overlay_weight,
        candidate_expected=candidate_expected,
        baseline_range=baseline_range,
        control_range=control_range[:2],
        candidate_range=candidate_range[:2],
        actual_return=None,
        calibration_count=int(candidate_range[2]),
        feedback_count=int(weight_info["count"]),
        relative_improvement=weight_info["relative_improvement"],
        attribution={**attribution, "baseline_residual_std": baseline_sigma},
        status="experimental",
        is_backtest=False,
        data_quality={**_loads(latest.get("data_quality_json"), {}), "forecast_context": _json_safe(context)},
        metrics={"model": model_metadata, "weight_reason": weight_info["reason"]},
    )
    return [row], None, {"baseline_spec": baseline_spec, "global_model": global_model, "metadata": model_metadata}


def _fit_global_model(train: pd.DataFrame, settings: Mapping[str, Any]) -> tuple[Pipeline, dict[str, Any]]:
    x = train.reindex(columns=MODEL_FEATURE_COLUMNS).apply(pd.to_numeric, errors="coerce")
    y = pd.to_numeric(train["label_return"], errors="coerce")
    valid = y.notna()
    x = x.loc[valid]
    y = y.loc[valid]
    if len(x) < 10:
        raise ValueError("insufficient global linkage training rows")
    steps = [
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        (
            "regressor",
            HuberRegressor(
                epsilon=float(settings["huber_epsilon"]),
                alpha=float(settings["huber_alpha"]),
                max_iter=int(settings["huber_max_iter"]),
            ),
        ),
    ]
    model: Pipeline = Pipeline(steps)
    try:
        model.fit(x, y)
        model_type = "huber"
    except (ValueError, FloatingPointError):
        model = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("regressor", Ridge(alpha=1.0)),
            ]
        )
        model.fit(x, y)
        model_type = "ridge_fallback"
    return model, {"model_type": model_type, "train_rows": int(len(x)), "feature_columns": MODEL_FEATURE_COLUMNS}


def _predict_global_model(model: Pipeline, row: pd.Series) -> tuple[float, dict[str, Any]]:
    x = pd.DataFrame([{column: _number(row.get(column)) for column in MODEL_FEATURE_COLUMNS}])
    prediction = float(model.predict(x)[0])
    imputed = model.named_steps["imputer"].transform(x)
    standardized = model.named_steps["scaler"].transform(imputed)[0]
    regressor = model.named_steps["regressor"]
    coefficients = np.asarray(getattr(regressor, "coef_", np.zeros(len(MODEL_FEATURE_COLUMNS))), dtype=float)
    contributions = {
        name: float(value * coefficient)
        for name, value, coefficient in zip(MODEL_FEATURE_COLUMNS, standardized, coefficients, strict=False)
    }
    return prediction, {
        "global_feature_contributions": contributions,
        "global_intercept": _number(getattr(regressor, "intercept_", None)),
        "raw_features": {column: _number(row.get(column)) for column in MODEL_FEATURE_COLUMNS},
    }


def _dynamic_overlay_weight(history: list[dict[str, Any]], settings: Mapping[str, Any]) -> dict[str, Any]:
    valid = [
        item
        for item in history
        if _number(item.get("actual_return")) is not None
        and _number(item.get("baseline_expected_return")) is not None
        and _number(item.get("global_expected_return")) is not None
    ]
    if len(valid) < int(settings["min_feedback_rows"]):
        return {"weight": 0.0, "relative_improvement": None, "count": len(valid), "reason": "insufficient_mature_feedback"}
    baseline_error = np.mean(
        [abs(float(item["actual_return"]) - float(item["baseline_expected_return"])) for item in valid]
    )
    global_error = np.mean(
        [abs(float(item["actual_return"]) - float(item["global_expected_return"])) for item in valid]
    )
    if not math.isfinite(baseline_error) or baseline_error <= 1e-12 or not math.isfinite(global_error):
        return {"weight": 0.0, "relative_improvement": None, "count": len(valid), "reason": "invalid_feedback_error"}
    improvement = (baseline_error - global_error) / baseline_error
    weight = float(settings["max_overlay_weight"]) * min(
        1.0,
        max(0.0, improvement / max(float(settings["skill_improvement_for_max_weight"]), 1e-9)),
    )
    return {
        "weight": weight,
        "relative_improvement": float(improvement),
        "count": len(valid),
        "reason": "positive_global_skill" if weight > 0 else "global_candidate_not_better",
    }


def _residual_interval(
    history: list[dict[str, Any]],
    *,
    center: float,
    expected_key: str,
    fallback: tuple[float, float],
    settings: Mapping[str, Any],
) -> tuple[float, float, int]:
    residuals = [
        float(item["actual_return"]) - float(item[expected_key])
        for item in history
        if _number(item.get("actual_return")) is not None and _number(item.get(expected_key)) is not None
    ]
    if len(residuals) < int(settings["min_calibration_rows"]):
        return float(fallback[0]), float(fallback[1]), len(residuals)
    lower, upper = np.quantile(np.asarray(residuals, dtype=float), [0.10, 0.90])
    return float(center + lower), float(center + upper), len(residuals)


def _forecast_row(
    item: pd.Series,
    *,
    baseline_expected: float,
    global_expected: float,
    overlay_weight: float,
    candidate_expected: float,
    baseline_range: tuple[float, float],
    control_range: tuple[float, float],
    candidate_range: tuple[float, float],
    actual_return: float | None,
    calibration_count: int,
    feedback_count: int,
    relative_improvement: float | None,
    attribution: dict[str, Any],
    status: str,
    is_backtest: bool,
    data_quality: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    row = {
        "prediction_date": _date_or_none(item.get("prediction_date")),
        "asof_date": _date_or_none(item.get("asof_date")),
        "market": str(item.get("market") or ""),
        "prediction_cutoff": _timestamp_or_none(item.get("prediction_cutoff")),
        "global_trade_date": _date_or_none(item.get("global_trade_date")),
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "baseline_expected_return": float(baseline_expected),
        "global_expected_return": float(global_expected),
        "global_delta": float(global_expected - baseline_expected),
        "dynamic_overlay_weight": float(overlay_weight),
        "candidate_expected_return": float(candidate_expected),
        "baseline_range_low": float(baseline_range[0]),
        "baseline_range_high": float(baseline_range[1]),
        "control_range_low": float(control_range[0]),
        "control_range_high": float(control_range[1]),
        "candidate_range_low": float(candidate_range[0]),
        "candidate_range_high": float(candidate_range[1]),
        "actual_return": _number(actual_return),
        "calibration_count": int(calibration_count),
        "feedback_count": int(feedback_count),
        "relative_mae_improvement": _number(relative_improvement),
        "attribution_json": _json(attribution),
        "status": status,
        "production_weight": 0.0,
        "is_backtest": bool(is_backtest),
        "metrics_json": _json(metrics),
        "data_quality_json": _json(data_quality),
        "created_at": _utcnow(),
    }
    return row


def _feedback_row(row: dict[str, Any]) -> dict[str, Any]:
    actual = _number(row.get("actual_return"))
    baseline = _number(row.get("baseline_expected_return"))
    candidate = _number(row.get("candidate_expected_return"))
    global_expected = _number(row.get("global_expected_return"))
    if actual is None or baseline is None or candidate is None:
        raise ValueError("feedback requires actual, baseline, and candidate returns")
    return {
        "prediction_date": _date_or_none(row.get("prediction_date")),
        "market": str(row.get("market") or ""),
        "model_name": str(row.get("model_name") or MODEL_NAME),
        "model_version": str(row.get("model_version") or MODEL_VERSION),
        "is_backtest": bool(row.get("is_backtest")),
        "actual_return": actual,
        "baseline_abs_error": abs(actual - baseline),
        "candidate_abs_error": abs(actual - candidate),
        "global_abs_error": None if global_expected is None else abs(actual - global_expected),
        "baseline_direction_hit": _direction_hit(actual, baseline),
        "candidate_direction_hit": _direction_hit(actual, candidate),
        "baseline_covered": _covered(actual, row.get("baseline_range_low"), row.get("baseline_range_high")),
        "candidate_covered": _covered(actual, row.get("candidate_range_low"), row.get("candidate_range_high")),
        "baseline_low_breach": _low_breach(actual, row.get("baseline_range_low")),
        "candidate_low_breach": _low_breach(actual, row.get("candidate_range_low")),
        "baseline_interval_score": _interval_score(actual, row.get("baseline_range_low"), row.get("baseline_range_high")),
        "candidate_interval_score": _interval_score(actual, row.get("candidate_range_low"), row.get("candidate_range_high")),
        "dynamic_overlay_weight": _number(row.get("dynamic_overlay_weight")) or 0.0,
        "relative_mae_improvement": _number(row.get("relative_mae_improvement")),
        "status": "mature",
        "created_at": _utcnow(),
    }


def _evaluate_gate(
    backtest: list[dict[str, Any]],
    settings: Mapping[str, Any],
    *,
    live_count: int,
) -> tuple[str, str | None, dict[str, Any]]:
    feedback = [_feedback_row(item) for item in backtest if _number(item.get("actual_return")) is not None]
    if len(feedback) < int(settings["min_oos_rows"]):
        return "not_ready", f"requires {settings['min_oos_rows']} OOS rows", {"oos_count": len(feedback)}
    frame = pd.DataFrame(feedback)
    baseline_mae = _mean(frame["baseline_abs_error"])
    candidate_mae = _mean(frame["candidate_abs_error"])
    baseline_direction = _mean_bool(frame["baseline_direction_hit"])
    candidate_direction = _mean_bool(frame["candidate_direction_hit"])
    baseline_coverage = _mean_bool(frame["baseline_covered"])
    candidate_coverage = _mean_bool(frame["candidate_covered"])
    baseline_width = _average_width(backtest, "baseline_range_low", "baseline_range_high")
    candidate_width = _average_width(backtest, "candidate_range_low", "candidate_range_high")
    baseline_low_breach = _mean_bool(frame["baseline_low_breach"])
    candidate_low_breach = _mean_bool(frame["candidate_low_breach"])
    mean_weight = _mean(frame["dynamic_overlay_weight"])
    checks = {
        "mae_not_worse": candidate_mae <= baseline_mae + 1e-12,
        "direction_not_worse": candidate_direction + 1e-12 >= baseline_direction,
        "coverage": float(settings["coverage_lower_bound"]) <= candidate_coverage <= float(settings["coverage_upper_bound"]),
        "width": candidate_width <= baseline_width * float(settings["max_average_width_ratio"]) + 1e-12,
        "low_breach": candidate_low_breach <= baseline_low_breach + float(settings["max_low_breach_worsening"]) + 1e-12,
        "effective_overlay": mean_weight >= float(settings["min_effective_weight"]),
    }
    metrics = {
        "oos_count": int(len(frame)),
        "baseline_mae": baseline_mae,
        "candidate_mae": candidate_mae,
        "baseline_direction_accuracy": baseline_direction,
        "candidate_direction_accuracy": candidate_direction,
        "baseline_coverage": baseline_coverage,
        "candidate_coverage": candidate_coverage,
        "baseline_average_width": baseline_width,
        "candidate_average_width": candidate_width,
        "baseline_low_breach_rate": baseline_low_breach,
        "candidate_low_breach_rate": candidate_low_breach,
        "candidate_interval_score": _mean(frame["candidate_interval_score"]),
        "baseline_interval_score": _mean(frame["baseline_interval_score"]),
        "mean_dynamic_overlay_weight": mean_weight,
        "live_observation_count": int(live_count),
        "checks": checks,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        status = "not_ready" if failed == ["effective_overlay"] else "rejected"
        return status, "; ".join(failed), metrics
    if live_count < int(settings["min_live_observations"]):
        return "not_ready", f"requires {settings['min_live_observations']} live mature observations", metrics
    return "accepted_shadow", None, metrics


def _market_returns(conn) -> pd.DataFrame:
    if not table_exists(conn, "benchmark_daily"):
        return pd.DataFrame(columns=["date", "market", "return_1d"])
    frame = conn.execute(
        """
        SELECT date, benchmark, close
        FROM benchmark_daily
        WHERE benchmark IN ('KOSPI', 'KOSDAQ', '1001', '2001')
        ORDER BY benchmark, date
        """
    ).fetchdf()
    if frame.empty:
        return pd.DataFrame(columns=["date", "market", "return_1d"])
    frame["market"] = frame["benchmark"].map({"1001": "KOSPI", "2001": "KOSDAQ"}).fillna(frame["benchmark"])
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["return_1d"] = frame.groupby("market", sort=False)["close"].pct_change()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    return frame[["date", "market", "return_1d"]].dropna(subset=["return_1d"])


def _feedback_summary(conn, *, market: str) -> dict[str, Any]:
    if not table_exists(conn, "market_us_linkage_feedback_daily"):
        return {"count": 0, "live_count": 0}
    params: list[Any] = []
    where = ""
    if market != "all":
        where = "WHERE market = ?"
        params.append(str(market).upper())
    frame = conn.execute(
        f"""
        SELECT * FROM market_us_linkage_feedback_daily
        {where}
        ORDER BY prediction_date DESC
        """,
        params,
    ).fetchdf()
    if frame.empty:
        return {"count": 0, "live_count": 0}
    return {
        "count": int(len(frame)),
        "live_count": int((~frame["is_backtest"].fillna(True).astype(bool)).sum()),
        "baseline_mae": _mean(frame["baseline_abs_error"]),
        "candidate_mae": _mean(frame["candidate_abs_error"]),
        "candidate_direction_accuracy": _mean_bool(frame["candidate_direction_hit"]),
        "candidate_coverage": _mean_bool(frame["candidate_covered"]),
        "mean_dynamic_overlay_weight": _mean(frame["dynamic_overlay_weight"]),
    }


def _correlation_summary(conn, *, market: str) -> list[dict[str, Any]]:
    if not table_exists(conn, "market_us_linkage_features_daily"):
        return []
    where = ""
    params: list[Any] = []
    if market != "all":
        where = "WHERE market = ?"
        params.append(str(market).upper())
    frame = conn.execute(
        f"""
        SELECT prediction_date, market, label_return, us_nasdaq_return_1d, us_sox_return_1d
        FROM market_us_linkage_features_daily
        {where}
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY prediction_date, market
          ORDER BY created_at DESC
        ) = 1
        ORDER BY prediction_date
        """,
        params,
    ).fetchdf()
    records: list[dict[str, Any]] = []
    for market_name, group in frame.groupby("market", sort=False):
        values = group.copy()
        values["label_return"] = pd.to_numeric(values["label_return"], errors="coerce")
        for label, column in (("nasdaq", "us_nasdaq_return_1d"), ("sox", "us_sox_return_1d")):
            subset = values[["label_return", column]].dropna()
            recent = subset.tail(60)
            records.append(
                {
                    "market": str(market_name),
                    "signal": label,
                    "sample_count": int(len(subset)),
                    "pearson": _correlation(subset["label_return"], subset[column]),
                    "recent_60d_pearson": _correlation(recent["label_return"], recent[column]),
                }
            )
    return records


def _live_feedback_count(conn, *, market: str | None = None) -> int:
    if not table_exists(conn, "market_us_linkage_feedback_daily"):
        return 0
    params: list[Any] = []
    clause = "WHERE is_backtest = FALSE"
    if market:
        clause += " AND market = ?"
        params.append(str(market).upper())
    return int(conn.execute(f"SELECT COUNT(*) FROM market_us_linkage_feedback_daily {clause}", params).fetchone()[0])


def _mature_forecast_history(
    conn,
    *,
    market: str,
    before_prediction_date: date | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Return only forecasts whose realised target close was already known.

    The order is chronological after the bounded query so the final item is
    the newest mature observation.  No later realised close is allowed into a
    live forecast's dynamic weight.
    """
    if not market or before_prediction_date is None or not table_exists(conn, "market_us_linkage_shadow_forecasts"):
        return []
    frame = conn.execute(
        """
        SELECT
          prediction_date,
          actual_return,
          baseline_expected_return,
          global_expected_return,
          candidate_expected_return
        FROM market_us_linkage_shadow_forecasts
        WHERE market = ?
          AND prediction_date < ?
          AND actual_return IS NOT NULL
        ORDER BY prediction_date DESC, is_backtest ASC, created_at DESC
        LIMIT ?
        """,
        [str(market).upper(), before_prediction_date, int(max(1, limit))],
    ).fetchdf()
    if frame.empty:
        return []
    return frame.sort_values("prediction_date").to_dict(orient="records")


def _insert_immutable_rows(conn, table: str, frame: pd.DataFrame, key_columns: list[str]) -> None:
    """Insert first-seen forecast evidence without rewriting earlier values."""
    if frame.empty:
        return
    aligned = align_to_table_columns(conn, table, frame)
    temporary = f"tmp_{table}_{uuid4().hex}"
    conn.register(temporary, aligned)
    columns = ", ".join(aligned.columns)
    comparisons = " AND ".join(f"existing.{column} = {temporary}.{column}" for column in key_columns)
    try:
        conn.execute(
            f"""
            INSERT INTO {table} ({columns})
            SELECT {columns}
            FROM {temporary}
            WHERE NOT EXISTS (
              SELECT 1 FROM {table} AS existing WHERE {comparisons}
            )
            """
        )
    finally:
        conn.unregister(temporary)


def _artifact_path(config: Mapping[str, Any] | None) -> Path:
    root = Path(((config or {}).get("paths") or {}).get("model_dir") or "models")
    return root / "market_us_linkage_shadow" / "model.joblib"


def _write_artifact(config: Mapping[str, Any] | None, settings: Mapping[str, Any], artifacts: dict[str, Any]) -> None:
    path = _artifact_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model_name": settings["model_name"],
            "model_version": MODEL_VERSION,
            "feature_version": settings["feature_version"],
            "feature_columns": MODEL_FEATURE_COLUMNS,
            "settings": dict(settings),
            "models": artifacts,
            "created_at": _utcnow().isoformat(),
        },
        path,
    )


def _market_filter(market: str) -> tuple[str, list[Any]]:
    value = str(market or "all").upper()
    if value == "ALL":
        return "", []
    if value not in MARKETS:
        raise ValueError("market must be KOSPI, KOSDAQ, or all")
    return "WHERE market = ?", [value]


def _overall_status(rows: list[Mapping[str, Any]]) -> str:
    statuses = [str(row.get("gate_status") or "not_ready") for row in rows]
    if not statuses:
        return "not_ready"
    if "rejected" in statuses:
        return "rejected"
    if all(status == "accepted_shadow" for status in statuses):
        return "accepted_shadow"
    return "not_ready"


def _empty_run(message: str) -> dict[str, Any]:
    return {"status": "not_ready", "model_name": MODEL_NAME, "production_weight": 0.0, "markets": [], "message": message}


def _empty_payload(message: str) -> dict[str, Any]:
    return {
        "status": "not_ready",
        "model_name": MODEL_NAME,
        "production_weight": 0.0,
        "items": [],
        "runs": [],
        "feedback": {"count": 0, "live_count": 0},
        "correlations": [],
        "message": message,
    }


def _empty_feature_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=FEATURE_STORE_COLUMNS)


def _column_or_nan(frame: pd.DataFrame, column: str) -> pd.Series:
    if column in frame.columns:
        return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(np.nan, index=frame.index, dtype=float)


def _safe_sigma(value: Any) -> float:
    number = _number(value)
    return max(0.006, min(0.08, number if number is not None and number > 0 else 0.015))


def _direction_hit(actual: float, predicted: float) -> bool:
    return bool((actual >= 0.0) == (predicted >= 0.0))


def _covered(actual: float, low: Any, high: Any) -> bool | None:
    lower, upper = _number(low), _number(high)
    if lower is None or upper is None:
        return None
    return bool(lower <= actual <= upper)


def _low_breach(actual: float, low: Any) -> bool | None:
    lower = _number(low)
    return None if lower is None else bool(actual < lower)


def _interval_score(actual: float, low: Any, high: Any) -> float | None:
    lower, upper = _number(low), _number(high)
    if lower is None or upper is None or upper < lower:
        return None
    score = upper - lower
    if actual < lower:
        score += (2.0 / ALPHA) * (lower - actual)
    elif actual > upper:
        score += (2.0 / ALPHA) * (actual - upper)
    return float(score)


def _average_width(rows: list[dict[str, Any]], low_key: str, high_key: str) -> float | None:
    values = [
        float(high - low)
        for row in rows
        if (low := _number(row.get(low_key))) is not None and (high := _number(row.get(high_key))) is not None and high >= low
    ]
    return _mean(values)


def _mean(values: Any) -> float | None:
    series = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    return None if series.empty else float(series.mean())


def _mean_bool(values: Any) -> float | None:
    series = pd.Series(values).dropna()
    return None if series.empty else float(series.astype(bool).mean())


def _correlation(left: pd.Series, right: pd.Series) -> float | None:
    if len(left) < 3 or len(right) < 3:
        return None
    value = left.corr(right)
    return _number(value)


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _json_safe(value) for key, value in row.items()}


def _forecast_record(row: Mapping[str, Any]) -> dict[str, Any]:
    item = _record(row)
    item["attribution"] = _loads(item.pop("attribution_json", None), {})
    item["metrics"] = _loads(item.pop("metrics_json", None), {})
    item["data_quality"] = _loads(item.pop("data_quality_json", None), {})
    return item


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def _timestamp_or_none(value: Any) -> datetime | None:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime()


def _date_string(value: Any) -> str | None:
    parsed = _date_or_none(value)
    return parsed.isoformat() if parsed else None


def _timestamp_string(value: Any) -> str | None:
    parsed = _timestamp_or_none(value)
    return parsed.isoformat() if parsed else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return _number(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    try:
        return None if pd.isna(value) else value
    except (TypeError, ValueError):
        return value


def _json(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, allow_nan=False)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(dict(result[key]), value)
        else:
            result[key] = value
    return result
