"""Chronological binary calibration and research gates for one-session index returns.

All calibration residuals are genuinely out of sample. The terminal block evaluates
one choice frozen using the preceding development block; it never chooses a model.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from roboquant.index_forecast.dataset import PRICE_FEATURES
from roboquant.index_forecast.fit_protocol import (
    artifact_path,
    content_hash,
    make_fit_snapshot,
    make_model_artifact,
    predict_model_artifact,
    restore_fit_snapshot,
    save_model_artifact,
    session_anchor,
    validate_model_artifact,
)
from roboquant.index_forecast.reliable_intervals import (
    interval_score80,
    normalized_interval_shadows,
)


@dataclass(frozen=True)
class ReliableSettings:
    initial_train_days: int = 504
    purge_days: int = 1
    retrain_step_days: int = 21
    calibration_window: int = 252
    min_calibration_rows: int = 60
    development_days: int = 252
    test_days: int = 252
    bootstrap_samples: int = 2000
    bootstrap_block_days: int = 20
    random_state: int = 42
    min_oos_rows: int = 252
    model_families: tuple[str, ...] = ("linear", "lightgbm", "ensemble")
    calendar_name: str = "XKRX"
    refit_origin: str = "2019-01-02"

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if name not in {"model_families", "random_state", "purge_days", "calendar_name", "refit_origin"} and value < 1:
                raise ValueError(f"{name} must be positive")
        if self.purge_days < 0:
            raise ValueError("purge_days must be nonnegative")
        if any(f not in {"linear", "lightgbm", "ensemble"} for f in self.model_families):
            raise ValueError("unsupported model family")
        if len(set(self.model_families)) != len(self.model_families):
            raise ValueError("duplicate model families would duplicate OOF observations")


def _timestamp(value: Any, *, session_close: bool = True) -> pd.Timestamp:
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return pd.NaT
    if pd.isna(stamp):
        return pd.NaT
    if stamp.tzinfo is None:
        if session_close and stamp == stamp.normalize():
            stamp = stamp.replace(hour=15, minute=30)
        stamp = stamp.tz_localize("Asia/Seoul")
    return stamp.tz_convert("UTC")


def _prepare(frame: pd.DataFrame, *, keep_unlabeled: bool = False) -> pd.DataFrame:
    data = frame.copy(deep=True)
    for column in ("asof_date", "target_date", "label_return"):
        if column not in data:
            return pd.DataFrame()
    data["_asof_label"] = pd.to_datetime(data["asof_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    data["_target_label"] = pd.to_datetime(data["target_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    data["_asof"] = (data["asof_close_at"].map(lambda x: _timestamp(x, session_close=False))
                      if "asof_close_at" in data else data["asof_date"].map(_timestamp))
    data["_cutoff"] = (data["prediction_cutoff"].map(lambda x: _timestamp(x, session_close=False))
                       if "prediction_cutoff" in data else data["asof_date"].map(_timestamp))
    data["_available"] = (data["label_available_at"].map(lambda x: _timestamp(x, session_close=False))
                          if "label_available_at" in data else data["target_date"].map(_timestamp))
    data["_target"] = (data["target_close_at"].map(lambda x: _timestamp(x, session_close=False))
                        if "target_close_at" in data else data["target_date"].map(_timestamp))
    data["label_return"] = pd.to_numeric(data["label_return"], errors="coerce")
    data = data.replace({"label_return": {np.inf: np.nan, -np.inf: np.nan}})
    required = ["_asof", "_cutoff", "_target"] + ([] if keep_unlabeled else ["label_return"])
    if keep_unlabeled:
        # Conflicting clocks cannot define a historical scheduled-fit cutoff.
        counts = data.groupby("_asof_label")["_cutoff"].transform("nunique")
        data = data.loc[counts <= 1]
    return data.dropna(subset=required).sort_values(
        "_asof", kind="stable").drop_duplicates("_asof_label" if keep_unlabeled else "_asof").reset_index(drop=True)


def _matrix(data: pd.DataFrame, features: list[str]) -> np.ndarray:
    return data[features].apply(pd.to_numeric, errors="coerce").replace(
        [np.inf, -np.inf], np.nan).to_numpy(dtype=float)


def _current_features_available(row: pd.DataFrame, cutoff: pd.Timestamp) -> bool:
    if "feature_available_at" not in row:
        # Historical close reconstruction remains explicitly unverified.
        return True
    receipt = _timestamp(row["feature_available_at"].iloc[0], session_close=False)
    return bool(pd.notna(receipt) and receipt <= cutoff)


def _model_parameters(family: str, config: ReliableSettings) -> dict:
    linear = {"return": {"estimator": "Ridge", "alpha": 10.0},
              "probability": {"estimator": "LogisticRegression", "C": 1.0, "max_iter": 300},
              "preprocessing": ["median_imputer", "standard_scaler"]}
    tree = {"num_leaves": 7, "min_child_samples": 80, "n_estimators": 250,
            "learning_rate": .03, "reg_lambda": 10.0, "colsample_bytree": .7,
            "random_state": config.random_state, "n_jobs": 4, "verbosity": -1}
    if family == "linear":
        return linear
    if family == "lightgbm":
        return {"return": "LGBMRegressor", "probability": "LGBMClassifier", **tree}
    return {"weights": [.5, .5], "linear": linear, "lightgbm": tree}


def _fit_base(train: pd.DataFrame, features: list[str], family: str,
              config: ReliableSettings) -> dict:
    x = _matrix(train, features)
    y = train["label_return"].to_numpy(dtype=float)
    binary = (y > 0).astype(int)
    if family == "linear":
        regressor = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True),
                                  StandardScaler(), Ridge(alpha=10.0))
        classifier = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True),
                                   StandardScaler(), LogisticRegression(C=1.0, max_iter=300))
    else:
        from lightgbm import LGBMClassifier, LGBMRegressor

        params = _model_parameters("lightgbm", config)
        params = {k: v for k, v in params.items() if k not in {"return", "probability"}}
        regressor = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True),
                                  LGBMRegressor(**params))
        classifier = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True),
                                   LGBMClassifier(**params))
    regressor.fit(x, y)
    if np.unique(binary).size == 2:
        classifier.fit(x, binary)
    else:
        classifier = None
    return {"regressor": regressor, "classifier": classifier,
            "up_rate": float((binary.sum() + 1) / (len(binary) + 2))}


def _predict(model: dict, x: np.ndarray) -> tuple[float, float]:
    predicted = float(model["regressor"].predict(x)[0])
    classifier = model["classifier"]
    raw = float(classifier.predict_proba(x)[0, 1]) if classifier is not None else model["up_rate"]
    return predicted, raw


def _logit(values: Any) -> np.ndarray:
    clipped = np.clip(np.asarray(values, dtype=float), 1e-6, 1 - 1e-6)
    return np.log(clipped / (1 - clipped)).reshape(-1, 1)


def _calibrate(records: list[dict], raw: float, config: ReliableSettings) -> tuple[float, bool]:
    binary = np.asarray([r["actual_return"] > 0 for r in records], dtype=int)
    if len(records) < config.min_calibration_rows or np.unique(binary).size < 2:
        return raw, False
    calibrator = LogisticRegression(C=1.0, max_iter=300)
    calibrator.fit(_logit([r["raw_p_up"] for r in records]), binary)
    return float(calibrator.predict_proba(_logit([raw]))[0, 1]), True


def _radius(records: list[dict], config: ReliableSettings, baseline: str | None = None) -> float | None:
    residuals = []
    for record in records:
        prediction = record["expected_return"] if baseline is None else record["baselines"][baseline]["expected_return"]
        error = abs(record["actual_return"] - prediction)
        if math.isfinite(error):
            residuals.append(error)
    n = len(residuals)
    if n < config.min_calibration_rows:
        return None
    rank = math.ceil((n + 1) * .8)
    # A requested order statistic beyond available evidence has an infinite bound;
    # abstain instead of inventing a finite width.
    if rank > n:
        return None
    return float(np.partition(residuals, rank - 1)[rank - 1])


def _range(predicted: float, radius: float | None) -> dict:
    return {"range_low": None if radius is None else predicted - radius,
            "range_high": None if radius is None else predicted + radius}


def _mature(records: list[dict], cutoff: pd.Timestamp, window: int) -> list[dict]:
    cutoff_text = cutoff.isoformat()
    return [r for r in records if r.get("label_available_at") is not None
            and r["label_available_at"] <= cutoff_text
            and r["target_close_at"] <= cutoff_text][-window:]


def _walk(data: pd.DataFrame, groups: dict[str, list[str]], config: ReliableSettings,
          artifact_sink: dict | None = None, prediction_grid: pd.DataFrame | None = None) -> dict:
    histories = {f"{family}:{group}": [] for group in groups for family in config.model_families}
    fitted: dict = {}
    last_anchor = None
    fit_artifacts = {}
    fit_snapshots = {}
    grid = prediction_grid if prediction_grid is not None else data
    grid_cutoffs = dict(zip(grid["_asof_label"], grid["_cutoff"], strict=True))
    legacy_records: list[dict] = []
    vol = pd.to_numeric(data.get("volatility_20d", pd.Series(index=data.index, dtype=float)), errors="coerce")
    thresholds = vol.expanding(min_periods=20).median().shift(1)
    for position in range(config.initial_train_days + config.purge_days, len(data)):
        row = data.iloc[[position]]
        cutoff = row["_cutoff"].iloc[0]
        anchor = session_anchor(row["_asof_label"].iloc[0], config.calendar_name,
                                config.refit_origin, config.retrain_step_days)
        if anchor is None:
            continue
        if anchor["anchor_asof"] in grid_cutoffs:
            anchor = {**anchor, "prediction_cutoff": grid_cutoffs[anchor["anchor_asof"]].isoformat()}
        elif prediction_grid is not None:
            anchor = {**anchor, "grid_cutoff_missing": True}
        if anchor["anchor_asof"] != last_anchor:
            fitted = {}
            fit_artifacts = {}
            fit_snapshots = {}
            for group, features in groups.items():
                bases = {"linear"}
                if any(f in config.model_families for f in ("lightgbm", "ensemble")):
                    bases.add("lightgbm")
                snapshot = make_fit_snapshot(data, features, anchor, asdict(config))
                fit_snapshots[group] = snapshot
                fitted[group] = ({family: _fit_base(restore_fit_snapshot(snapshot, features, asdict(config)),
                                                   features, family, config) for family in sorted(bases)}
                                 if snapshot["status"] == "ready" else {})
                if fitted[group]:
                    fit_artifacts[group] = make_model_artifact(snapshot, fitted[group])
            if "price" in groups and groups["price"] == PRICE_FEATURES and fitted["price"]:
                fitted["_legacy"] = fitted["price"]["linear"]
                fit_snapshots["_legacy"] = fit_snapshots["price"]
            else:
                snapshot = make_fit_snapshot(data, PRICE_FEATURES, anchor, asdict(config))
                fit_snapshots["_legacy"] = snapshot
                fitted["_legacy"] = (_fit_base(restore_fit_snapshot(snapshot, PRICE_FEATURES, asdict(config)),
                                              PRICE_FEATURES, "linear", config)
                                     if snapshot["status"] == "ready" else None)
            last_anchor = anchor["anchor_asof"]
            if artifact_sink is not None:
                artifact_sink.clear()
                artifact_sink.update(fit_artifacts)
        if fitted.get("_legacy") is None:
            continue
        if not _current_features_available(row, cutoff):
            continue
        # Reuse only this observation's converted inputs. Each group's feature
        # order is part of the key; training and maturity rules stay unchanged.
        price_key = tuple(PRICE_FEATURES)
        row_matrices = {price_key: _matrix(row, PRICE_FEATURES)}
        legacy_prediction, legacy_p = _predict(fitted["_legacy"], row_matrices[price_key])
        prior_legacy = _mature(legacy_records, cutoff, config.calibration_window)
        # Expanding baselines use every label actually available at this prediction,
        # independently of the model's periodic refit cadence.
        available = data.iloc[:position]
        available = available[(available["_available"] <= cutoff) & (available["_target"] <= cutoff)]
        y = available["label_return"].to_numpy(dtype=float)
        mean, rate = float(y.mean()), float(((y > 0).sum() + 1) / (len(y) + 2))
        baselines = {
            "legacy": {"expected_return": legacy_prediction, "p_up": legacy_p,
                       **_range(legacy_prediction, _radius(prior_legacy, config, "legacy"))},
            "up_rate": {"p_up": rate},
            "mean": {"expected_return": mean, "p_up": rate,
                     **_range(mean, _radius(prior_legacy, config, "mean"))},
        }
        # Descriptive forecasting references do not alter the approved gates.
        # Their hard directions are deliberately not advertised as calibrated.
        previous_return = float(row["return_1d"].iloc[0])
        trend_return = float(row["return_20d"].iloc[0]) / 20
        record_base = {
            "asof_date": row["_asof_label"].iloc[0],
            "target_date": row["_target_label"].iloc[0],
            "prediction_cutoff": cutoff.isoformat(),
            "label_available_at": (None if pd.isna(row["_available"].iloc[0])
                                   else row["_available"].iloc[0].isoformat()),
            "target_close_at": row["_target"].iloc[0].isoformat(),
            "actual_return": float(row["label_return"].iloc[0]),
            "calendar_event_bucket": (bool(row["calendar_event_bucket"].iloc[0])
                if "calendar_event_bucket" in row and pd.notna(row["calendar_event_bucket"].iloc[0]) else None),
            "baselines": baselines,
            "references": {"previous_direction": {"expected_return": previous_return, "p_up": float(previous_return > 0)},
                           "simple_trend": {"expected_return": trend_return, "p_up": float(trend_return > 0)}},
            "volatility_regime": ("unknown" if pd.isna(thresholds.iloc[position]) else
                                  "high" if vol.iloc[position] > thresholds.iloc[position] else "low"),
            "trend_regime": "positive" if float(row["return_20d"].iloc[0]) > 0 else "nonpositive",
            "train_n": fit_snapshots["_legacy"]["train_n"],
            "fit_cutoff": fit_snapshots["_legacy"]["fit_cutoff"],
        }
        for group, features in groups.items():
            if not fitted[group]:
                continue
            feature_key = tuple(features)
            if feature_key not in row_matrices:
                row_matrices[feature_key] = _matrix(row, features)
            matrix = row_matrices[feature_key]
            if not np.isfinite(matrix).all():
                continue
            predictions = {base: (legacy_prediction, legacy_p)
                           if model is fitted["_legacy"] and feature_key == price_key
                           else _predict(model, matrix)
                           for base, model in fitted[group].items()}
            for family in config.model_families:
                expected, raw = (tuple(np.mean([predictions["linear"], predictions["lightgbm"]], axis=0))
                                 if family == "ensemble" else predictions[family])
                key = f"{family}:{group}"
                previous = _mature(histories[key], cutoff, config.calibration_window)
                p_up, calibrated = _calibrate(previous, float(raw), config)
                radius = _radius(previous, config)
                histories[key].append({**record_base,
                    "fit_asof": fit_snapshots[group]["anchor_asof"],
                    "fit_snapshot_hash": fit_snapshots[group]["snapshot_hash"],
                    "fit_cutoff": fit_snapshots[group]["fit_cutoff"],
                    "train_n": fit_snapshots[group]["train_n"],
                    "expected_return": float(expected),
                    "raw_p_up": float(raw), "p_up": p_up, "p_down": 1 - p_up, "p_flat": 0.0,
                    "calibration_n": len(previous), "calibration_ready": calibrated and radius is not None,
                    "probability_method": "sigmoid_oof" if calibrated else "raw_uncalibrated",
                    "scale_at_prediction": float(vol.iloc[position]) if np.isfinite(vol.iloc[position]) else None,
                    "interval_shadows": normalized_interval_shadows(previous, cutoff=cutoff,
                        predicted=float(expected), scale=vol.iloc[position], mean_prediction=mean,
                        window=config.calibration_window, min_rows=config.min_calibration_rows,
                        event_bucket=record_base["calendar_event_bucket"]),
                    **_range(float(expected), radius)})
        legacy_records.append(record_base)
    return histories


def _simple_metrics(actual: np.ndarray, predicted: np.ndarray | None, probability: np.ndarray,
                    low: np.ndarray | None = None, high: np.ndarray | None = None) -> dict:
    binary = (actual > 0).astype(float)
    p = np.clip(probability, 1e-12, 1 - 1e-12)
    result = {"n": len(actual), "brier": float(np.mean((probability - binary) ** 2)),
              "probability_direction_hit": float(np.mean((probability >= .5) == (actual > 0))),
              "log_loss": float(np.mean(-binary * np.log(p) - (1 - binary) * np.log(1 - p)))}
    if predicted is not None:
        result.update(mae=float(np.mean(abs(actual - predicted))),
                      return_direction_hit=float(np.mean((predicted > 0) == (actual > 0))),
                      direction_hit=float(np.mean((predicted > 0) == (actual > 0))))
    if low is not None and high is not None:
        result.update(coverage80=float(np.mean((actual >= low) & (actual <= high))),
                      average_width=float(np.mean(high - low)),
                      interval_score80=float(np.mean(interval_score80(actual, low, high))),
                      upper_breach=float(np.mean(actual > high)),
                      lower_breach=float(np.mean(actual < low)))
    return result


def forecast_reliable_challenger(history: pd.DataFrame, latest: pd.DataFrame, evaluation: dict, *,
                                 family: str, group: str, settings: ReliableSettings | None = None) -> dict:
    """Report a predeclared unpromoted candidate using only its own mature OOF.

    This does not choose a runner-up from terminal outcomes or change the main
    frozen model. Its retrospective terminal diagnostics remain explicitly separate.
    """
    config = settings or ReliableSettings()
    candidate = next((r for r in evaluation.get("candidate_reports", [])
                      if r["family"] == family and r["group"] == group), None)
    if candidate is None or latest.empty:
        return {"status": "not_ready", "group": group, "family": family, "production_weight": 0.0,
                "gate_reasons": ["requested_challenger_inputs_unavailable"]}
    cutoff = _timestamp(latest["prediction_cutoff"].iloc[-1], session_close=False)
    records = evaluation.get("candidate_observations", {}).get(f"{family}:{group}", [])
    mature = _mature(records, cutoff, len(records))
    terminal = [r for r in mature if r["asof_date"] >= evaluation["selection_frozen_before"]]
    metrics = _score(terminal, config)
    selected = {key: candidate[key] for key in ("family", "group", "features")}
    promoted = evaluation.get("selected") == selected and evaluation.get("status") == "accepted_shadow"
    reasons = list(dict.fromkeys([*candidate["gate_reasons"], *_gate(metrics, config),
                                  *([] if promoted else ["predeclared_challenger_not_promoted"])]))
    own_evaluation = {"selected": selected, "status": "accepted_shadow" if promoted else "rejected",
                      "gate_reasons": reasons, "calibration_observations": mature[-config.calibration_window:],
                      "fit_artifacts": evaluation.get("fit_artifacts", {})}
    forecast = forecast_reliable_latest(history, latest, own_evaluation, settings=config)
    return {**forecast, "role": "predeclared_research_challenger_not_terminal_selected",
            "development_metrics": candidate["development_metrics"], "terminal_metrics": metrics,
            "slices": _slices(terminal, config),
            "trial_status": "retrospective_terminal_previously_viewed"}


def _bootstrap(difference: np.ndarray, config: ReliableSettings) -> dict:
    n = len(difference)
    block = min(config.bootstrap_block_days, n)
    rng = np.random.default_rng(config.random_state)
    means = np.empty(config.bootstrap_samples)
    for i in range(config.bootstrap_samples):
        starts = rng.integers(0, n - block + 1, size=math.ceil(n / block))
        indices = (starts[:, None] + np.arange(block)).ravel()[:n]
        means[i] = difference[indices].mean()
    low, high = np.quantile(means, [.025, .975])
    return {"low": float(low), "high": float(high), "mean": float(difference.mean()),
            "samples": config.bootstrap_samples, "block_days": block,
            "method": "paired_moving_block", "random_state": config.random_state}


def _score(records: list[dict], config: ReliableSettings, *, bootstrap: bool = True) -> dict:
    usable = [r for r in records if r["calibration_ready"] and
              all(r["baselines"][b]["range_low"] is not None for b in ("legacy", "mean"))]
    if not usable:
        return {"n": 0, "baselines": {}, "brier_difference_ci95": {}}
    y = np.asarray([r["actual_return"] for r in usable])
    predicted = np.asarray([r["expected_return"] for r in usable])
    p = np.asarray([r["p_up"] for r in usable])
    metrics = _simple_metrics(y, predicted, p,
                             np.asarray([r["range_low"] for r in usable]),
                             np.asarray([r["range_high"] for r in usable]))
    baseline_metrics = {}
    for name in ("legacy", "up_rate", "mean"):
        baseline = [r["baselines"][name] for r in usable]
        baseline_metrics[name] = _simple_metrics(y,
            None if name == "up_rate" else np.asarray([r["expected_return"] for r in baseline]),
            np.asarray([r["p_up"] for r in baseline]),
            None if name == "up_rate" else np.asarray([r["range_low"] for r in baseline]),
            None if name == "up_rate" else np.asarray([r["range_high"] for r in baseline]))
    metrics["baselines"] = baseline_metrics
    references = {}
    for name in ("previous_direction", "simple_trend"):
        predictions = [record["references"][name] for record in usable]
        references[name] = {**_simple_metrics(y, np.asarray([r["expected_return"] for r in predictions]),
                                              np.asarray([r["p_up"] for r in predictions])),
                            "probability_method": "hard_direction_reference_uncalibrated",
                            "role": "forecast_reference_not_acceptance_gate"}
    references["buy_hold"] = {"n": len(y), "hypothetical_exposure_return": float(np.prod(1 + y) - 1),
                               "role": "descriptive_index_exposure_not_execution_pnl",
                               "assumptions": "unit index exposure on aligned scored sessions; no fees, financing, dividends or execution model"}
    references["cash"] = {"n": len(y), "hypothetical_exposure_return": 0.0,
                           "role": "descriptive_zero_exposure_not_execution_pnl",
                           "assumptions": "zero index exposure; interest and costs not modeled"}
    metrics["reference_baselines"] = references
    metrics["interval_shadows"] = _score_interval_shadows(usable, config, bootstrap=False)
    metrics["brier_difference_ci95"] = {}
    if bootstrap:
        loss = (p - (y > 0)) ** 2
        for name in ("legacy", "up_rate"):
            baseline_loss = (np.asarray([r["baselines"][name]["p_up"] for r in usable]) - (y > 0)) ** 2
            metrics["brier_difference_ci95"][name] = _bootstrap(loss - baseline_loss, config)
    return metrics


def _score_interval_shadows(records: list[dict], config: ReliableSettings, *, bootstrap: bool) -> dict:
    reports = {}
    for name in ("volatility_normalized", "mean_volatility_normalized"):
        rows = [row for row in records if row.get("interval_shadows", {}).get(name, {}).get("status") == "ready"
                and row.get("range_low") is not None]
        report = {"n": len(rows), "eligible_forecasts": len(records),
                  "availability_rate": len(rows) / len(records) if records else 0.,
                  "evidence_status": "retrospective_diagnostic_not_promotion", "production_weight": 0.}
        if rows:
            y = np.asarray([row["actual_return"] for row in rows])
            low = np.asarray([row["interval_shadows"][name]["range_low"] for row in rows])
            high = np.asarray([row["interval_shadows"][name]["range_high"] for row in rows])
            score = interval_score80(y, low, high)
            incumbent = interval_score80(y, np.asarray([row["range_low"] for row in rows]),
                                        np.asarray([row["range_high"] for row in rows]))
            report.update(coverage80=float(np.mean((y >= low) & (y <= high))),
                average_width=float(np.mean(high - low)), lower_breach=float(np.mean(y < low)),
                upper_breach=float(np.mean(y > high)), interval_score80=float(np.mean(score)),
                incumbent_interval_score80=float(np.mean(incumbent)),
                interval_score_difference=float(np.mean(score - incumbent)))
            if bootstrap:
                report["interval_score_difference_ci95"] = _bootstrap(score - incumbent, config)
        reports[name] = report
    return reports


def _gate(metrics: dict, config: ReliableSettings) -> list[str]:
    reasons = []
    if metrics["n"] < config.min_oos_rows:
        reasons.append("insufficient_oos_rows")
    if not metrics.get("baselines"):
        return [*reasons, "missing_calibrated_baseline_evidence"]
    for name in ("legacy", "up_rate"):
        baseline = metrics["baselines"][name]
        if not (metrics["brier"] < baseline["brier"] and
                metrics["brier_difference_ci95"].get(name, {}).get("high", math.inf) < 0):
            reasons.append(f"brier_not_significantly_better_than_{name}")
        if metrics["log_loss"] > baseline["log_loss"]:
            reasons.append(f"log_loss_worse_than_{name}")
    for name in ("legacy", "mean"):
        if metrics["mae"] > metrics["baselines"][name]["mae"]:
            reasons.append(f"mae_worse_than_{name}")
    if not .75 <= metrics["coverage80"] <= .85:
        reasons.append("coverage80_outside_75_85")
    if metrics["average_width"] > 1.1 * metrics["baselines"]["mean"]["average_width"]:
        reasons.append("interval_too_wide")
    if metrics["lower_breach"] > metrics["baselines"]["mean"]["lower_breach"] + .02:
        reasons.append("excess_lower_breach")
    return reasons


def _slices(records: list[dict], config: ReliableSettings) -> dict:
    output: dict[str, dict] = {}
    for dimension in ("year", "volatility_regime", "trend_regime"):
        subsets: dict[str, list] = {}
        for row in records:
            value = row["asof_date"][:4] if dimension == "year" else row[dimension]
            subsets.setdefault(value, []).append(row)
        output[dimension] = {name: _score(rows, config, bootstrap=False) for name, rows in subsets.items()}
    bins = []
    for lower in np.arange(0, 1, .1):
        subset = [r for r in records if r["calibration_ready"] and lower <= r["p_up"] < lower + .1]
        bins.append({"lower": round(float(lower), 1), "upper": round(float(lower + .1), 1),
                     "n": len(subset),
                     "mean_p_up": float(np.mean([r["p_up"] for r in subset])) if subset else None,
                     "observed_up_rate": float(np.mean([r["actual_return"] > 0 for r in subset])) if subset else None})
    output["reliability_bins"] = bins
    return output


def _empty(reason: str, config: ReliableSettings) -> dict:
    return {"status": "not_ready", "selected": None, "candidate_reports": [], "metrics": {"n": 0},
            "gate_reasons": [reason], "observations": [], "calibration_observations": [],
            "candidate_observations": {}, "production_weight": 0.0, "settings": asdict(config)}


def evaluate_reliable_forecasts(frame: pd.DataFrame, *, feature_groups: dict[str, list[str]] | None = None,
                                settings: ReliableSettings | None = None,
                                reporting_cutoff: Any = None,
                                prediction_grid: pd.DataFrame | None = None) -> dict:
    """Evaluate only labeled history, preserving the caller's frame and frozen test choice."""
    config = settings or ReliableSettings()
    data = _prepare(frame)
    grid = _prepare(frame if prediction_grid is None else prediction_grid, keep_unlabeled=True)
    if reporting_cutoff is not None:
        reporting_cutoff = _timestamp(reporting_cutoff, session_close=False)
        if pd.isna(reporting_cutoff):
            return _empty("invalid_reporting_cutoff", config)
        # The reporting date bounds prediction chronology as well as labels.
        # Otherwise unseen future feature rows could move the terminal split.
        data = data.loc[data["_cutoff"] <= reporting_cutoff].reset_index(drop=True) if not data.empty else data
        grid = grid.loc[grid["_cutoff"] <= reporting_cutoff].reset_index(drop=True) if not grid.empty else grid
    if data.empty or len(data) <= config.initial_train_days + config.purge_days:
        return _empty("insufficient_history", config)
    if any(column not in data for column in PRICE_FEATURES):
        return _empty("missing_price_features", config)
    if any(not pd.to_numeric(data[c], errors="coerce").notna().any() for c in PRICE_FEATURES):
        return _empty("missing_price_feature_values", config)
    groups = {name: list(dict.fromkeys(columns)) for name, columns in
              (feature_groups or {"price": PRICE_FEATURES}).items()}
    invalid = [name for name, columns in groups.items() if not columns or any(c not in data for c in columns)]
    if invalid:
        return _empty("missing_candidate_features:" + ",".join(invalid), config)
    fit_artifacts = {}
    histories = _walk(data, groups, config, fit_artifacts, grid)
    if not histories or not any(histories.values()):
        return _empty("insufficient_mature_training_labels", config)
    # Boundaries depend only on chronology, never outcome values or candidate success.
    first_history = histories.get("linear:price") or next(rows for rows in histories.values() if rows)
    terminal_start = first_history[max(0, len(first_history) - config.test_days)]["asof_date"]
    selection_cutoff = _timestamp(next(r["prediction_cutoff"] for r in first_history
                                      if r["asof_date"] == terminal_start), session_close=False)
    reporting_cutoff = (data["_cutoff"].max() if reporting_cutoff is None
                        else _timestamp(reporting_cutoff, session_close=False))
    if pd.isna(reporting_cutoff):
        return _empty("invalid_reporting_cutoff", config)
    development_dates = [r["asof_date"] for r in first_history
                         if r["asof_date"] < terminal_start and r["calibration_ready"]]
    development_start = development_dates[max(0, len(development_dates) - config.development_days)] if development_dates else terminal_start
    reports = []
    for group, features in groups.items():
        for family in config.model_families:
            records = histories[f"{family}:{group}"]
            development = [r for r in _mature(records, selection_cutoff, len(records))
                           if development_start <= r["asof_date"] < terminal_start and r["calibration_ready"]]
            metrics = _score(development, config)
            reports.append({"family": family, "group": group, "features": features,
                            "development_metrics": metrics, "gate_reasons": _gate(metrics, config),
                            "eligible_n": len(development),
                            "eligible_start": development[0]["asof_date"] if development else None,
                            "eligible_end": development[-1]["asof_date"] if development else None})
    eligible = [r for r in reports if not r["gate_reasons"]]
    ranking = {"linear": 0, "ensemble": 1, "lightgbm": 2}
    chosen = min(eligible, key=lambda r: (r["development_metrics"]["brier"],
                 r["development_metrics"]["log_loss"], r["development_metrics"]["mae"],
                 ranking[r["family"]], r["group"] != "price", len(r["features"]), r["group"])) if eligible else next(
                     (r for r in reports if r["family"] == "linear" and r["group"] == "price"), None)
    if chosen is None:
        output = _empty("no_eligible_development_candidate_or_linear_price_fallback", config)
        output.update(candidate_reports=reports, candidate_observations=histories)
        return output
    selected = {k: chosen[k] for k in ("family", "group", "features")}
    records = histories[f"{selected['family']}:{selected['group']}"]
    terminal = [r for r in _mature(records, reporting_cutoff, len(records)) if r["asof_date"] >= terminal_start]
    metrics = _score(terminal, config)
    metrics["interval_shadows"] = _score_interval_shadows(terminal, config, bootstrap=True)
    reasons = _gate(metrics, config)
    if not eligible:
        reasons = ["development_gate_failed", *chosen["gate_reasons"], *reasons]
    reasons = list(dict.fromkeys(reasons))
    status = "accepted_shadow" if not reasons else "not_ready" if metrics["n"] < config.min_oos_rows else "rejected"
    result = {"status": status, "selected": selected, "candidate_reports": reports,
              "metrics": metrics, "gate_reasons": reasons, "observations": terminal,
              "calibration_observations": _mature(records, reporting_cutoff, config.calibration_window),
              "candidate_observations": histories, "production_weight": 0.0,
              "fit_artifacts": fit_artifacts,
              "evaluation_protocol": "retrospective_exchange_anchor_v1; previously_viewed_terminal_exploratory",
              "selection_frozen_before": terminal_start, "settings": asdict(config),
              "development_start": development_start, "evaluated_through": reporting_cutoff.isoformat(),
              "model_params": _model_parameters(selected["family"], config),
              "interval_note": "80% target coverage; temporal dependence prevents a coverage guarantee.",
              "model_card": {"target": "P(next_session_return > 0)", "horizon": "1D",
                             "calibration": "sigmoid on prior mature OOF logits",
                             "range": "prior mature OOF absolute residual order statistic",
                             "interval_shadow": "prior mature vol20-normalized OOF order statistic; point/probability unchanged",
                             "direction_definitions": {"probability": "p_up >= 0.5", "return": "expected_return > 0", "actual": "actual_return > 0"},
                             "selection": "development only; terminal confirmation", "production_weight": 0.0},
              "ablation_comparison": [{"family": r["family"], "group": r["group"],
                  "development_metrics": r["development_metrics"], "gate_reasons": r["gate_reasons"]} for r in reports]}
    result.update(_slices(terminal, config))
    return result


