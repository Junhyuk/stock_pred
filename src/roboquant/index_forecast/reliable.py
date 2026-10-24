"""Chronological binary calibration and research gates for one-session index returns.

All calibration residuals are genuinely out of sample. The terminal block evaluates
one choice frozen using the preceding development block; it never chooses a model.
"""
from __future__ import annotations

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

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if name not in {"model_families", "random_state", "purge_days"} and value < 1:
                raise ValueError(f"{name} must be positive")
        if self.purge_days < 0:
            raise ValueError("purge_days must be nonnegative")
        if any(f not in {"linear", "lightgbm", "ensemble"} for f in self.model_families):
            raise ValueError("unsupported model family")


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


def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
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
    return data.dropna(subset=["_asof", "_cutoff", "_target", "label_return"]).sort_values(
        "_asof", kind="stable").drop_duplicates("_asof").reset_index(drop=True)


def _matrix(data: pd.DataFrame, features: list[str]) -> np.ndarray:
    return data[features].apply(pd.to_numeric, errors="coerce").replace(
        [np.inf, -np.inf], np.nan).to_numpy(dtype=float)


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


def _walk(data: pd.DataFrame, groups: dict[str, list[str]], config: ReliableSettings) -> dict:
    histories = {f"{family}:{group}": [] for group in groups for family in config.model_families}
    fitted: dict = {}
    last_fit = -config.retrain_step_days
    legacy_records: list[dict] = []
    vol = pd.to_numeric(data.get("volatility_20d", pd.Series(index=data.index, dtype=float)), errors="coerce")
    thresholds = vol.expanding(min_periods=20).median().shift(1)
    for position in range(config.initial_train_days + config.purge_days, len(data)):
        row = data.iloc[[position]]
        cutoff = row["_cutoff"].iloc[0]
        prefix = data.iloc[:position - config.purge_days] if config.purge_days else data.iloc[:position]
        train = prefix[(prefix["_asof"] < row["_asof"].iloc[0]) &
                       (prefix["_available"] <= cutoff) & (prefix["_target"] <= cutoff)]
        if len(train) < config.initial_train_days:
            continue
        if not fitted or position - last_fit >= config.retrain_step_days:
            fitted = {}
            for group, features in groups.items():
                bases = {"linear"}
                if any(f in config.model_families for f in ("lightgbm", "ensemble")):
                    bases.add("lightgbm")
                complete = train.loc[np.isfinite(_matrix(train, features)).all(axis=1)]
                fitted[group] = ({family: _fit_base(complete, features, family, config)
                                  for family in sorted(bases)}
                                 if len(complete) >= config.initial_train_days else {})
            if "price" in groups and groups["price"] == PRICE_FEATURES and fitted["price"]:
                fitted["_legacy"] = fitted["price"]["linear"]
            else:
                fitted["_legacy"] = _fit_base(train, PRICE_FEATURES, "linear", config)
            last_fit = position
        legacy_prediction, legacy_p = _predict(fitted["_legacy"], _matrix(row, PRICE_FEATURES))
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
            "baselines": baselines,
            "references": {"previous_direction": {"expected_return": previous_return, "p_up": float(previous_return > 0)},
                           "simple_trend": {"expected_return": trend_return, "p_up": float(trend_return > 0)}},
            "volatility_regime": ("unknown" if pd.isna(thresholds.iloc[position]) else
                                  "high" if vol.iloc[position] > thresholds.iloc[position] else "low"),
            "trend_regime": "positive" if float(row["return_20d"].iloc[0]) > 0 else "nonpositive",
            "train_n": len(train), "fit_cutoff": data["_cutoff"].iloc[last_fit].isoformat(),
        }
        for group, features in groups.items():
            if not fitted[group] or not np.isfinite(_matrix(row, features)).all():
                continue
            predictions = {base: _predict(model, _matrix(row, features))
                           for base, model in fitted[group].items()}
            for family in config.model_families:
                expected, raw = (tuple(np.mean([predictions["linear"], predictions["lightgbm"]], axis=0))
                                 if family == "ensemble" else predictions[family])
                key = f"{family}:{group}"
                previous = _mature(histories[key], cutoff, config.calibration_window)
                p_up, calibrated = _calibrate(previous, float(raw), config)
                radius = _radius(previous, config)
                histories[key].append({**record_base, "expected_return": float(expected),
                    "raw_p_up": float(raw), "p_up": p_up, "p_down": 1 - p_up, "p_flat": 0.0,
                    "calibration_n": len(previous), "calibration_ready": calibrated and radius is not None,
                    "probability_method": "sigmoid_oof" if calibrated else "raw_uncalibrated",
                    **_range(float(expected), radius)})
        legacy_records.append(record_base)
    return histories


