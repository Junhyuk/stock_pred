"""Shadow-only LightGBM quantile forecasts for next-session market outlooks.

The existing Ridge market outlook remains the production path. This module
keeps its predictions, calibrated ranges, metrics, and attributions in
separate tables until a future explicit promotion decision.
"""

from __future__ import annotations

import json
import math
import warnings
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor

from roboquant.db import append_dedup_table, table_exists
from roboquant.market_outlook import (
    FEATURE_COLUMNS,
    LOCAL_LLM_NEWS_FEATURE_COLUMNS,
    MARKETS,
    _predict_row,
    build_market_outlook_dataset,
    fit_market_outlook_model,
    normal_cdf,
    resolve_market_outlook_asof,
)
from roboquant.registry.model_registry import register_feature_set, register_model

MODEL_NAME = "market-outlook-lgbm-news-shadow-v1"
MODEL_VERSION = f"{MODEL_NAME}-v1"
HORIZON = "TODAY"
FEATURE_SET_NAME = "market_outlook_lgbm_news_shadow_v1"
SHADOW_FEATURE_COLUMNS = [*FEATURE_COLUMNS, *LOCAL_LLM_NEWS_FEATURE_COLUMNS]


def market_outlook_ml_shadow_settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict((config or {}).get("market_outlook_ml_shadow") or {})
    defaults: dict[str, Any] = {
        "enabled": True,
        "model_name": MODEL_NAME,
        "horizon": HORIZON,
        "initial_train_days": 504,
        "purge_days": 1,
        "retrain_step_days": 21,
        "calibration_window": 252,
        "min_calibration_rows": 60,
        "min_signal_days": 60,
        "min_mature_dates": 60,
        "target_coverage": 0.80,
        "coverage_lower_bound": 0.75,
        "coverage_upper_bound": 0.85,
        "max_average_width_ratio": 1.10,
        "max_low_breach_worsening": 0.02,
        "lgbm": {
            "n_estimators": 300,
            "learning_rate": 0.03,
            "num_leaves": 7,
            "min_child_samples": 60,
            "reg_lambda": 10.0,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "random_state": 42,
        },
    }
    settings = _deep_merge(defaults, raw)
    for key, minimum in {
        "initial_train_days": 30,
        "purge_days": 1,
        "retrain_step_days": 1,
        "calibration_window": 20,
        "min_calibration_rows": 10,
        "min_signal_days": 1,
        "min_mature_dates": 1,
    }.items():
        settings[key] = max(minimum, int(settings[key]))
    settings["coverage_lower_bound"] = float(settings["coverage_lower_bound"])
    settings["coverage_upper_bound"] = float(settings["coverage_upper_bound"])
    settings["max_average_width_ratio"] = max(1.0, float(settings["max_average_width_ratio"]))
    settings["max_low_breach_worsening"] = max(0.0, float(settings["max_low_breach_worsening"]))
    settings["model_name"] = str(settings["model_name"])
    settings["horizon"] = str(settings["horizon"]).upper()
    return settings


def default_market_outlook_ml_shadow_model_path(config: Mapping[str, Any]) -> Path:
    model_dir = Path(config["paths"]["model_dir"])
    if model_dir.name == "market_outlook_ml_shadow":
        return model_dir / "model.joblib"
    return model_dir / "market_outlook_ml_shadow" / "model.joblib"