def forecast_reliable_latest(history: pd.DataFrame, latest: pd.DataFrame, evaluation: dict, *,
                             settings: ReliableSettings | None = None,
                             fit_artifact: dict | None = None,
                             require_live_snapshot: bool = False) -> dict:
    """Refit the frozen selection; use only prior mature OOF records for calibration."""
    config = settings or ReliableSettings()
    selection = evaluation.get("selected")
    result = {"status": "not_ready", "gate_reasons": [], "production_weight": 0.0,
              "selected": selection, "expected_return": None, "range_low": None, "range_high": None,
              "p_up": None, "p_down": None, "p_flat": 0.0, "calibration_n": 0,
              "probability_method": "unavailable"}
    if not selection or latest.empty:
        return {**result, "gate_reasons": ["missing_selection_or_latest"]}
    features, family = list(selection["features"]), selection["family"]
    if any(c not in latest or c not in history for c in features):
        return {**result, "gate_reasons": ["missing_selected_features"]}
    current = latest.iloc[[-1]].copy()
    cutoff = _timestamp(current.get("prediction_cutoff", current["asof_date"]).iloc[0],
                        session_close="prediction_cutoff" not in current)
    asof_label = pd.Timestamp(current["asof_date"].iloc[0]).date().isoformat()
    if pd.isna(cutoff) or not np.isfinite(_matrix(current, features)).all():
        return {**result, "gate_reasons": ["invalid_cutoff_or_missing_selected_feature_values"]}
    if not _current_features_available(current, cutoff):
        return {**result, "gate_reasons": ["current_features_unavailable_at_prediction_cutoff"]}
    anchor = session_anchor(asof_label, config.calendar_name, config.refit_origin, config.retrain_step_days)
    if anchor is None:
        return {**result, "gate_reasons": ["before_refit_protocol_origin"]}
    artifact = fit_artifact or evaluation.get("fit_artifacts", {}).get(selection["group"])
    if artifact is None:
        return {**result, "gate_reasons": ["missing_live_anchor_snapshot" if require_live_snapshot
                                           else "missing_anchor_snapshot"], **anchor}
    try:
        validate_model_artifact(artifact, features, asdict(config), anchor["anchor_asof"],
                                require_live=require_live_snapshot, prediction_cutoff=cutoff)
    except (ValueError, KeyError, TypeError) as error:
        return {**result, "gate_reasons": [str(error)], **anchor}
    snapshot = artifact["snapshot"]
    expected, raw = predict_model_artifact(artifact, family, _matrix(current, features))
    records = [r for r in evaluation.get("calibration_observations", [])
               if r["asof_date"] < asof_label]
    previous = _mature(records, cutoff, config.calibration_window)
    p_up, ready = _calibrate(previous, raw, config)
    radius = _radius(previous, config)
    known = _prepare(history)
    known = known.loc[(known["_asof_label"] < asof_label) & (known["_available"] <= cutoff)
                      & (known["_target"] <= cutoff)]
    mean = float(known["label_return"].mean()) if not known.empty else None
    scale = current["volatility_20d"].iloc[0]
    shadows = normalized_interval_shadows(previous, cutoff=cutoff, predicted=expected, scale=scale,
        mean_prediction=mean, window=config.calibration_window, min_rows=config.min_calibration_rows,
        event_bucket=(bool(current["calendar_event_bucket"].iloc[0])
            if "calendar_event_bucket" in current and pd.notna(current["calendar_event_bucket"].iloc[0]) else None))
    reasons = list(evaluation.get("gate_reasons", []))
    if not ready or radius is None:
        status = "not_ready"
        reasons = [*reasons, "insufficient_prior_oof_calibration"]
        radius = None
    else:
        status = "research_candidate" if evaluation.get("status") == "accepted_shadow" else "rejected"
    result.update(status=status, gate_reasons=reasons, expected_return=expected, raw_p_up=raw,
                  p_up=p_up, p_down=1 - p_up, calibration_n=len(previous),
                  probability_method="sigmoid_oof" if ready else "raw_uncalibrated",
                  calibration_ready=ready and radius is not None, **_range(expected, radius),
                  asof_date=asof_label,
                  prediction_cutoff=cutoff.isoformat(), model_params=_model_parameters(family, config),
                  family=family, group=selection["group"], features=features,
                  train_n=snapshot["train_n"], train_last_asof=snapshot["train_last_asof"],
                  fit_asof=snapshot["anchor_asof"], fit_cutoff=snapshot["fit_cutoff"],
                  fit_snapshot_hash=snapshot["snapshot_hash"], fit_artifact_hash=artifact["artifact_hash"],
                  fit_provenance=snapshot["provenance"], next_refit_asof=anchor["next_refit_asof"])
    result.update(scale_at_prediction=float(scale) if np.isfinite(scale) else None, interval_shadows=shadows)
    rate = float((known['label_return'] > 0).mean()) if not known.empty else None
    result['baselines'] = {'up_rate': {'p_up': rate, 'known_label_n': len(known)},
                          'mean': {'expected_return': mean, 'p_up': rate, 'known_label_n': len(known),
                                   **_range(mean, _radius(previous, config, 'mean'))}}
    prior_vol = pd.to_numeric(known['volatility_20d'], errors='coerce').dropna()
    result['volatility_regime'] = ('unknown' if prior_vol.empty or not np.isfinite(scale)
                                   else 'high' if scale > prior_vol.median() else 'low')
    return result


