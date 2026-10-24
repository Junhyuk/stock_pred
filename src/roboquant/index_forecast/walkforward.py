"""Expanding walk-forward comparison for 1D/5D index return and 80% range."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import ElasticNet, LogisticRegression, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from roboquant.index_forecast.dataset import (
    FEATURE_VERSION,
    active_feature_groups,
    build_index_forecast_dataset,
)
from roboquant.index_forecast.indexes import horizon_label
from roboquant.index_forecast.sqlite_store import (
    PRODUCTION_WEIGHT,
    insert_immutable,
    json_dumps,
    utcnow_text,
)

TARGET_COVERAGE = 0.80
COVERAGE_LOW = 0.75
COVERAGE_HIGH = 0.85
MODEL_TYPES = ("naive", "previous_direction", "ridge", "elasticnet", "hgb", "lightgbm")
HORIZON = "1D"


def run_index_forecast_walkforward(
    sqlite_conn,
    *,
    index_code: str = "KOSPI",
    horizon_days: int = 1,
    initial_train_days: int = 252,
    purge_days: int | None = None,
    retrain_step_days: int = 21,
    calibration_window: int = 60,
    min_oos_rows: int = 60,
    eval_years: int | None = None,
    eval_start_date: date | None = None,
    persist: bool = True,
    model_types: tuple[str, ...] | None = None,
    include_live_forecast: bool = False,
) -> dict[str, Any]:
    horizon = horizon_label(horizon_days)
    embargo = int(horizon_days) if purge_days is None else int(purge_days)
    frame = build_index_forecast_dataset(
        sqlite_conn,
        index_code=index_code,
        horizon_days=horizon_days,
    )
    if frame.empty:
        return {
            "status": "not_ready",
            "reason": f"{index_code}_history_missing",
            "index_code": index_code,
            "horizon": horizon,
            "horizon_days": int(horizon_days),
            "production_weight": PRODUCTION_WEIGHT,
            "groups": {},
            "winner": {"status": "not_ready", "reason": f"{index_code}_history_missing", "model_type": None},
            "live_forecast": None,
        }
    groups = active_feature_groups(frame)
    selected_models = tuple(model_types) if model_types else MODEL_TYPES
    eval_start = eval_start_date or _eval_start(frame, eval_years=eval_years)
    group_reports: dict[str, Any] = {}
    for group_name, columns in groups.items():
        group_reports[group_name] = _run_group(
            sqlite_conn,
            frame,
            index_code=index_code,
            horizon=horizon,
            group_name=group_name,
            feature_columns=columns,
            initial_train_days=initial_train_days,
            purge_days=embargo,
            retrain_step_days=retrain_step_days,
            calibration_window=calibration_window,
            persist=persist,
            model_types=selected_models,
            eval_start=eval_start,
        )
    winner = _select_winner(group_reports, min_oos_rows=min_oos_rows)
    live = None
    if include_live_forecast and winner.get("model_type") and winner.get("group"):
        live = forecast_latest(
            sqlite_conn,
            index_code=index_code,
            horizon_days=horizon_days,
            model_type=str(winner["model_type"]),
            feature_columns=list(group_reports[str(winner["group"])]["feature_columns"]),
            calibration_window=calibration_window,
            persist=persist,
            group_name=str(winner["group"]),
        )
    return {
        "status": winner["status"],
        "reason": winner["reason"],
        "index_code": index_code,
        "horizon": horizon,
        "horizon_days": int(horizon_days),
        "eval_start": eval_start.isoformat() if eval_start else None,
        "production_weight": PRODUCTION_WEIGHT,
        "feature_version": FEATURE_VERSION,
        "rows": int(len(frame)),
        "groups": group_reports,
        "winner": winner,
        "live_forecast": live,
    }


def _run_group(
    sqlite_conn,
    frame: pd.DataFrame,
    *,
    index_code: str,
    horizon: str,
    group_name: str,
    feature_columns: list[str],
    initial_train_days: int,
    purge_days: int,
    retrain_step_days: int,
    calibration_window: int,
    persist: bool,
    model_types: tuple[str, ...],
    eval_start: date | None,
) -> dict[str, Any]:
    dates = sorted(pd.to_datetime(frame["asof_date"], errors="coerce").dt.date.dropna().unique())
    if len(dates) <= initial_train_days + purge_days + 5:
        return {
            "status": "not_ready",
            "reason": "insufficient_history",
            "models": {},
            "feature_columns": feature_columns,
        }
    models: dict[str, Any] = {}
    for model_type in model_types:
        estimator = _make_estimator(model_type)
        if estimator is None and model_type not in {"naive", "previous_direction"}:
            continue
        predictions = _expanding_predict(
            frame,
            feature_columns=feature_columns,
            model_type=model_type,
            dates=dates,
            initial_train_days=initial_train_days,
            purge_days=purge_days,
            retrain_step_days=retrain_step_days,
            calibration_window=calibration_window,
            eval_start=eval_start,
        )
        metrics = _score(predictions)
        model_version = f"index-{index_code.lower()}-{model_type}-{group_name}-{horizon.lower()}-v1"
        if persist and not predictions.empty:
            _persist_run(
                sqlite_conn,
                predictions,
                index_code=index_code,
                horizon=horizon,
                model_type=model_type,
                model_version=model_version,
                group_name=group_name,
                feature_columns=feature_columns,
                metrics=metrics,
            )
        models[model_type] = {
            "model_version": model_version,
            "metrics": metrics,
            "n": int(len(predictions)),
            "status": "ready" if not predictions.empty else "not_ready",
            "recent": _recent_comparison(predictions),
        }
    return {
        "status": "ready",
        "feature_columns": feature_columns,
        "models": models,
    }


def _expanding_predict(
    frame: pd.DataFrame,
    *,
    feature_columns: list[str],
    model_type: str,
    dates: list[date],
    initial_train_days: int,
    purge_days: int,
    retrain_step_days: int,
    calibration_window: int,
    eval_start: date | None = None,
) -> pd.DataFrame:
    indexed = frame.copy()
    indexed["asof_date"] = pd.to_datetime(indexed["asof_date"], errors="coerce").dt.date
    indexed["target_date"] = pd.to_datetime(indexed["target_date"], errors="coerce").dt.date
    indexed = indexed.set_index("asof_date", drop=False)
    rows: list[dict[str, Any]] = []
    fitted = None
    last_fit_at: date | None = None
    for position, asof in enumerate(dates):
        if position < initial_train_days:
            continue
        train_end_pos = position - purge_days
        if train_end_pos < initial_train_days:
            continue
        train_dates = dates[:train_end_pos]
        if not train_dates:
            continue
        need_fit = last_fit_at is None or (position - dates.index(last_fit_at) >= retrain_step_days)
        if need_fit:
            train = indexed.loc[indexed["asof_date"].isin(train_dates)].copy()
            fitted = _fit(model_type, train, feature_columns)
            last_fit_at = asof
        if eval_start is not None and asof < eval_start:
            continue
        if fitted is None or asof not in indexed.index:
            continue
        current = indexed.loc[[asof]].copy()
        expected, probabilities = _predict_row(model_type, fitted, current, feature_columns)
        residual_window = indexed.loc[indexed["asof_date"].isin(train_dates[-calibration_window:])]
        low, high, vol = _conformal_range(residual_window, fitted, feature_columns, model_type, expected)
        actual = float(current["label_return"].iloc[0]) if pd.notna(current["label_return"].iloc[0]) else None
        rows.append(
            {
                "index_code": current["index_code"].iloc[0],
                "asof_date": asof,
                "prediction_time": asof.isoformat(),
                "target_date": current["target_date"].iloc[0],
                "expected_return": expected,
                "range_low": low,
                "range_high": high,
                "expected_volatility": vol,
                "p_up": probabilities.get("p_up"),
                "p_flat": probabilities.get("p_flat"),
                "p_down": probabilities.get("p_down"),
                "actual_return": actual,
                "feature_payload": {
                    column: _json_number(current[column].iloc[0]) if column in current.columns else None
                    for column in feature_columns
                },
            }
        )
    return pd.DataFrame(rows)


def _fit(model_type: str, train: pd.DataFrame, feature_columns: list[str]) -> dict[str, Any]:
    y = pd.to_numeric(train["label_return"], errors="coerce")
    valid = y.notna()
    y = y.loc[valid]
    mean = float(y.mean()) if not y.empty else 0.0
    last_return = float(pd.to_numeric(train["return_1d"], errors="coerce").dropna().iloc[-1]) if not train.empty else 0.0
    payload: dict[str, Any] = {
        "model_type": model_type,
        "mean": mean,
        "last_return": last_return,
        "estimator": None,
        "classifier": None,
    }
    if model_type in {"naive", "previous_direction"} or y.empty:
        return payload
    x = _matrix(train.loc[valid], feature_columns)
    estimator = _make_estimator(model_type)
    if estimator is None:
        return payload
    estimator.fit(x, y.to_numpy(dtype=float))
    payload["estimator"] = estimator
    direction = np.sign(y.to_numpy(dtype=float))
    if np.unique(direction).size >= 2:
        classifier = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("model", LogisticRegression(max_iter=200, solver="lbfgs")),
            ]
        )
        classifier.fit(x, (direction > 0).astype(int))
        payload["classifier"] = classifier
    return payload


def _predict_row(
    model_type: str,
    fitted: dict[str, Any],
    row: pd.DataFrame,
    feature_columns: list[str],
) -> tuple[float, dict[str, float | None]]:
    if model_type == "naive":
        expected = float(fitted["mean"])
    elif model_type == "previous_direction":
        expected = float(pd.to_numeric(row["return_1d"], errors="coerce").iloc[0] or 0.0)
    else:
        estimator = fitted.get("estimator")
        if estimator is None:
            expected = float(fitted["mean"])
        else:
            expected = float(estimator.predict(_matrix(row, feature_columns))[0])
    probabilities: dict[str, float | None] = {"p_up": None, "p_flat": 0.0, "p_down": None}
    classifier = fitted.get("classifier")
    if classifier is not None:
        proba = classifier.predict_proba(_matrix(row, feature_columns))[0]
        classes = list(classifier.named_steps["model"].classes_)
        p_up = float(proba[classes.index(1)]) if 1 in classes else 0.0
        probabilities["p_up"] = p_up
        probabilities["p_down"] = 1.0 - p_up
    else:
        probabilities["p_up"] = None
        probabilities["p_down"] = None
        probabilities["p_flat"] = None
    return expected, probabilities


def _conformal_range(
    calibration: pd.DataFrame,
    fitted: dict[str, Any],
    feature_columns: list[str],
    model_type: str,
    expected: float,
) -> tuple[float, float, float]:
    if calibration.empty:
        width = 0.02
        return expected - width, expected + width, width
    actual = pd.to_numeric(calibration["label_return"], errors="coerce")
    if model_type == "naive":
        predicted = np.full(len(calibration), float(fitted["mean"]))
    elif model_type == "previous_direction":
        predicted = pd.to_numeric(calibration["return_1d"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    else:
        estimator = fitted.get("estimator")
        if estimator is None:
            predicted = np.full(len(calibration), float(fitted["mean"]))
        else:
            predicted = np.asarray(estimator.predict(_matrix(calibration, feature_columns)), dtype=float)
    residuals = (actual.to_numpy(dtype=float) - predicted)
    residuals = residuals[np.isfinite(residuals)]
    if residuals.size < 5:
        width = 0.02
        return expected - width, expected + width, width
    width = float(np.quantile(np.abs(residuals), TARGET_COVERAGE))
    width = max(0.004, min(0.08, width))
    return expected - width, expected + width, width


def _make_estimator(model_type: str):
    if model_type == "ridge":
        return Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("model", Ridge(alpha=10.0)),
            ]
        )
    if model_type == "elasticnet":
        return Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("model", ElasticNet(alpha=0.001, l1_ratio=0.2, max_iter=4000)),
            ]
        )
    if model_type == "hgb":
        from sklearn.ensemble import HistGradientBoostingRegressor

        return Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                (
                    "model",
                    HistGradientBoostingRegressor(max_depth=3, max_iter=80, learning_rate=0.06, random_state=7),
                ),
            ]
        )
    if model_type == "lightgbm":
        try:
            from lightgbm import LGBMRegressor
        except Exception:
            return None
        return Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                (
                    "model",
                    LGBMRegressor(
                        n_estimators=80,
                        max_depth=3,
                        learning_rate=0.05,
                        subsample=0.8,
                        colsample_bytree=0.8,
                        random_state=7,
                        verbose=-1,
                    ),
                ),
            ]
        )
    return None


def _matrix(frame: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
    matrix = frame.reindex(columns=feature_columns)
    return matrix.apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)


def _score(predictions: pd.DataFrame) -> dict[str, Any]:
    if predictions.empty:
        return {"n": 0}
    actual = pd.to_numeric(predictions["actual_return"], errors="coerce")
    expected = pd.to_numeric(predictions["expected_return"], errors="coerce")
    low = pd.to_numeric(predictions["range_low"], errors="coerce")
    high = pd.to_numeric(predictions["range_high"], errors="coerce")
    valid = actual.notna() & expected.notna()
    n = int(valid.sum())
    if n == 0:
        return {"n": 0}
    error = (actual[valid] - expected[valid]).abs()
    covered = (actual[valid] >= low[valid]) & (actual[valid] <= high[valid])
    width = (high[valid] - low[valid]).abs()
    direction = np.sign(actual[valid].to_numpy(dtype=float))
    pred_dir = np.sign(expected[valid].to_numpy(dtype=float))
    usable = (direction != 0) & (pred_dir != 0)
    hit = float((direction[usable] == pred_dir[usable]).mean()) if usable.any() else None
    spearman = None
    if n >= 5:
        value = pd.Series(expected[valid].to_numpy()).corr(pd.Series(actual[valid].to_numpy()), method="spearman")
        spearman = None if pd.isna(value) else float(value)
    return {
        "n": n,
        "mae": float(error.mean()),
        "rmse": float(np.sqrt((error**2).mean())),
        "direction_hit": hit,
        "spearman": spearman,
        "coverage": float(covered.mean()),
        "average_width": float(width.mean()),
    }


def _select_winner(group_reports: dict[str, Any], *, min_oos_rows: int) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    naive_mae: dict[str, float] = {}
    for group_name, report in group_reports.items():
        models = report.get("models") or {}
        naive = (models.get("naive") or {}).get("metrics") or {}
        if naive.get("mae") is not None:
            naive_mae[group_name] = float(naive["mae"])
        for model_type, payload in models.items():
            metrics = payload.get("metrics") or {}
            if int(metrics.get("n") or 0) < min_oos_rows:
                continue
            if metrics.get("mae") is None:
                continue
            candidates.append(
                {
                    "group": group_name,
                    "model_type": model_type,
                    "model_version": payload.get("model_version"),
                    "metrics": metrics,
                }
            )
    if not candidates:
        return {
            "status": "not_ready",
            "reason": "insufficient_oos_rows",
            "model_type": None,
            "group": None,
            "production_weight": PRODUCTION_WEIGHT,
        }
    ranked = sorted(
        candidates,
        key=lambda item: (
            -float(item["metrics"]["direction_hit"]) if item["metrics"].get("direction_hit") is not None else 1.0,
            float(item["metrics"]["mae"]),
        ),
    )
    best = ranked[0]
    baseline = naive_mae.get(str(best["group"]))
    coverage = best["metrics"].get("coverage")
    coverage_ok = coverage is not None and COVERAGE_LOW <= float(coverage) <= COVERAGE_HIGH
    beats_naive = baseline is None or float(best["metrics"]["mae"]) < float(baseline)
    recent = ((group_reports.get(str(best["group"])) or {}).get("models") or {}).get(best["model_type"], {}).get(
        "recent"
    )
    payload = {
        "model_type": best["model_type"],
        "group": best["group"],
        "model_version": best["model_version"],
        "metrics": best["metrics"],
        "recent": recent or [],
        "production_weight": PRODUCTION_WEIGHT,
    }
    if best["model_type"] != "naive" and beats_naive and coverage_ok:
        payload.update({"status": "accepted_shadow", "reason": "beats_naive_mae_with_range_coverage"})
        return payload
    payload.update({"status": "rejected", "reason": "no_model_beats_naive_with_coverage_gate"})
    return payload


def _persist_run(
    sqlite_conn,
    predictions: pd.DataFrame,
    *,
    index_code: str,
    horizon: str,
    model_type: str,
    model_version: str,
    group_name: str,
    feature_columns: list[str],
    metrics: dict[str, Any],
) -> None:
    created_at = utcnow_text()
    registry = pd.DataFrame(
        [
            {
                "model_version": model_version,
                "model_type": model_type,
                "horizon": horizon,
                "trained_from": str(predictions["asof_date"].min()),
                "trained_to": str(predictions["asof_date"].max()),
                "feature_version": f"{FEATURE_VERSION}:{group_name}",
                "metrics_json": json_dumps(
                    {
                        "group": group_name,
                        "feature_columns": feature_columns,
                        "metrics": metrics,
                        "production_weight": PRODUCTION_WEIGHT,
                    }
                ),
                "artifact_path": None,
                "created_at": created_at,
            }
        ]
    )
    insert_immutable(sqlite_conn, "model_registry", registry, ["model_version"])
    pred_rows = []
    feature_rows = []
    outcome_rows = []
    for item in predictions.itertuples(index=False):
        pred_rows.append(
            {
                "index_code": index_code,
                "prediction_time": str(item.prediction_time),
                "horizon": horizon,
                "model_version": model_version,
                "p_up": item.p_up,
                "p_flat": item.p_flat,
                "p_down": item.p_down,
                "expected_return": item.expected_return,
                "expected_volatility": item.expected_volatility,
                "range_low": item.range_low,
                "range_high": item.range_high,
                "signal": _signal(item.expected_return, item.p_up),
                "confidence": item.p_up if item.expected_return and item.expected_return >= 0 else item.p_down,
                "data_quality": 1.0,
                "production_weight": PRODUCTION_WEIGHT,
                "reason_codes_json": json_dumps({"group": group_name, "range": "residual_conformal_80"}),
            }
        )
        feature_rows.append(
            {
                "index_code": index_code,
                "prediction_time": str(item.prediction_time),
                "feature_version": f"{FEATURE_VERSION}:{group_name}",
                "features_json": json_dumps(item.feature_payload),
            }
        )
        if item.actual_return is not None and not (isinstance(item.actual_return, float) and math.isnan(item.actual_return)):
            outcome_rows.append(
                {
                    "index_code": index_code,
                    "prediction_time": str(item.prediction_time),
                    "horizon": horizon,
                    "realized_return": item.actual_return,
                    "realized_direction": _direction(item.actual_return),
                    "evaluated_at": created_at,
                }
            )
    insert_immutable(
        sqlite_conn,
        "prediction",
        pd.DataFrame(pred_rows),
        ["index_code", "prediction_time", "horizon", "model_version"],
    )
    insert_immutable(
        sqlite_conn,
        "feature_snapshot",
        pd.DataFrame(feature_rows),
        ["index_code", "prediction_time", "feature_version"],
    )
    if outcome_rows:
        insert_immutable(
            sqlite_conn,
            "realized_outcome",
            pd.DataFrame(outcome_rows),
            ["index_code", "prediction_time", "horizon"],
        )


def _signal(expected: float | None, p_up: float | None) -> str:
    if expected is None or (isinstance(expected, float) and not math.isfinite(expected)):
        return "NO_TRADE"
    if abs(expected) < 0.001:
        return "NEUTRAL"
    return "LONG" if expected > 0 else "SHORT"


def _direction(value: float) -> str:
    if value > 0:
        return "UP"
    if value < 0:
        return "DOWN"
    return "FLAT"


def _json_number(value: Any) -> float | None:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(number):
        return None
    return float(number)


def _eval_start(frame: pd.DataFrame, *, eval_years: int | None) -> date | None:
    if not eval_years:
        return None
    asof = pd.to_datetime(frame["asof_date"], errors="coerce").dt.date.dropna()
    if asof.empty:
        return None
    last = max(asof.tolist())
    return last - timedelta(days=365 * int(eval_years))


def _recent_comparison(predictions: pd.DataFrame, limit: int = 12) -> list[dict[str, Any]]:
    if predictions.empty:
        return []
    scored = predictions.dropna(subset=["actual_return"]).tail(limit)
    rows: list[dict[str, Any]] = []
    for item in scored.itertuples(index=False):
        actual = float(item.actual_return)
        expected = float(item.expected_return)
        rows.append(
            {
                "asof_date": str(item.asof_date),
                "target_date": str(item.target_date),
                "expected_return": expected,
                "actual_return": actual,
                "range_low": float(item.range_low) if item.range_low is not None else None,
                "range_high": float(item.range_high) if item.range_high is not None else None,
                "direction_hit": bool(np.sign(actual) == np.sign(expected) and actual != 0 and expected != 0),
            }
        )
    return rows


def forecast_latest(
    sqlite_conn,
    *,
    index_code: str,
    horizon_days: int,
    model_type: str,
    feature_columns: list[str],
    calibration_window: int = 60,
    persist: bool = False,
    group_name: str = "price",
) -> dict[str, Any]:
    horizon = horizon_label(horizon_days)
    frame = build_index_forecast_dataset(
        sqlite_conn,
        index_code=index_code,
        horizon_days=horizon_days,
        include_unlabeled=True,
    )
    if frame.empty:
        return {
            "status": "not_ready",
            "reason": f"{index_code}_history_missing",
            "index_code": index_code,
            "horizon": horizon,
            "production_weight": PRODUCTION_WEIGHT,
        }
    labeled = frame.dropna(subset=["label_return"])
    unlabeled = frame[frame["label_return"].isna()]
    if unlabeled.empty or labeled.empty:
        return {
            "status": "not_ready",
            "reason": "no_unlabeled_asof_for_live_forecast",
            "index_code": index_code,
            "horizon": horizon,
            "production_weight": PRODUCTION_WEIGHT,
        }
    current = unlabeled.iloc[[-1]].copy()
    fitted = _fit(model_type, labeled, feature_columns)
    expected, probabilities = _predict_row(model_type, fitted, current, feature_columns)
    residual_window = labeled.tail(calibration_window)
    low, high, vol = _conformal_range(residual_window, fitted, feature_columns, model_type, expected)
    asof = current["asof_date"].iloc[0]
    last_close = _json_number(current["close"].iloc[0]) if "close" in current.columns else None
    payload = {
        "status": "ready",
        "reason": None,
        "index_code": index_code,
        "horizon": horizon,
        "horizon_days": int(horizon_days),
        "asof_date": str(asof),
        "target": "다음 1 거래일" if horizon_days == 1 else "다음 5 거래일",
        "last_close": last_close,
        "model_type": model_type,
        "group": group_name,
        "model_version": f"index-{index_code.lower()}-{model_type}-{group_name}-{horizon.lower()}-v1",
        "expected_return": expected,
        "range_low": low,
        "range_high": high,
        "expected_volatility": vol,
        "p_up": probabilities.get("p_up"),
        "p_flat": probabilities.get("p_flat"),
        "p_down": probabilities.get("p_down"),
        "signal": _signal(expected, probabilities.get("p_up")),
        "production_weight": PRODUCTION_WEIGHT,
        "disclaimer": "연구용 확률·범위입니다. 상승 확정이나 매수 지시가 아닙니다.",
    }
    if persist:
        _persist_run(
            sqlite_conn,
            pd.DataFrame(
                [
                    {
                        "index_code": index_code,
                        "asof_date": asof,
                        "prediction_time": str(asof),
                        "target_date": None,
                        "expected_return": expected,
                        "range_low": low,
                        "range_high": high,
                        "expected_volatility": vol,
                        "p_up": probabilities.get("p_up"),
                        "p_flat": probabilities.get("p_flat"),
                        "p_down": probabilities.get("p_down"),
                        "actual_return": None,
                        "feature_payload": {
                            column: _json_number(current[column].iloc[0]) if column in current.columns else None
                            for column in feature_columns
                        },
                    }
                ]
            ),
            index_code=index_code,
            horizon=horizon,
            model_type=model_type,
            model_version=str(payload["model_version"]),
            group_name=group_name,
            feature_columns=feature_columns,
            metrics={"n": 0, "live": True},
        )
    return payload