def _simple_metrics(actual: np.ndarray, predicted: np.ndarray | None, probability: np.ndarray,
                    low: np.ndarray | None = None, high: np.ndarray | None = None) -> dict:
    binary = (actual > 0).astype(float)
    p = np.clip(probability, 1e-12, 1 - 1e-12)
    result = {"n": len(actual), "brier": float(np.mean((probability - binary) ** 2)),
              "log_loss": float(np.mean(-binary * np.log(p) - (1 - binary) * np.log(1 - p)))}
    if predicted is not None:
        result.update(mae=float(np.mean(abs(actual - predicted))),
                      direction_hit=float(np.mean((predicted > 0) == (actual > 0))))
    if low is not None and high is not None:
        result.update(coverage80=float(np.mean((actual >= low) & (actual <= high))),
                      average_width=float(np.mean(high - low)),
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
                      "gate_reasons": reasons, "calibration_observations": mature[-config.calibration_window:]}
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
    metrics["brier_difference_ci95"] = {}
    if bootstrap:
        loss = (p - (y > 0)) ** 2
        for name in ("legacy", "up_rate"):
            baseline_loss = (np.asarray([r["baselines"][name]["p_up"] for r in usable]) - (y > 0)) ** 2
            metrics["brier_difference_ci95"][name] = _bootstrap(loss - baseline_loss, config)
    return metrics


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
                                reporting_cutoff: Any = None) -> dict:
    """Evaluate only labeled history, preserving the caller's frame and frozen test choice."""
    config = settings or ReliableSettings()
    data = _prepare(frame)
    if reporting_cutoff is not None:
        reporting_cutoff = _timestamp(reporting_cutoff, session_close=False)
        if pd.isna(reporting_cutoff):
            return _empty("invalid_reporting_cutoff", config)
        # The reporting date bounds prediction chronology as well as labels.
        # Otherwise unseen future feature rows could move the terminal split.
        data = data.loc[data["_cutoff"] <= reporting_cutoff].reset_index(drop=True) if not data.empty else data
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
    histories = _walk(data, groups, config)
    if not histories or not any(histories.values()):
        return _empty("insufficient_mature_training_labels", config)
    # Boundaries depend only on chronology, never outcome values or candidate success.
    first_history = histories.get("linear:price") or next(iter(histories.values()))
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
    reasons = _gate(metrics, config)
    if not eligible:
        reasons = ["development_gate_failed", *chosen["gate_reasons"], *reasons]
    reasons = list(dict.fromkeys(reasons))
    status = "accepted_shadow" if not reasons else "not_ready" if metrics["n"] < config.min_oos_rows else "rejected"
    result = {"status": status, "selected": selected, "candidate_reports": reports,
              "metrics": metrics, "gate_reasons": reasons, "observations": terminal,
              "calibration_observations": _mature(records, reporting_cutoff, config.calibration_window),
              "candidate_observations": histories, "production_weight": 0.0,
              "selection_frozen_before": terminal_start, "settings": asdict(config),
              "development_start": development_start, "evaluated_through": reporting_cutoff.isoformat(),
              "model_params": _model_parameters(selected["family"], config),
              "interval_note": "80% target coverage; temporal dependence prevents a coverage guarantee.",
              "model_card": {"target": "P(next_session_return > 0)", "horizon": "1D",
                             "calibration": "sigmoid on prior mature OOF logits",
                             "range": "prior mature OOF absolute residual order statistic",
                             "selection": "development only; terminal confirmation", "production_weight": 0.0},
              "ablation_comparison": [{"family": r["family"], "group": r["group"],
                  "development_metrics": r["development_metrics"], "gate_reasons": r["gate_reasons"]} for r in reports]}
    result.update(_slices(terminal, config))
    return result


def forecast_reliable_latest(history: pd.DataFrame, latest: pd.DataFrame, evaluation: dict, *,
                             settings: ReliableSettings | None = None) -> dict:
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
    asof = _timestamp(current.get("asof_close_at", current["asof_date"]).iloc[0],
                      session_close="asof_close_at" not in current)
    data = _prepare(history)
    if data.empty:
        return {**result, "gate_reasons": ["insufficient_mature_training_labels"]}
    data = data[(data["_asof"] < asof) & (data["_available"] <= cutoff) & (data["_target"] <= cutoff)]
    data = data.loc[np.isfinite(_matrix(data, features)).all(axis=1)]
    if pd.isna(cutoff) or not np.isfinite(_matrix(current, features)).all():
        return {**result, "gate_reasons": ["invalid_cutoff_or_missing_selected_feature_values"]}
    if len(data) < config.initial_train_days:
        return {**result, "gate_reasons": ["insufficient_mature_training_labels"]}
    bases = ("linear", "lightgbm") if family == "ensemble" else (family,)
    predictions = [_predict(_fit_base(data, features, base, config), _matrix(current, features)) for base in bases]
    expected, raw = map(float, np.mean(predictions, axis=0))
    records = [r for r in evaluation.get("calibration_observations", [])
               if r["asof_date"] < asof_label]
    previous = _mature(records, cutoff, config.calibration_window)
    p_up, ready = _calibrate(previous, raw, config)
    radius = _radius(previous, config)
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
                  train_n=len(data), train_last_asof=data["_asof"].iloc[-1].isoformat())
    return result