def resolve_live_fit(history: pd.DataFrame, latest: pd.DataFrame, selection: dict, *,
                     settings: ReliableSettings, state_dir, market: str,
                     allow_capture: bool = True) -> dict:
    """Restore the first actual fit; capture only on its scheduled anchor session."""
    unavailable = {"status": "not_ready", "reason": "missing_live_anchor_snapshot"}
    if latest.empty or not selection:
        return unavailable
    current = latest.iloc[-1]
    asof = pd.Timestamp(current["asof_date"]).date().isoformat()
    anchor = session_anchor(asof, settings.calendar_name, settings.refit_origin, settings.retrain_step_days)
    if anchor is None:
        return {**unavailable, "reason": "before_refit_protocol_origin"}
    metadata = {key: anchor[key] for key in ("anchor_asof", "next_refit_asof")}
    path = artifact_path(state_dir, market, anchor["anchor_asof"])
    features = list(selection["features"])
    cutoff = _timestamp(current.get("prediction_cutoff", current["asof_date"]),
                        session_close="prediction_cutoff" not in current)
    if pd.isna(cutoff):
        return {**unavailable, **metadata, "reason": "invalid_live_fit_cutoff"}
    try:
        if path.is_file():
            artifact = json.loads(path.read_text())
        elif asof != anchor["anchor_asof"] or not allow_capture:
            return {**unavailable, **metadata}
        else:
            # The actual issue context supplies this clock even when its own
            # target label is pending and _prepare(history) excludes that row.
            snapshot = make_fit_snapshot(_prepare(history), features,
                {**anchor, "prediction_cutoff": cutoff.isoformat()}, asdict(settings), provenance="live_captured")
            if snapshot["status"] != "ready":
                failed = {"schema_version": "fit_failure_v1", "snapshot": snapshot}
                artifact = save_model_artifact(state_dir, market,
                    {**failed, "artifact_hash": content_hash(failed)})
            else:
                training = restore_fit_snapshot(snapshot, features, asdict(settings))
                family = selection["family"]
                bases = ("linear", "lightgbm") if family == "ensemble" else (family,)
                models = {base: _fit_base(training, features, base, settings) for base in bases}
                artifact = save_model_artifact(state_dir, market, make_model_artifact(snapshot, models))
        if artifact.get("schema_version") == "fit_failure_v1":
            if content_hash({k: v for k, v in artifact.items() if k != "artifact_hash"}) != artifact.get("artifact_hash"):
                raise ValueError("fit failure artifact hash mismatch")
            if artifact["snapshot"]["anchor_asof"] != anchor["anchor_asof"]:
                raise ValueError("fit failure anchor mismatch")
            return {**unavailable, **metadata, "reason": artifact["snapshot"]["reason"],
                    "failed_at": artifact["snapshot"].get("fit_cutoff")}
        validate_model_artifact(artifact, features, asdict(settings), anchor["anchor_asof"],
                                require_live=True, prediction_cutoff=cutoff)
        required = {"linear", "lightgbm"} if selection["family"] == "ensemble" else {selection["family"]}
        if not required.issubset(artifact["models"]):
            raise ValueError("fit model artifact family mismatch")
        return {"status": "ready", "artifact": artifact, **metadata}
    except (ValueError, KeyError, TypeError, OSError) as error:
        return {**unavailable, **metadata, "reason": str(error)}
