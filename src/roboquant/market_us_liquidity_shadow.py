"""Cutoff-safe U.S. ETF price/volume overlay for KRX market outlook research.

The production Ridge market outlook remains unchanged.  This module uses the
already collected local-PoC global OHLCV history to test a separate Huber/Ridge
overlay and records immutable shadow evidence only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
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

from roboquant.db import append_dedup_table, table_exists
from roboquant.market_outlook import (
    _fit_market_horizon_spec,
    _predict_subset,
    market_outlook_input_status,
)
from roboquant.market_us_linkage import (
    MARKETS,
    _average_width,
    _covered,
    _date_or_none,
    _deep_merge,
    _direction_hit,
    _forecast_record,
    _insert_immutable_rows,
    _interval_score,
    _join_baseline_dataset,
    _json,
    _json_safe,
    _load_final_premarket_rows,
    _loads,
    _low_breach,
    _market_filter,
    _market_returns,
    _mean,
    _mean_bool,
    _number,
    _overall_status,
    _record,
    _safe_sigma,
    _timestamp_or_none,
    _utcnow,
)
from roboquant.registry.model_registry import register_feature_set, register_model

MODEL_NAME = "market-us-liquidity-shadow-v1"
MODEL_VERSION = f"{MODEL_NAME}-v1"
FEATURE_VERSION = "market_us_liquidity_v1"
FEATURE_SET_NAME = "market_us_liquidity_shadow_v1"

ETF_PREFIX = {
    "QQQ": "qqq",
    "SOXX": "soxx",
    "SMH": "smh",
    "SPY": "spy",
    "EWY": "ewy",
    "KORU": "koru",
}
RETURN_COLUMNS = [f"us_{prefix}_return_1d" for prefix in ETF_PREFIX.values()]
VOLUME_COLUMNS = [f"us_{prefix}_volume_z20" for prefix in ETF_PREFIX.values()]
FEATURE_COLUMNS = [
    *RETURN_COLUMNS,
    *VOLUME_COLUMNS,
    "tech_down_volume_pressure",
    "broad_down_volume_pressure",
    "tech_broad_volume_divergence",
]
MODEL_FEATURE_COLUMNS = [
    "us_qqq_return_1d",
    "us_soxx_return_1d",
    "us_spy_return_1d",
    "us_ewy_return_1d",
    "us_qqq_volume_z20",
    "us_soxx_volume_z20",
    "tech_down_volume_pressure",
    "broad_down_volume_pressure",
    "tech_broad_volume_divergence",
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
    "liquidity_expected_return",
    "liquidity_delta",
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
    "liquidity_abs_error",
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


def market_us_liquidity_settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    defaults = {
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
        "coverage_lower_bound": 0.75,
        "coverage_upper_bound": 0.85,
        "max_average_width_ratio": 1.10,
        "max_low_breach_worsening": 0.02,
        "huber_epsilon": 1.35,
        "huber_alpha": 0.0005,
        "huber_max_iter": 300,
    }
    values = _deep_merge(defaults, dict((config or {}).get("market_us_liquidity_shadow") or {}))
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
        "coverage_lower_bound",
        "coverage_upper_bound",
        "max_average_width_ratio",
        "max_low_breach_worsening",
        "huber_epsilon",
        "huber_alpha",
    ):
        values[key] = float(values[key])
    values["max_overlay_weight"] = min(0.20, max(0.0, values["max_overlay_weight"]))
    values["model_name"] = str(values["model_name"])
    values["feature_version"] = str(values["feature_version"])
    return values


def build_market_us_liquidity_features(
    conn,
    config: Mapping[str, Any] | None = None,
    *,
    asof_date: str | date = "latest",
) -> pd.DataFrame:
    """Join final premarket timing to U.S. ETF OHLCV without look-ahead."""
    settings = market_us_liquidity_settings(config)
    premarket = _load_final_premarket_rows(conn, asof_date=asof_date)
    if premarket.empty:
        return _empty_feature_frame()
    daily = _load_etf_liquidity(conn)
    if daily.empty:
        return _empty_feature_frame()
    premarket["global_trade_date"] = pd.to_datetime(premarket["global_trade_date"], errors="coerce").dt.date
    output = premarket.merge(daily, left_on="global_trade_date", right_on="trade_date", how="left")
    output["prediction_date"] = pd.to_datetime(output["prediction_date"], errors="coerce").dt.date
    output["asof_date"] = pd.to_datetime(output["asof_date"], errors="coerce").dt.date
    output["global_trade_date"] = pd.to_datetime(output["global_trade_date"], errors="coerce").dt.date
    output["global_available_at"] = pd.to_datetime(output.get("available_at"), errors="coerce")
    output["prediction_cutoff"] = pd.to_datetime(output.get("prediction_cutoff"), errors="coerce")
    for column in FEATURE_COLUMNS:
        output[column] = pd.to_numeric(output.get(column), errors="coerce")
    output["label_return"] = pd.to_numeric(output.get("label_return"), errors="coerce")
    output["data_quality_json"] = output.apply(_feature_quality, axis=1).map(_json)
    output["feature_version"] = str(settings["feature_version"])
    output["created_at"] = _utcnow()
    for column in FEATURE_STORE_COLUMNS:
        if column not in output:
            output[column] = None
    return output[FEATURE_STORE_COLUMNS].sort_values(["prediction_date", "market"]).reset_index(drop=True)


def refresh_market_us_liquidity_features(conn, frame: pd.DataFrame) -> pd.DataFrame:
    if not frame.empty:
        append_dedup_table(conn, "market_us_liquidity_features_daily", frame, ["prediction_date", "market", "feature_version"])
    return frame


def run_market_us_liquidity_shadow(
    conn,
    config: Mapping[str, Any] | None = None,
    *,
    asof_date: str | date = "latest",
    include_live: bool = True,
) -> dict[str, Any]:
    settings = market_us_liquidity_settings(config)
    if not settings["enabled"]:
        return _empty_run("U.S. liquidity shadow is disabled")
    features = refresh_market_us_liquidity_features(conn, build_market_us_liquidity_features(conn, config, asof_date=asof_date))
    data = _join_baseline_dataset(conn, features, config) if not features.empty else pd.DataFrame()
    if data.empty:
        return _empty_run("cutoff-safe U.S. ETF liquidity history is unavailable")

    forecast_rows: list[dict[str, Any]] = []
    feedback_rows: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {}
    for market in MARKETS:
        market_data = data[data["market"].astype(str).eq(market)].sort_values("prediction_date").reset_index(drop=True)
        backtest, artifact = _walk_forward(market_data, settings)
        feedback_rows.extend(_feedback_row(row) for row in backtest if _number(row.get("actual_return")) is not None)
        live_count = _live_feedback_count(conn, market)
        gate_status, fail_reason, metrics = _evaluate_gate(backtest, settings, live_count)
        live_rows, live_message, live_artifact = ([], None, None)
        if include_live:
            live_rows, live_message, live_artifact = _build_live(conn, market_data, backtest, settings, config)
        if live_artifact is not None:
            artifact["live"] = live_artifact
        forecast_rows.extend(backtest)
        forecast_rows.extend(live_rows)
        artifacts[market] = artifact
        runs.append(
            {
                "run_id": uuid4().hex,
                "model_name": settings["model_name"],
                "model_version": MODEL_VERSION,
                "market": market,
                "start_date": _date_or_none(market_data["prediction_date"].min() if not market_data.empty else None),
                "end_date": _date_or_none(market_data["prediction_date"].max() if not market_data.empty else None),
                "oos_count": len(backtest),
                "live_observation_count": live_count,
                "gate_status": gate_status,
                "fail_reason": fail_reason or live_message,
                "metrics_json": _json(metrics),
                "created_at": _utcnow(),
            }
        )
    _insert_immutable_rows(conn, "market_us_liquidity_shadow_forecasts", pd.DataFrame(forecast_rows, columns=FORECAST_COLUMNS), ["prediction_date", "market", "model_version", "is_backtest"])
    _insert_immutable_rows(conn, "market_us_liquidity_shadow_feedback_daily", pd.DataFrame(feedback_rows, columns=FEEDBACK_COLUMNS), ["prediction_date", "market", "model_version", "is_backtest"])
    append_dedup_table(conn, "market_us_liquidity_shadow_backtest_runs", pd.DataFrame(runs), ["run_id"])
    _write_artifact(config, settings, artifacts)
    status = _overall_status(runs)
    register_feature_set(conn, FEATURE_SET_NAME, MODEL_FEATURE_COLUMNS, status="experimental", description="Cutoff-safe U.S. ETF OHLCV and volume anomaly overlay; news excluded.")
    register_model(
        conn,
        model_name=settings["model_name"],
        model_type="us_etf_liquidity_market_overlay_shadow",
        feature_set_name=FEATURE_SET_NAME,
        label_name="next_trading_day_index_return",
        horizons=["TODAY"],
        artifact_path=str(_artifact_path(config)),
        metrics={row["market"]: _loads(row["metrics_json"], {}) for row in runs},
        split={"mode": "expanding_walk_forward", "purge_days": settings["purge_days"]},
        status=status,
        production_weight=0.0,
        shadow_mode=True,
        fail_reason="; ".join(str(row["fail_reason"]) for row in runs if row.get("fail_reason")) or None,
    )
    return {"status": status, "model_name": settings["model_name"], "model_version": MODEL_VERSION, "feature_rows": len(features), "markets": [_record(row) for row in runs], "production_weight": 0.0}


def update_market_us_liquidity_feedback(conn, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    del config
    if not table_exists(conn, "market_us_liquidity_shadow_forecasts"):
        return {"status": "not_ready", "updated": 0, "message": "liquidity shadow schema is unavailable"}
    returns = _market_returns(conn)
    if returns.empty:
        return {"status": "not_ready", "updated": 0, "message": "benchmark closes are unavailable"}
    pending = conn.execute("SELECT * FROM market_us_liquidity_shadow_forecasts WHERE is_backtest = FALSE AND actual_return IS NULL ORDER BY prediction_date, market").fetchdf()
    returns_by_key = {(_date_or_none(row.date), str(row.market)): _number(row.return_1d) for row in returns.itertuples(index=False)}
    rows: list[dict[str, Any]] = []
    for value in pending.to_dict(orient="records"):
        actual = returns_by_key.get((_date_or_none(value.get("prediction_date")), str(value.get("market") or "")))
        if actual is None:
            continue
        value["actual_return"] = actual
        rows.append(_feedback_row(value))
        conn.execute("UPDATE market_us_liquidity_shadow_forecasts SET actual_return = ? WHERE prediction_date = ? AND market = ? AND model_version = ? AND is_backtest = FALSE", [actual, _date_or_none(value.get("prediction_date")), str(value.get("market") or ""), str(value.get("model_version") or MODEL_VERSION)])
    _insert_immutable_rows(conn, "market_us_liquidity_shadow_feedback_daily", pd.DataFrame(rows, columns=FEEDBACK_COLUMNS), ["prediction_date", "market", "model_version", "is_backtest"])
    return {"status": "ready" if rows else "pending", "updated": len(rows), "message": "mature live KRX closes recorded" if rows else "target sessions have not closed yet"}


def get_market_us_liquidity_shadow(conn, *, market: str = "all", limit: int = 20) -> dict[str, Any]:
    if not table_exists(conn, "market_us_liquidity_shadow_backtest_runs"):
        return _empty_payload("U.S. liquidity shadow schema is unavailable")
    filter_sql, params = _market_filter(market)
    runs = conn.execute(f"SELECT * FROM market_us_liquidity_shadow_backtest_runs {filter_sql} QUALIFY ROW_NUMBER() OVER (PARTITION BY market ORDER BY created_at DESC) = 1 ORDER BY CASE market WHEN 'KOSPI' THEN 1 WHEN 'KOSDAQ' THEN 2 ELSE 3 END", params).fetchdf()
    forecast_where = "is_backtest = FALSE" + (" AND market = ?" if market != "all" else "")
    forecasts = conn.execute(f"SELECT * FROM market_us_liquidity_shadow_forecasts WHERE {forecast_where} QUALIFY ROW_NUMBER() OVER (PARTITION BY market ORDER BY prediction_date DESC, created_at DESC) = 1 ORDER BY CASE market WHEN 'KOSPI' THEN 1 WHEN 'KOSDAQ' THEN 2 ELSE 3 END", params).fetchdf()
    run_items = []
    for row in runs.to_dict(orient="records"):
        item = _record(row)
        item["metrics"] = _loads(item.pop("metrics_json", None), {})
        run_items.append(item)
    items = [_forecast_record(row) for row in forecasts.to_dict(orient="records")]
    return {
        "status": "ready" if items else _overall_status(run_items),
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "production_weight": 0.0,
        "items": items,
        "runs": run_items,
        "feedback": _feedback_summary(conn, market),
        "correlations": _correlations(conn, market),
        "message": "미국 ETF 가격·거래량 후보는 backtest/close-loop 검증용 shadow이며 production 시장전망을 변경하지 않습니다.",
        "disclaimer": "연구·정보제공용 미국 거래량 연동 검증입니다. 매수·매도 지시나 수익 보장이 아닙니다.",
    }


def _load_etf_liquidity(conn) -> pd.DataFrame:
    if not table_exists(conn, "global_market_daily"):
        return pd.DataFrame()
    symbols = ", ".join(repr(item) for item in ETF_PREFIX)
    raw = conn.execute(
        f"""
        SELECT trade_date, symbol, close, volume, return_1d, source_timestamp, ingested_at
        FROM global_market_daily
        WHERE symbol IN ({symbols})
        QUALIFY ROW_NUMBER() OVER (PARTITION BY trade_date, symbol ORDER BY source_timestamp DESC NULLS LAST, ingested_at DESC NULLS LAST) = 1
        ORDER BY symbol, trade_date
        """
    ).fetchdf()
    if raw.empty:
        return pd.DataFrame()
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], errors="coerce").dt.date
    raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
    raw["volume"] = pd.to_numeric(raw["volume"], errors="coerce")
    raw["return_1d"] = pd.to_numeric(raw["return_1d"], errors="coerce")
    raw["return_1d"] = raw["return_1d"].combine_first(raw.groupby("symbol", sort=False)["close"].pct_change())
    raw["dollar_volume"] = raw["close"] * raw["volume"]
    for value_column, output_column in (("volume", "volume_z20"), ("dollar_volume", "dollar_volume_z20")):
        mean = raw.groupby("symbol", sort=False)[value_column].transform(lambda series: series.rolling(20, min_periods=20).mean())
        std = raw.groupby("symbol", sort=False)[value_column].transform(lambda series: series.rolling(20, min_periods=20).std())
        raw[output_column] = (raw[value_column] - mean) / std.replace(0.0, np.nan)
    records: list[dict[str, Any]] = []
    for trade_date, group in raw.groupby("trade_date", sort=True):
        values: dict[str, Any] = {"trade_date": trade_date}
        for item in group.itertuples(index=False):
            prefix = ETF_PREFIX.get(str(item.symbol))
            if prefix is None:
                continue
            values[f"us_{prefix}_return_1d"] = _number(item.return_1d)
            values[f"us_{prefix}_volume_z20"] = _number(item.volume_z20)
        tech_returns = [values.get("us_qqq_return_1d"), values.get("us_soxx_return_1d"), values.get("us_smh_return_1d")]
        tech_volume = [values.get("us_qqq_volume_z20"), values.get("us_soxx_volume_z20"), values.get("us_smh_volume_z20")]
        broad_returns = [values.get("us_spy_return_1d"), values.get("us_ewy_return_1d"), values.get("us_koru_return_1d")]
        broad_volume = [values.get("us_spy_volume_z20"), values.get("us_ewy_volume_z20"), values.get("us_koru_volume_z20")]
        values["tech_down_volume_pressure"] = _down_pressure(tech_returns, tech_volume)
        values["broad_down_volume_pressure"] = _down_pressure(broad_returns, broad_volume)
        values["tech_broad_volume_divergence"] = _finite_mean(tech_volume) - _finite_mean(broad_volume) if _finite_mean(tech_volume) is not None and _finite_mean(broad_volume) is not None else np.nan
        records.append(values)
    frame = pd.DataFrame(records)
    for column in FEATURE_COLUMNS:
        if column not in frame:
            frame[column] = np.nan
    return frame


def _feature_quality(row: pd.Series) -> dict[str, Any]:
    missing = [column for column in MODEL_FEATURE_COLUMNS if pd.isna(row.get(column))]
    available_at = pd.to_datetime(row.get("global_available_at"), errors="coerce")
    cutoff = pd.to_datetime(row.get("prediction_cutoff"), errors="coerce")
    cutoff_safe = not pd.isna(available_at) and not pd.isna(cutoff) and available_at <= cutoff
    return {
        "status": "ready" if bool(row.get("is_final_close")) and cutoff_safe and not missing else "partial_ready",
        "is_final_close": bool(row.get("is_final_close")),
        "global_trade_date": str(row.get("global_trade_date") or "")[:10],
        "cutoff_safe": cutoff_safe,
        "missing_features": missing,
        "news_llm_policy": "excluded",
        "source": "global_market_daily:yfinance_poc",
    }


def _walk_forward(frame: pd.DataFrame, settings: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = frame.dropna(subset=["prediction_date", "asof_date", "label_return"]).sort_values("prediction_date").reset_index(drop=True)
    first = int(settings["initial_train_days"]) + int(settings["purge_days"])
    if len(data) <= first:
        return [], {"status": "not_ready", "rows": len(data)}
    rows: list[dict[str, Any]] = []
    baseline_spec = None
    model = None
    metadata: dict[str, Any] = {}
    for index in range(first, len(data)):
        train_end = index - int(settings["purge_days"])
        if baseline_spec is None or (index - first) % int(settings["retrain_step_days"]) == 0:
            train = data.iloc[:train_end]
            baseline_spec = _fit_market_horizon_spec(train)
            model, metadata = _fit_model(train, settings)
        item = data.iloc[index]
        baseline = float(_predict_subset(item.to_frame().T, baseline_spec).iloc[0])
        liquidity, attribution = _predict_model(model, item)
        weight_info = _weight_info(rows[-int(settings["feedback_window"]) :], settings)
        candidate = baseline + float(weight_info["weight"]) * (liquidity - baseline)
        baseline_sigma = _safe_sigma(baseline_spec.get("residual_std"))
        baseline_range = (baseline - baseline_sigma, baseline + baseline_sigma)
        control = _residual_range(rows, baseline, "baseline_expected_return", baseline_range, settings)
        candidate_range = _residual_range(rows, candidate, "candidate_expected_return", control[:2], settings)
        rows.append(_forecast(item, baseline, liquidity, float(weight_info["weight"]), candidate, baseline_range, control[:2], candidate_range[:2], _number(item.label_return), int(candidate_range[2]), weight_info, attribution, "backtest", True, _loads(item.data_quality_json, {}), {"model": metadata, "weight_reason": weight_info["reason"]}))
    return rows, {"status": "ready", "metadata": metadata}


def _build_live(conn, data: pd.DataFrame, backtest: list[dict[str, Any]], settings: Mapping[str, Any], config: Mapping[str, Any] | None):
    labelled = data[data["label_return"].notna()].copy()
    candidates = data[data["label_return"].isna()].copy()
    if candidates.empty:
        latest_label = labelled["prediction_date"].max() if not labelled.empty else None
        candidates = data[data["prediction_date"].gt(latest_label)].copy() if latest_label else data.tail(1).copy()
    if candidates.empty or len(labelled) < int(settings["initial_train_days"]):
        return [], "latest liquidity target is not ready", None
    latest = candidates.sort_values("prediction_date").tail(1).iloc[0]
    context = market_outlook_input_status(conn, asof_date=_date_or_none(latest.asof_date), target_date=_date_or_none(latest.prediction_date), config=dict(config or {}), calendar_source="us_etf_liquidity_v1", calendar_messages=[])
    if context.get("forecast_status") != "confirmed":
        return [], "blocked_stale_input", None
    quality = _loads(latest.data_quality_json, {})
    if quality.get("status") != "ready":
        return [], "blocked_missing_liquidity_input", None
    baseline_spec = _fit_market_horizon_spec(labelled)
    model, metadata = _fit_model(labelled, settings)
    baseline = float(_predict_subset(latest.to_frame().T, baseline_spec).iloc[0])
    liquidity, attribution = _predict_model(model, latest)
    history = _mature_history(conn, str(latest.market), _date_or_none(latest.prediction_date), int(settings["feedback_window"])) or backtest[-int(settings["feedback_window"]) :]
    weight_info = _weight_info(history, settings)
    candidate = baseline + float(weight_info["weight"]) * (liquidity - baseline)
    sigma = _safe_sigma(baseline_spec.get("residual_std"))
    baseline_range = (baseline - sigma, baseline + sigma)
    control = _residual_range(backtest[-int(settings["calibration_window"]) :], baseline, "baseline_expected_return", baseline_range, settings)
    candidate_range = _residual_range(backtest[-int(settings["calibration_window"]) :], candidate, "candidate_expected_return", control[:2], settings)
    row = _forecast(latest, baseline, liquidity, float(weight_info["weight"]), candidate, baseline_range, control[:2], candidate_range[:2], None, int(candidate_range[2]), weight_info, attribution, "experimental", False, {**quality, "forecast_context": _json_safe(context)}, {"model": metadata, "weight_reason": weight_info["reason"]})
    return [row], None, {"baseline_spec": baseline_spec, "liquidity_model": model, "metadata": metadata}


def _fit_model(train: pd.DataFrame, settings: Mapping[str, Any]) -> tuple[Pipeline, dict[str, Any]]:
    x = train.reindex(columns=MODEL_FEATURE_COLUMNS).apply(pd.to_numeric, errors="coerce")
    y = pd.to_numeric(train["label_return"], errors="coerce")
    valid = y.notna()
    steps = [("imputer", SimpleImputer(strategy="median")), ("scaler", StandardScaler()), ("regressor", HuberRegressor(epsilon=float(settings["huber_epsilon"]), alpha=float(settings["huber_alpha"]), max_iter=int(settings["huber_max_iter"])))]
    model: Pipeline = Pipeline(steps)
    try:
        model.fit(x.loc[valid], y.loc[valid])
        kind = "huber"
    except (ValueError, FloatingPointError):
        model = Pipeline([("imputer", SimpleImputer(strategy="median")), ("scaler", StandardScaler()), ("regressor", Ridge(alpha=1.0))])
        model.fit(x.loc[valid], y.loc[valid])
        kind = "ridge_fallback"
    return model, {"model_type": kind, "train_rows": int(valid.sum()), "feature_columns": MODEL_FEATURE_COLUMNS}


def _predict_model(model: Pipeline, row: pd.Series) -> tuple[float, dict[str, Any]]:
    x = pd.DataFrame([{column: _number(row.get(column)) for column in MODEL_FEATURE_COLUMNS}])
    value = float(model.predict(x)[0])
    standardized = model.named_steps["scaler"].transform(model.named_steps["imputer"].transform(x))[0]
    regressor = model.named_steps["regressor"]
    coefficients = np.asarray(getattr(regressor, "coef_", np.zeros(len(MODEL_FEATURE_COLUMNS))), dtype=float)
    return value, {"liquidity_feature_contributions": {name: float(raw * coefficient) for name, raw, coefficient in zip(MODEL_FEATURE_COLUMNS, standardized, coefficients, strict=False)}, "liquidity_intercept": _number(getattr(regressor, "intercept_", None)), "raw_features": {column: _number(row.get(column)) for column in MODEL_FEATURE_COLUMNS}}


def _weight_info(history: list[dict[str, Any]], settings: Mapping[str, Any]) -> dict[str, Any]:
    valid = [row for row in history if _number(row.get("actual_return")) is not None and _number(row.get("baseline_expected_return")) is not None and _number(row.get("liquidity_expected_return")) is not None]
    if len(valid) < int(settings["min_feedback_rows"]):
        return {"weight": 0.0, "relative_improvement": None, "count": len(valid), "reason": "insufficient_mature_feedback"}
    baseline = float(np.mean([abs(float(row["actual_return"]) - float(row["baseline_expected_return"])) for row in valid]))
    liquidity = float(np.mean([abs(float(row["actual_return"]) - float(row["liquidity_expected_return"])) for row in valid]))
    if not math.isfinite(baseline) or baseline <= 1e-12 or not math.isfinite(liquidity):
        return {"weight": 0.0, "relative_improvement": None, "count": len(valid), "reason": "invalid_feedback_error"}
    improvement = (baseline - liquidity) / baseline
    weight = float(settings["max_overlay_weight"]) * min(1.0, max(0.0, improvement / max(float(settings["skill_improvement_for_max_weight"]), 1e-9)))
    return {"weight": weight, "relative_improvement": improvement, "count": len(valid), "reason": "positive_liquidity_skill" if weight > 0 else "liquidity_candidate_not_better"}


def _residual_range(history: list[dict[str, Any]], center: float, expected_key: str, fallback: tuple[float, float], settings: Mapping[str, Any]) -> tuple[float, float, int]:
    residuals = [float(row["actual_return"]) - float(row[expected_key]) for row in history if _number(row.get("actual_return")) is not None and _number(row.get(expected_key)) is not None]
    if len(residuals) < int(settings["min_calibration_rows"]):
        return float(fallback[0]), float(fallback[1]), len(residuals)
    lower, upper = np.quantile(np.asarray(residuals), [0.10, 0.90])
    return float(center + lower), float(center + upper), len(residuals)


def _forecast(item, baseline, liquidity, weight, candidate, baseline_range, control_range, candidate_range, actual, calibration_count, weight_info, attribution, status, is_backtest, quality, metrics):
    return {
        "prediction_date": _date_or_none(item.get("prediction_date")), "asof_date": _date_or_none(item.get("asof_date")), "market": str(item.get("market") or ""), "prediction_cutoff": _timestamp_or_none(item.get("prediction_cutoff")), "global_trade_date": _date_or_none(item.get("global_trade_date")), "model_name": MODEL_NAME, "model_version": MODEL_VERSION,
        "baseline_expected_return": float(baseline), "liquidity_expected_return": float(liquidity), "liquidity_delta": float(liquidity - baseline), "dynamic_overlay_weight": float(weight), "candidate_expected_return": float(candidate),
        "baseline_range_low": float(baseline_range[0]), "baseline_range_high": float(baseline_range[1]), "control_range_low": float(control_range[0]), "control_range_high": float(control_range[1]), "candidate_range_low": float(candidate_range[0]), "candidate_range_high": float(candidate_range[1]),
        "actual_return": _number(actual), "calibration_count": int(calibration_count), "feedback_count": int(weight_info["count"]), "relative_mae_improvement": _number(weight_info["relative_improvement"]), "attribution_json": _json(attribution), "status": status, "production_weight": 0.0, "is_backtest": bool(is_backtest), "metrics_json": _json(metrics), "data_quality_json": _json(quality), "created_at": _utcnow(),
    }


def _feedback_row(row: Mapping[str, Any]) -> dict[str, Any]:
    actual, baseline, candidate = (_number(row.get(key)) for key in ("actual_return", "baseline_expected_return", "candidate_expected_return"))
    if actual is None or baseline is None or candidate is None:
        raise ValueError("feedback requires actual, baseline, and candidate returns")
    liquidity = _number(row.get("liquidity_expected_return"))
    return {"prediction_date": _date_or_none(row.get("prediction_date")), "market": str(row.get("market") or ""), "model_name": str(row.get("model_name") or MODEL_NAME), "model_version": str(row.get("model_version") or MODEL_VERSION), "is_backtest": bool(row.get("is_backtest")), "actual_return": actual, "baseline_abs_error": abs(actual - baseline), "candidate_abs_error": abs(actual - candidate), "liquidity_abs_error": None if liquidity is None else abs(actual - liquidity), "baseline_direction_hit": _direction_hit(actual, baseline), "candidate_direction_hit": _direction_hit(actual, candidate), "baseline_covered": _covered(actual, row.get("baseline_range_low"), row.get("baseline_range_high")), "candidate_covered": _covered(actual, row.get("candidate_range_low"), row.get("candidate_range_high")), "baseline_low_breach": _low_breach(actual, row.get("baseline_range_low")), "candidate_low_breach": _low_breach(actual, row.get("candidate_range_low")), "baseline_interval_score": _interval_score(actual, row.get("baseline_range_low"), row.get("baseline_range_high")), "candidate_interval_score": _interval_score(actual, row.get("candidate_range_low"), row.get("candidate_range_high")), "dynamic_overlay_weight": _number(row.get("dynamic_overlay_weight")) or 0.0, "relative_mae_improvement": _number(row.get("relative_mae_improvement")), "status": "mature", "created_at": _utcnow()}


def _evaluate_gate(backtest: list[dict[str, Any]], settings: Mapping[str, Any], live_count: int):
    feedback = [_feedback_row(row) for row in backtest if _number(row.get("actual_return")) is not None]
    if len(feedback) < int(settings["min_oos_rows"]):
        return "not_ready", f"requires {settings['min_oos_rows']} OOS rows", {"oos_count": len(feedback)}
    frame = pd.DataFrame(feedback)
    baseline_mae, candidate_mae = _mean(frame.baseline_abs_error), _mean(frame.candidate_abs_error)
    baseline_direction, candidate_direction = _mean_bool(frame.baseline_direction_hit), _mean_bool(frame.candidate_direction_hit)
    baseline_coverage, candidate_coverage = _mean_bool(frame.baseline_covered), _mean_bool(frame.candidate_covered)
    baseline_width, candidate_width = _average_width(backtest, "baseline_range_low", "baseline_range_high"), _average_width(backtest, "candidate_range_low", "candidate_range_high")
    baseline_breach, candidate_breach = _mean_bool(frame.baseline_low_breach), _mean_bool(frame.candidate_low_breach)
    mean_weight = _mean(frame.dynamic_overlay_weight)
    checks = {"mae_not_worse": candidate_mae <= baseline_mae + 1e-12, "direction_not_worse": candidate_direction + 1e-12 >= baseline_direction, "coverage": float(settings["coverage_lower_bound"]) <= candidate_coverage <= float(settings["coverage_upper_bound"]), "width": candidate_width <= baseline_width * float(settings["max_average_width_ratio"]) + 1e-12, "low_breach": candidate_breach <= baseline_breach + float(settings["max_low_breach_worsening"]), "effective_overlay": mean_weight >= float(settings["min_effective_weight"])}
    metrics = {"oos_count": len(frame), "baseline_mae": baseline_mae, "candidate_mae": candidate_mae, "baseline_direction_accuracy": baseline_direction, "candidate_direction_accuracy": candidate_direction, "baseline_coverage": baseline_coverage, "candidate_coverage": candidate_coverage, "baseline_average_width": baseline_width, "candidate_average_width": candidate_width, "baseline_low_breach_rate": baseline_breach, "candidate_low_breach_rate": candidate_breach, "baseline_interval_score": _mean(frame.baseline_interval_score), "candidate_interval_score": _mean(frame.candidate_interval_score), "mean_dynamic_overlay_weight": mean_weight, "live_observation_count": live_count, "checks": checks}
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        return ("not_ready" if failed == ["effective_overlay"] else "rejected"), "; ".join(failed), metrics
    if live_count < int(settings["min_live_observations"]):
        return "not_ready", f"requires {settings['min_live_observations']} live mature observations", metrics
    return "accepted_shadow", None, metrics


def _mature_history(conn, market: str, before: date | None, limit: int) -> list[dict[str, Any]]:
    if before is None:
        return []
    frame = conn.execute("SELECT prediction_date, actual_return, baseline_expected_return, liquidity_expected_return, candidate_expected_return FROM market_us_liquidity_shadow_forecasts WHERE market = ? AND prediction_date < ? AND actual_return IS NOT NULL ORDER BY prediction_date DESC, is_backtest ASC, created_at DESC LIMIT ?", [market, before, max(1, limit)]).fetchdf()
    return frame.sort_values("prediction_date").to_dict(orient="records") if not frame.empty else []


def _live_feedback_count(conn, market: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM market_us_liquidity_shadow_feedback_daily WHERE is_backtest = FALSE AND market = ?", [market]).fetchone()[0])


def _feedback_summary(conn, market: str) -> dict[str, Any]:
    where, params = ("", []) if market == "all" else ("WHERE market = ?", [market])
    frame = conn.execute(f"SELECT * FROM market_us_liquidity_shadow_feedback_daily {where}", params).fetchdf()
    if frame.empty:
        return {"count": 0, "live_count": 0}
    return {"count": len(frame), "live_count": int((~frame.is_backtest.fillna(True).astype(bool)).sum()), "baseline_mae": _mean(frame.baseline_abs_error), "candidate_mae": _mean(frame.candidate_abs_error), "candidate_direction_accuracy": _mean_bool(frame.candidate_direction_hit), "candidate_coverage": _mean_bool(frame.candidate_covered), "mean_dynamic_overlay_weight": _mean(frame.dynamic_overlay_weight)}


def _correlations(conn, market: str) -> list[dict[str, Any]]:
    where, params = ("", []) if market == "all" else ("WHERE market = ?", [market])
    frame = conn.execute(f"SELECT market, label_return, tech_down_volume_pressure, tech_broad_volume_divergence FROM market_us_liquidity_features_daily {where}", params).fetchdf()
    records = []
    for name, group in frame.groupby("market", sort=False):
        for label, column in (("tech_down_volume_pressure", "tech_down_volume_pressure"), ("tech_broad_volume_divergence", "tech_broad_volume_divergence")):
            values = group[["label_return", column]].dropna()
            records.append({"market": name, "signal": label, "sample_count": len(values), "pearson": _number(values.label_return.corr(values[column])) if not values.empty else None, "recent_60d_pearson": _number(values.tail(60).label_return.corr(values.tail(60)[column])) if len(values) > 1 else None})
    return records


def _down_pressure(returns: list[Any], volumes: list[Any]) -> float:
    values = [min(float(ret), 0.0) * max(float(vol), 0.0) for ret, vol in zip(returns, volumes, strict=False) if _number(ret) is not None and _number(vol) is not None]
    return float(np.mean(values)) if values else np.nan


def _finite_mean(values: list[Any]) -> float | None:
    finite = [float(value) for value in values if _number(value) is not None and math.isfinite(float(value))]
    return float(np.mean(finite)) if finite else None


def _artifact_path(config: Mapping[str, Any] | None) -> Path:
    return Path(((config or {}).get("paths") or {}).get("model_dir") or "models") / "market_us_liquidity_shadow" / "model.joblib"


def _write_artifact(config: Mapping[str, Any] | None, settings: Mapping[str, Any], artifacts: dict[str, Any]) -> None:
    path = _artifact_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model_name": settings["model_name"], "model_version": MODEL_VERSION, "feature_version": settings["feature_version"], "feature_columns": MODEL_FEATURE_COLUMNS, "settings": dict(settings), "models": artifacts, "created_at": _utcnow().isoformat()}, path)


def _empty_feature_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=FEATURE_STORE_COLUMNS)


def _empty_run(message: str) -> dict[str, Any]:
    return {"status": "not_ready", "model_name": MODEL_NAME, "model_version": MODEL_VERSION, "production_weight": 0.0, "feature_rows": 0, "markets": [], "message": message}


def _empty_payload(message: str) -> dict[str, Any]:
    return {"status": "not_ready", "model_name": MODEL_NAME, "model_version": MODEL_VERSION, "production_weight": 0.0, "items": [], "runs": [], "feedback": {"count": 0, "live_count": 0}, "correlations": [], "message": message}