def run_market_outlook_ml_shadow(
    conn,
    config: Mapping[str, Any] | None = None,
    *,
    asof_date: str | date = "latest",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run expanding LGBM quantile validation and save an experimental snapshot."""
    settings = market_outlook_ml_shadow_settings(config)
    target_asof = resolve_market_outlook_asof(conn, asof_date)
    run_id = uuid4().hex
    created_at = _utcnow()
    if not bool(settings["enabled"]):
        return _record_disabled_run(conn, settings, run_id, target_asof, created_at)

    dataset_asof: str | date = "latest" if str(asof_date).strip().lower() == "latest" else target_asof
    dataset = build_market_outlook_dataset(
        conn,
        config=dict(config or {}),
        asof_date=dataset_asof,
        now=now,
        include_feature_provenance=False,
        include_local_llm_features=True,
    )
    dataset = dataset[
        dataset["horizon"].astype(str).str.upper().eq(settings["horizon"])
        & pd.to_datetime(dataset["asof_date"], errors="coerce").dt.date.le(target_asof)
    ].drop_duplicates(["asof_date", "target_date", "horizon", "market"], keep="last").copy()
    if dataset.empty:
        return _record_empty_run(conn, settings, run_id, target_asof, created_at, "market outlook dataset is unavailable")

    all_forecasts: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {}
    latest_rows: list[dict[str, Any]] = []
    for market in MARKETS:
        market_frame = dataset[dataset["market"].astype(str).str.upper().eq(market)].copy()
        result = _walk_forward_market(market_frame, target_asof=target_asof, settings=settings)
        status, reason, baseline_metrics, candidate_metrics = _evaluate_gate(result, settings)
        forecast_rows = _to_storage_rows(
            result["backtest"],
            gate_status=status,
            target_asof=target_asof,
            settings=settings,
            created_at=created_at,
        )
        latest = _latest_forecast_row(
            result,
            market_frame,
            target_asof=target_asof,
            settings=settings,
            gate_status=status,
            created_at=created_at,
        )
        if latest is not None:
            forecast_rows.append(latest)
            latest_rows.append(latest)
        all_forecasts.extend(forecast_rows)
        artifacts[market] = result.get("latest_bundle")
        run_rows.append(
            {
                "run_id": uuid4().hex,
                "model_name": settings["model_name"],
                "model_version": MODEL_VERSION,
                "horizon": settings["horizon"],
                "market": market,
                "signal_days": int(result["signal_days"]),
                "mature_prediction_dates": int(result["mature_dates"]),
                "gate_status": status,
                "production_weight": 0.0,
                "baseline_metrics_json": _json(baseline_metrics),
                "candidate_metrics_json": _json(candidate_metrics),
                "fail_reason": reason,
                "report_path": None,
                "created_at": created_at,
            }
        )

    artifact_path = default_market_outlook_ml_shadow_model_path(dict(config or {}))
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "model_name": settings["model_name"],
        "model_version": MODEL_VERSION,
        "asof_date": target_asof.isoformat(),
        "feature_columns": SHADOW_FEATURE_COLUMNS,
        "settings": settings,
        "models": artifacts,
        "created_at": created_at.isoformat(),
    }
    joblib.dump(artifact, artifact_path)

    forecast_frame = pd.DataFrame(all_forecasts, columns=_forecast_columns())
    if not forecast_frame.empty:
        forecast_frame = forecast_frame.drop_duplicates(
            ["asof_date", "target_date", "horizon", "market", "model_name", "model_version"],
            keep="last",
        )
        append_dedup_table(
            conn,
            "market_outlook_ml_shadow_forecasts",
            forecast_frame,
            ["asof_date", "target_date", "horizon", "market", "model_name", "model_version"],
        )
    run_frame = pd.DataFrame(run_rows)
    append_dedup_table(conn, "market_outlook_ml_shadow_runs", run_frame, ["run_id"])

    provenance_dataset = build_market_outlook_dataset(
        conn,
        config=dict(config or {}),
        asof_date=dataset_asof,
        now=now,
        include_feature_provenance=True,
        include_local_llm_features=False,
    )
    provenance_dataset = provenance_dataset[
        provenance_dataset["horizon"].astype(str).str.upper().eq(settings["horizon"])
    ].drop_duplicates(["asof_date", "target_date", "horizon", "market"], keep="last")
    _persist_baseline_attributions(conn, provenance_dataset, target_asof=target_asof)
    _persist_shadow_attributions(conn, market_frame=dataset, latest_rows=latest_rows, artifacts=artifacts, target_asof=target_asof)

    market_statuses = [str(row["gate_status"]) for row in run_rows]
    overall_status = "accepted_shadow" if market_statuses and all(item == "accepted_shadow" for item in market_statuses) else (
        "rejected" if "rejected" in market_statuses else "not_ready"
    )
    metrics = {row["market"]: _loads(row["candidate_metrics_json"], {}) for row in run_rows}
    register_feature_set(
        conn,
        FEATURE_SET_NAME,
        SHADOW_FEATURE_COLUMNS,
        status="experimental",
        description="Cutoff-safe local Ollama structured news plus domestic/global market features.",
    )
    register_model(
        conn,
        model_name=settings["model_name"],
        model_type="lightgbm_quantile_market_shadow",
        feature_set_name=FEATURE_SET_NAME,
        label_name="next_trading_day_index_return",
        horizons=[settings["horizon"]],
        artifact_path=str(artifact_path),
        metrics=metrics,
        split={"train_start": dataset["asof_date"].min(), "test_end": target_asof},
        status=overall_status,
        production_weight=0.0,
        shadow_mode=True,
        fail_reason="; ".join(str(row["fail_reason"]) for row in run_rows if row["fail_reason"]) or None,
    )
    report_path = _write_report(config, target_asof, run_rows, latest_rows)
    if report_path:
        for row in run_rows:
            conn.execute(
                "UPDATE market_outlook_ml_shadow_runs SET report_path = ? WHERE run_id = ?",
                [str(report_path), row["run_id"]],
            )
    return {
        "status": overall_status,
        "model_name": settings["model_name"],
        "model_version": MODEL_VERSION,
        "asof_date": target_asof.isoformat(),
        "artifact_path": str(artifact_path),
        "markets": [_run_record(row) for row in run_rows],
        "latest_forecasts": [_run_record(row) for row in latest_rows],
        "production_weight": 0.0,
        "report_path": str(report_path) if report_path else None,
    }


def get_market_outlook_ml_shadow(
    conn,
    *,
    date_value: str = "latest",
    market: str = "all",
) -> dict[str, Any]:
    """Read shadow-only records without falling back to production forecasts."""
    if not table_exists(conn, "market_outlook_ml_shadow_forecasts"):
        return _empty_payload("shadow schema is unavailable")
    params: list[Any] = []
    if date_value == "latest":
        row = conn.execute("SELECT MAX(asof_date) FROM market_outlook_ml_shadow_forecasts").fetchone()
        if not row or row[0] is None:
            return _empty_payload("shadow forecasts are not available")
        asof = pd.to_datetime(row[0]).date()
    else:
        asof = pd.to_datetime(date_value, errors="coerce").date()
    where = ["asof_date = ?", "horizon = ?"]
    params.extend([asof, HORIZON])
    if market and market != "all":
        where.append("market = ?")
        params.append(str(market).upper())
    forecasts = conn.execute(
        f"""
        SELECT *
        FROM market_outlook_ml_shadow_forecasts
        WHERE {' AND '.join(where)}
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY market ORDER BY created_at DESC
        ) = 1
        ORDER BY CASE market WHEN 'KOSPI' THEN 1 WHEN 'KOSDAQ' THEN 2 ELSE 3 END
        """,
        params,
    ).fetchdf()
    runs = conn.execute(
        """
        SELECT *
        FROM market_outlook_ml_shadow_runs
        QUALIFY ROW_NUMBER() OVER (PARTITION BY market ORDER BY created_at DESC) = 1
        ORDER BY CASE market WHEN 'KOSPI' THEN 1 WHEN 'KOSDAQ' THEN 2 ELSE 3 END
        """
    ).fetchdf()
    records = [_run_record(row) for row in forecasts.to_dict(orient="records")]
    run_records = []
    for row in runs.to_dict(orient="records"):
        item = _run_record(row)
        item["baseline_metrics"] = _loads(item.pop("baseline_metrics_json", None), {})
        item["candidate_metrics"] = _loads(item.pop("candidate_metrics_json", None), {})
        run_records.append(item)
    return {
        "status": "ready" if records else "not_ready",
        "production_weight": 0.0,
        "asof_date": asof.isoformat() if records else None,
        "model_name": MODEL_NAME,
        "items": records,
        "runs": run_records,
        "disclaimer": "LightGBM+local LLM 결과는 연구용 shadow 검증이며 production 시장 전망을 변경하지 않습니다.",
    }


def _walk_forward_market(frame: pd.DataFrame, *, target_asof: date, settings: Mapping[str, Any]) -> dict[str, Any]:
    data = frame.copy().drop_duplicates(["asof_date", "target_date"], keep="last").sort_values("asof_date").reset_index(drop=True)
    data["asof_date"] = pd.to_datetime(data["asof_date"], errors="coerce").dt.date
    data["label_return"] = pd.to_numeric(data["label_return"], errors="coerce")
    mature = data[data["label_return"].notna() & data["asof_date"].lt(target_asof)].reset_index(drop=True)
    signal_days = int(
        mature.loc[pd.to_numeric(mature.get("llm_news_coverage"), errors="coerce").fillna(0.0).gt(0), "asof_date"].nunique()
    )
    initial = int(settings["initial_train_days"])
    purge = int(settings["purge_days"])
    step = int(settings["retrain_step_days"])
    candidate_residuals: list[float] = []
    baseline_residuals: list[float] = []
    rows: list[dict[str, Any]] = []
    bundle: dict[str, Any] | None = None
    baseline_model: dict[str, Any] | None = None
    start_index = initial + purge
    for index in range(start_index, len(mature)):
        train_end = index - purge
        train = mature.iloc[:train_end].copy()
        test = mature.iloc[index]
        if len(train) < initial:
            continue
        if bundle is None or (index - start_index) % step == 0:
            bundle = _fit_quantile_bundle(train, settings)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                baseline_model = fit_market_outlook_model(train)
        if bundle is None or baseline_model is None:
            continue
        quantiles, _ = _predict_quantiles(bundle, test)
        baseline = float(_predict_row(test.to_dict(), baseline_model)["expected_return"])
        candidate_low, candidate_high, candidate_calibration = _conformal_range(
            quantiles["q50"], candidate_residuals, settings
        )
        baseline_low, baseline_high, baseline_calibration = _conformal_range(
            baseline, baseline_residuals, settings
        )
        actual = float(test["label_return"])
        candidate_residuals.append(actual - quantiles["q50"])
        baseline_residuals.append(actual - baseline)
        rows.append(
            {
                "asof_date": test["asof_date"],
                "target_date": test["target_date"],
                "market": str(test["market"]),
                "baseline_expected_return": baseline,
                "q10": quantiles["q10"],
                "q50": quantiles["q50"],
                "q90": quantiles["q90"],
                "range_low": candidate_low,
                "range_high": candidate_high,
                "baseline_range_low": baseline_low,
                "baseline_range_high": baseline_high,
                "calibration_count": candidate_calibration,
                "baseline_calibration_count": baseline_calibration,
                "actual_return": actual,
                "feature_coverage": _feature_coverage(test),
                "bundle": bundle,
            }
        )

    eligible = mature[mature["asof_date"].lt(target_asof)].copy()
    latest_train = eligible.iloc[:-purge].copy() if purge and len(eligible) > purge else eligible.copy()
    latest_bundle = _fit_quantile_bundle(latest_train, settings) if len(latest_train) >= initial else None
    if len(latest_train) >= initial:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            latest_baseline = fit_market_outlook_model(latest_train)
    else:
        latest_baseline = None
    return {
        "backtest": rows,
        "mature_dates": int(len(rows)),
        "signal_days": signal_days,
        "latest_bundle": latest_bundle,
        "latest_baseline": latest_baseline,
        "candidate_residuals": candidate_residuals,
        "baseline_residuals": baseline_residuals,
    }


def _latest_forecast_row(
    result: Mapping[str, Any],
    frame: pd.DataFrame,
    *,
    target_asof: date,
    settings: Mapping[str, Any],
    gate_status: str,
    created_at: datetime,
) -> dict[str, Any] | None:
    latest = frame[pd.to_datetime(frame["asof_date"], errors="coerce").dt.date.eq(target_asof)].copy()
    if latest.empty or result.get("latest_bundle") is None or result.get("latest_baseline") is None:
        return None
    row = latest.sort_values("target_date").iloc[0]
    quantiles, _ = _predict_quantiles(result["latest_bundle"], row)
    baseline = float(_predict_row(row.to_dict(), result["latest_baseline"])["expected_return"])
    lower, upper, count = _conformal_range(quantiles["q50"], result.get("candidate_residuals") or [], settings)
    return _storage_row(
        asof_date=row["asof_date"],
        target_date=row["target_date"],
        market=str(row["market"]),
        baseline_expected_return=baseline,
        quantiles=quantiles,
        range_low=lower,
        range_high=upper,
        calibration_count=count,
        feature_coverage=_feature_coverage(row),
        gate_status=gate_status,
        settings=settings,
        created_at=created_at,
    )


def _to_storage_rows(
    rows: list[Mapping[str, Any]],
    *,
    gate_status: str,
    target_asof: date,
    settings: Mapping[str, Any],
    created_at: datetime,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        if pd.to_datetime(row["asof_date"]).date() >= target_asof:
            continue
        output.append(
            _storage_row(
                asof_date=row["asof_date"],
                target_date=row["target_date"],
                market=str(row["market"]),
                baseline_expected_return=float(row["baseline_expected_return"]),
                quantiles={"q10": float(row["q10"]), "q50": float(row["q50"]), "q90": float(row["q90"])},
                range_low=row["range_low"],
                range_high=row["range_high"],
                calibration_count=int(row["calibration_count"]),
                feature_coverage=float(row["feature_coverage"]),
                gate_status=gate_status,
                settings=settings,
                created_at=created_at,
            )
        )
    return output


def _storage_row(
    *,
    asof_date: Any,
    target_date: Any,
    market: str,
    baseline_expected_return: float,
    quantiles: Mapping[str, float],
    range_low: float | None,
    range_high: float | None,
    calibration_count: int,
    feature_coverage: float,
    gate_status: str,
    settings: Mapping[str, Any],
    created_at: datetime,
) -> dict[str, Any]:
    q50 = float(quantiles["q50"])
    q10 = float(quantiles["q10"])
    q90 = float(quantiles["q90"])
    spread = max(0.001, (q90 - q10) / 2.563)
    up_probability = float(1.0 - normal_cdf((0.0 - q50) / spread))
    direction = "BULLISH" if q50 >= 0.005 else "BEARISH" if q50 <= -0.005 else "NEUTRAL"
    confidence = _clip(0.20 + 0.45 * float(feature_coverage) + 0.20 * min(1.0, calibration_count / 252.0))
    return {
        "asof_date": pd.to_datetime(asof_date).date(),
        "target_date": pd.to_datetime(target_date).date(),
        "horizon": str(settings["horizon"]),
        "market": market,
        "model_name": str(settings["model_name"]),
        "model_version": MODEL_VERSION,
        "baseline_expected_return": baseline_expected_return,
        "expected_return": q50,
        "q10": q10,
        "q50": q50,
        "q90": q90,
        "range_low": range_low,
        "range_high": range_high,
        "up_probability": _clip(up_probability),
        "direction": direction,
        "confidence": confidence,
        "calibration_count": int(calibration_count),
        "feature_coverage": _clip(float(feature_coverage)),
        "gate_status": gate_status,
        "production_weight": 0.0,
        "created_at": created_at,
    }


def _fit_quantile_bundle(train: pd.DataFrame, settings: Mapping[str, Any]) -> dict[str, Any]:
    features = train.reindex(columns=SHADOW_FEATURE_COLUMNS).apply(pd.to_numeric, errors="coerce")
    label = pd.to_numeric(train["label_return"], errors="coerce")
    valid = label.notna()
    features = features.loc[valid]
    label = label.loc[valid]
    lgbm = dict(settings.get("lgbm") or {})
    params = {
        "n_estimators": int(lgbm.get("n_estimators", 300)),
        "learning_rate": float(lgbm.get("learning_rate", 0.03)),
        "num_leaves": int(lgbm.get("num_leaves", 7)),
        "min_child_samples": int(lgbm.get("min_child_samples", 60)),
        "reg_lambda": float(lgbm.get("reg_lambda", 10.0)),
        "colsample_bytree": float(lgbm.get("feature_fraction", 0.8)),
        "subsample": float(lgbm.get("bagging_fraction", 0.8)),
        "subsample_freq": int(lgbm.get("bagging_freq", 1)),
        "random_state": int(lgbm.get("random_state", 42)),
        "n_jobs": 1,
        "verbosity": -1,
    }
    models: dict[str, LGBMRegressor] = {}
    for name, alpha in (("q10", 0.10), ("q50", 0.50), ("q90", 0.90)):
        model = LGBMRegressor(objective="quantile", alpha=alpha, **params)
        model.fit(features, label)
        models[name] = model
    return {
        "models": models,
        "feature_columns": list(SHADOW_FEATURE_COLUMNS),
        "fill_values": {column: _number(features[column].median(), 0.0) for column in SHADOW_FEATURE_COLUMNS},
        "train_rows": int(len(features)),
    }


def _predict_quantiles(bundle: Mapping[str, Any], row: Mapping[str, Any] | pd.Series) -> tuple[dict[str, float], np.ndarray]:
    values = pd.DataFrame([{column: _number(row.get(column), np.nan) for column in SHADOW_FEATURE_COLUMNS}])
    predicted = {
        name: float(model.predict(values)[0])
        for name, model in dict(bundle["models"]).items()
    }
    sorted_values = sorted(predicted.values())
    quantiles = {"q10": sorted_values[0], "q50": sorted_values[1], "q90": sorted_values[2]}
    model = dict(bundle["models"])["q50"]
    try:
        contribution = np.asarray(model.predict(values, pred_contrib=True)[0], dtype=float)
    except (AttributeError, TypeError, ValueError):
        contribution = np.full(len(SHADOW_FEATURE_COLUMNS) + 1, np.nan)
    return quantiles, contribution


def _conformal_range(
    prediction: float,
    residuals: list[float],
    settings: Mapping[str, Any],
) -> tuple[float | None, float | None, int]:
    calibration = residuals[-int(settings["calibration_window"]) :]
    if len(calibration) < int(settings["min_calibration_rows"]):
        return None, None, len(calibration)
    lower = float(prediction + np.quantile(calibration, 0.10))
    upper = float(prediction + np.quantile(calibration, 0.90))
    return min(lower, upper), max(lower, upper), len(calibration)


def _evaluate_gate(result: Mapping[str, Any], settings: Mapping[str, Any]) -> tuple[str, str | None, dict[str, Any], dict[str, Any]]:
    rows = list(result.get("backtest") or [])
    candidate_metrics, baseline_metrics = _metric_pair(rows)
    if int(result.get("signal_days") or 0) < int(settings["min_signal_days"]):
        return "not_ready", f"requires {settings['min_signal_days']} LLM signal days", baseline_metrics, candidate_metrics
    if int(result.get("mature_dates") or 0) < int(settings["min_mature_dates"]):
        return "not_ready", f"requires {settings['min_mature_dates']} mature prediction dates", baseline_metrics, candidate_metrics
    if candidate_metrics.get("calibrated_count", 0) < int(settings["min_calibration_rows"]):
        return "not_ready", "insufficient calibrated OOS residuals", baseline_metrics, candidate_metrics
    failures: list[str] = []
    if candidate_metrics["q50_mae"] > baseline_metrics["q50_mae"] + 1e-12:
        failures.append("q50 MAE worsened")
    if candidate_metrics["direction_accuracy"] + 1e-12 < baseline_metrics["direction_accuracy"]:
        failures.append("direction accuracy worsened")
    if not (float(settings["coverage_lower_bound"]) <= candidate_metrics["coverage"] <= float(settings["coverage_upper_bound"])):
        failures.append("80% range coverage is outside target band")
    if candidate_metrics["average_width"] > baseline_metrics["average_width"] * float(settings["max_average_width_ratio"]):
        failures.append("average interval width exceeded control")
    if candidate_metrics["low_breach_rate"] > baseline_metrics["low_breach_rate"] + float(settings["max_low_breach_worsening"]):
        failures.append("lower-tail breach worsened")
    return ("rejected", "; ".join(failures), baseline_metrics, candidate_metrics) if failures else (
        "accepted_shadow",
        None,
        baseline_metrics,
        candidate_metrics,
    )


def _metric_pair(rows: list[Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    valid = [
        row
        for row in rows
        if row.get("range_low") is not None
        and row.get("range_high") is not None
        and row.get("baseline_range_low") is not None
        and row.get("baseline_range_high") is not None
    ]
    all_actual = np.asarray([float(row["actual_return"]) for row in rows], dtype=float)
    candidate = np.asarray([float(row["q50"]) for row in rows], dtype=float)
    baseline = np.asarray([float(row["baseline_expected_return"]) for row in rows], dtype=float)
    if not len(rows):
        empty = {
            "q50_mae": math.inf,
            "direction_accuracy": 0.0,
            "coverage": 0.0,
            "average_width": math.inf,
            "low_breach_rate": 1.0,
            "calibrated_count": 0,
        }
        return empty, dict(empty)
    candidate_metrics = _range_metrics(valid, key_low="range_low", key_high="range_high")
    baseline_metrics = _range_metrics(valid, key_low="baseline_range_low", key_high="baseline_range_high")
    candidate_metrics.update(
        {
            "q50_mae": float(np.mean(np.abs(all_actual - candidate))),
            "direction_accuracy": _direction_accuracy(all_actual, candidate),
        }
    )
    baseline_metrics.update(
        {
            "q50_mae": float(np.mean(np.abs(all_actual - baseline))),
            "direction_accuracy": _direction_accuracy(all_actual, baseline),
        }
    )
    return candidate_metrics, baseline_metrics


def _range_metrics(rows: list[Mapping[str, Any]], *, key_low: str, key_high: str) -> dict[str, Any]:
    if not rows:
        return {"coverage": 0.0, "average_width": math.inf, "low_breach_rate": 1.0, "calibrated_count": 0}
    actual = np.asarray([float(row["actual_return"]) for row in rows], dtype=float)
    lower = np.asarray([float(row[key_low]) for row in rows], dtype=float)
    upper = np.asarray([float(row[key_high]) for row in rows], dtype=float)
    return {
        "coverage": float(np.mean((actual >= lower) & (actual <= upper))),
        "average_width": float(np.mean(upper - lower)),
        "low_breach_rate": float(np.mean(actual < lower)),
        "calibrated_count": int(len(rows)),
    }


def _persist_baseline_attributions(conn, dataset: pd.DataFrame, *, target_asof: date) -> None:
    latest = dataset[pd.to_datetime(dataset["asof_date"], errors="coerce").dt.date.eq(target_asof)].copy()
    if latest.empty:
        return
    model = fit_market_outlook_model(dataset)
    rows: list[dict[str, Any]] = []
    now = _utcnow()
    for _, item in latest.iterrows():
        forecast = _predict_row(item.to_dict(), model)
        spec = model.get("models", {}).get(f"{item['market']}|{item['horizon']}", {})
        for component, weight in (("ridge_index", 0.65), ("ridge_breadth", 0.35)):
            component_spec = dict(spec.get("index" if component == "ridge_index" else "breadth", {}))
            rows.extend(_ridge_attribution_rows(item, forecast, component, weight, component_spec, model, now))
    if rows:
        append_dedup_table(
            conn,
            "market_outlook_feature_attributions",
            pd.DataFrame(rows),
            ["asof_date", "target_date", "horizon", "market", "model_version", "component", "feature_name"],
        )


def _ridge_attribution_rows(
    item: pd.Series,
    forecast: Mapping[str, Any],
    component: str,
    blend_weight: float,
    spec: Mapping[str, Any],
    model: Mapping[str, Any],
    created_at: datetime,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    model_version = str(model.get("model_version") or "market-outlook-v2")
    rows.append(
        _attribution_row(
            item,
            model_version=model_version,
            component=component,
            feature_name="__intercept__",
            raw_value=None,
            filled_value=None,
            default_applied=False,
            mean_value=None,
            std_value=None,
            standardized_value=None,
            clipped_value=None,
            coefficient=float(spec.get("intercept") or 0.0),
            blend_weight=blend_weight,
            contribution=float(spec.get("intercept") or 0.0) * blend_weight,
            forecast=float(forecast["expected_return"]),
            reconstructed=float(forecast["expected_return"]),
            source_status="model_intercept",
            created_at=created_at,
        )
    )
    for feature, coefficient in zip(spec.get("features") or [], spec.get("coef") or [], strict=False):
        raw = _number(item.get(f"__raw_{feature}"), np.nan)
        default_applied = not math.isfinite(raw)
        filled = _number(item.get(feature), _number(spec.get("fill", {}).get(feature), 0.0))
        mean = _number(spec.get("mean", {}).get(feature), 0.0)
        std = _number(spec.get("std", {}).get(feature), 1.0)
        std = 1.0 if abs(std) < 1e-12 else std
        standardized = max(-5.0, min(5.0, (filled - mean) / std))
        rows.append(
            _attribution_row(
                item,
                model_version=model_version,
                component=component,
                feature_name=str(feature),
                raw_value=None if default_applied else raw,
                filled_value=filled,
                default_applied=default_applied,
                mean_value=mean,
                std_value=std,
                standardized_value=standardized,
                clipped_value=standardized,
                coefficient=float(coefficient),
                blend_weight=blend_weight,
                contribution=float(coefficient) * standardized * blend_weight,
                forecast=float(forecast["expected_return"]),
                reconstructed=float(forecast["expected_return"]),
                source_status="default_applied" if default_applied else "observed",
                created_at=created_at,
            )
        )
    return rows


def _persist_shadow_attributions(
    conn,
    *,
    market_frame: pd.DataFrame,
    latest_rows: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
    target_asof: date,
) -> None:
    rows: list[dict[str, Any]] = []
    now = _utcnow()
    for forecast in latest_rows:
        market = str(forecast["market"])
        bundle = artifacts.get(market)
        if not bundle:
            continue
        source = market_frame[
            market_frame["market"].astype(str).eq(market)
            & pd.to_datetime(market_frame["asof_date"], errors="coerce").dt.date.eq(target_asof)
        ]
        if source.empty:
            continue
        item = source.iloc[0]
        _, contribution = _predict_quantiles(bundle, item)
        for index, feature in enumerate(SHADOW_FEATURE_COLUMNS):
            raw = _number(item.get(feature), np.nan)
            missing = not math.isfinite(raw)
            filled = _number(dict(bundle.get("fill_values") or {}).get(feature), 0.0) if missing else raw
            source_status = "llm_missing" if feature in LOCAL_LLM_NEWS_FEATURE_COLUMNS and missing else (
                "llm_observed" if feature in LOCAL_LLM_NEWS_FEATURE_COLUMNS else ("missing" if missing else "observed")
            )
            rows.append(
                _attribution_row(
                    item,
                    model_version=MODEL_VERSION,
                    component="lgbm_q50_shadow",
                    feature_name=feature,
                    raw_value=None if missing else raw,
                    filled_value=filled,
                    default_applied=missing,
                    mean_value=None,
                    std_value=None,
                    standardized_value=None,
                    clipped_value=None,
                    coefficient=None,
                    blend_weight=1.0,
                    contribution=_number(contribution[index] if index < len(contribution) else np.nan, np.nan),
                    forecast=float(forecast["expected_return"]),
                    reconstructed=float(np.nansum(contribution)),
                    source_status=source_status,
                    created_at=now,
                )
            )
        rows.append(
            _attribution_row(
                item,
                model_version=MODEL_VERSION,
                component="lgbm_q50_shadow",
                feature_name="__bias__",
                raw_value=None,
                filled_value=None,
                default_applied=False,
                mean_value=None,
                std_value=None,
                standardized_value=None,
                clipped_value=None,
                coefficient=None,
                blend_weight=1.0,
                contribution=_number(contribution[-1] if len(contribution) else np.nan, np.nan),
                forecast=float(forecast["expected_return"]),
                reconstructed=float(np.nansum(contribution)),
                source_status="model_bias",
                created_at=now,
            )
        )
    if rows:
        append_dedup_table(
            conn,
            "market_outlook_feature_attributions",
            pd.DataFrame(rows),
            ["asof_date", "target_date", "horizon", "market", "model_version", "component", "feature_name"],
        )


def _attribution_row(
    item: pd.Series,
    *,
    model_version: str,
    component: str,
    feature_name: str,
    raw_value: float | None,
    filled_value: float | None,
    default_applied: bool,
    mean_value: float | None,
    std_value: float | None,
    standardized_value: float | None,
    clipped_value: float | None,
    coefficient: float | None,
    blend_weight: float,
    contribution: float,
    forecast: float,
    reconstructed: float,
    source_status: str,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "asof_date": pd.to_datetime(item["asof_date"]).date(),
        "target_date": pd.to_datetime(item["target_date"]).date(),
        "horizon": str(item["horizon"]),
        "market": str(item["market"]),
        "model_version": model_version,
        "component": component,
        "feature_name": feature_name,
        "attribution_status": "default" if default_applied else "ready",
        "raw_value": raw_value,
        "filled_value": filled_value,
        "default_applied": bool(default_applied),
        "mean_value": mean_value,
        "std_value": std_value,
        "standardized_value": standardized_value,
        "clipped_value": clipped_value,
        "coefficient": coefficient,
        "blend_weight": blend_weight,
        "contribution": contribution,
        "forecast_expected_return": forecast,
        "reconstructed_expected_return": reconstructed,
        "source_status": source_status,
        "created_at": created_at,
    }


def _feature_coverage(row: Mapping[str, Any] | pd.Series) -> float:
    values = [_number(row.get(column), np.nan) for column in LOCAL_LLM_NEWS_FEATURE_COLUMNS]
    return float(sum(math.isfinite(value) for value in values) / len(values)) if values else 0.0


def _record_disabled_run(conn, settings: Mapping[str, Any], run_id: str, asof: date, created_at: datetime) -> dict[str, Any]:
    return _record_empty_run(conn, settings, run_id, asof, created_at, "market_outlook_ml_shadow.enabled=false", status="disabled")


def _record_empty_run(
    conn,
    settings: Mapping[str, Any],
    run_id: str,
    asof: date,
    created_at: datetime,
    reason: str,
    *,
    status: str = "not_ready",
) -> dict[str, Any]:
    rows = [
        {
            "run_id": f"{run_id}-{market}",
            "model_name": settings["model_name"],
            "model_version": MODEL_VERSION,
            "horizon": settings["horizon"],
            "market": market,
            "signal_days": 0,
            "mature_prediction_dates": 0,
            "gate_status": status,
            "production_weight": 0.0,
            "baseline_metrics_json": _json({}),
            "candidate_metrics_json": _json({}),
            "fail_reason": reason,
            "report_path": None,
            "created_at": created_at,
        }
        for market in MARKETS
    ]
    append_dedup_table(conn, "market_outlook_ml_shadow_runs", pd.DataFrame(rows), ["run_id"])
    return {
        "status": status,
        "model_name": settings["model_name"],
        "asof_date": asof.isoformat(),
        "markets": [_run_record(row) for row in rows],
        "production_weight": 0.0,
    }


def _write_report(
    config: Mapping[str, Any] | None,
    asof: date,
    runs: list[Mapping[str, Any]],
    forecasts: list[Mapping[str, Any]],
) -> Path | None:
    raw_path = ((config or {}).get("paths") or {}).get("report_dir")
    if not raw_path:
        return None
    target = Path(str(raw_path)) / f"market_outlook_ml_shadow_{asof.isoformat()}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"asof_date": asof.isoformat(), "runs": [_run_record(row) for row in runs], "forecasts": [_run_record(row) for row in forecasts]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def _forecast_columns() -> list[str]:
    return [
        "asof_date",
        "target_date",
        "horizon",
        "market",
        "model_name",
        "model_version",
        "baseline_expected_return",
        "expected_return",
        "q10",
        "q50",
        "q90",
        "range_low",
        "range_high",
        "up_probability",
        "direction",
        "confidence",
        "calibration_count",
        "feature_coverage",
        "gate_status",
        "production_weight",
        "created_at",
    ]


def _empty_payload(reason: str) -> dict[str, Any]:
    return {
        "status": "not_ready",
        "production_weight": 0.0,
        "asof_date": None,
        "model_name": MODEL_NAME,
        "items": [],
        "runs": [],
        "disclaimer": reason,
    }


def _direction_accuracy(actual: np.ndarray, predicted: np.ndarray) -> float:
    if not len(actual):
        return 0.0
    return float(np.mean(np.sign(actual) == np.sign(predicted)))


def _number(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _clip(value: float) -> float:
    return float(max(0.0, min(1.0, value)))


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _run_record(value: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, pd.Timestamp):
            output[key] = item.isoformat()
        elif hasattr(item, "isoformat"):
            output[key] = item.isoformat()
        elif isinstance(item, float) and (math.isnan(item) or math.isinf(item)):
            output[key] = None
        else:
            output[key] = item
    return output


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(output.get(key), Mapping):
            output[key] = _deep_merge(dict(output[key]), value)
        else:
            output[key] = value
    return output
