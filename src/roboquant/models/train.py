from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TrainResult:
    bundle: dict[str, Any]
    metrics: dict[str, float | int | str | None]
    validation_predictions: pd.DataFrame
    walk_forward_predictions: pd.DataFrame


def train_horizon_model(
    dataset: pd.DataFrame,
    horizon: str,
    feature_columns: list[str],
    model_config: dict[str, Any],
    horizon_days: int,
) -> TrainResult:
    data = _prepare_dataset(dataset, horizon, feature_columns)
    if data.empty:
        raise ValueError(f"No modeling rows available for horizon={horizon}")

    dates = sorted(pd.to_datetime(data["date"]).dropna().unique())
    validation_fraction = float(model_config.get("validation_fraction", 0.2))
    split_index = max(1, int(len(dates) * (1.0 - validation_fraction)))
    split_index = min(split_index, len(dates) - 1)
    train_dates = set(dates[:split_index])
    valid_dates = set(dates[split_index:])

    train = data[pd.to_datetime(data["date"]).isin(train_dates)].copy()
    valid = data[pd.to_datetime(data["date"]).isin(valid_dates)].copy()

    model_version = f"poc-{horizon}-{date.today().isoformat()}"
    bundle = _fit_bundle(train, horizon, feature_columns, model_config, model_version)
    validation_predictions = predict_with_bundle(bundle, valid)
    metrics = evaluate_predictions(valid, validation_predictions, top_k=20)
    metrics.update(
        {
            "horizon": horizon,
            "model_version": model_version,
            "train_rows": int(len(train)),
            "validation_rows": int(len(valid)),
            "feature_count": int(len(feature_columns)),
        }
    )

    walk_forward_predictions = pd.DataFrame()
    walk_config = model_config.get("walk_forward", {})
    if walk_config.get("enabled", True):
        walk_forward_predictions = walk_forward_predict(
            data,
            horizon,
            feature_columns,
            model_config,
            horizon_days,
            model_version=f"{model_version}-wf",
        )

    final_bundle = _fit_bundle(data, horizon, feature_columns, model_config, model_version)
    return TrainResult(
        bundle=final_bundle,
        metrics=metrics,
        validation_predictions=validation_predictions,
        walk_forward_predictions=walk_forward_predictions,
    )


def walk_forward_predict(
    dataset: pd.DataFrame,
    horizon: str,
    feature_columns: list[str],
    model_config: dict[str, Any],
    horizon_days: int,
    model_version: str,
) -> pd.DataFrame:
    data = _prepare_dataset(dataset, horizon, feature_columns)
    dates = sorted(pd.to_datetime(data["date"]).dropna().unique())
    if len(dates) < 10:
        return pd.DataFrame()

    walk_config = model_config.get("walk_forward", {})
    initial_train_days = int(walk_config.get("initial_train_days", 504))
    step_days = int(walk_config.get("step_days", 21))
    max_splits = walk_config.get("max_splits")
    min_train_dates = int(model_config.get("min_train_dates", 252))
    min_train_rows = int(model_config.get("min_train_rows", 1000))

    initial_train_days = min(initial_train_days, max(2, len(dates) // 2))
    predictions: list[pd.DataFrame] = []
    split_count = 0

    for test_start in range(initial_train_days, len(dates), step_days):
        train_end = test_start - int(horizon_days)
        if train_end < min_train_dates:
            continue
        train_dates = dates[:train_end]
        test_dates = dates[test_start : min(test_start + step_days, len(dates))]
        train = data[pd.to_datetime(data["date"]).isin(train_dates)].copy()
        test = data[pd.to_datetime(data["date"]).isin(test_dates)].copy()
        if len(train_dates) < min_train_dates or len(train) < min_train_rows or test.empty:
            continue

        split_version = f"{model_version}-split{split_count + 1:03d}"
        bundle = _fit_bundle(train, horizon, feature_columns, model_config, split_version)
        split_predictions = predict_with_bundle(bundle, test)
        split_predictions["model_version"] = model_version
        predictions.append(split_predictions)
        split_count += 1
        if max_splits is not None and split_count >= int(max_splits):
            break

    if not predictions:
        return pd.DataFrame()
    return pd.concat(predictions, ignore_index=True).sort_values(["asof_date", "symbol"])


def predict_with_bundle(bundle: dict[str, Any], frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "asof_date",
                "symbol",
                "horizon",
                "pred_return",
                "pred_prob_top20",
                "pred_prob_bottom20",
                "long_score",
                "short_score",
                "pred_risk",
                "confidence",
                "model_version",
            ]
        )

    features = bundle["feature_columns"]
    x = _feature_matrix(frame, features, pd.Series(bundle["fill_values"]))
    classifier = bundle["classifier"]
    bottom_classifier = bundle.get("bottom_classifier")
    regressor = bundle["regressor"]

    pred_prob = _predict_probability(classifier, x)
    pred_prob_bottom = (
        _predict_probability(bottom_classifier, x)
        if bottom_classifier is not None
        else 1.0 - pred_prob
    )
    pred_return = np.asarray(regressor.predict(x), dtype=float)
    pred_risk = pd.to_numeric(frame.get("risk_score", pd.Series(0.5, index=frame.index))).fillna(
        0.5
    )
    confidence = np.maximum(pred_prob, 1.0 - pred_prob)

    output = pd.DataFrame(
        {
            "asof_date": pd.to_datetime(frame["date"]).dt.date,
            "symbol": frame["symbol"].astype(str).str.zfill(6),
            "horizon": bundle["horizon"],
            "pred_return": pred_return,
            "pred_prob_top20": pred_prob,
            "pred_prob_bottom20": np.asarray(pred_prob_bottom, dtype=float).clip(0.0, 1.0),
            "pred_risk": pred_risk.to_numpy(dtype=float),
            "confidence": confidence,
            "model_version": bundle["model_version"],
        }
    )
    pred_return_rank = output.groupby("asof_date")["pred_return"].rank(pct=True).fillna(0.5)
    output["long_score"] = (
        0.6 * output["pred_prob_top20"] + 0.4 * pred_return_rank
    ).clip(0.0, 1.0)
    output["short_score"] = (
        0.6 * output["pred_prob_bottom20"] + 0.4 * (1.0 - pred_return_rank)
    ).clip(0.0, 1.0)
    return output


def evaluate_predictions(
    truth: pd.DataFrame,
    predictions: pd.DataFrame,
    top_k: int = 20,
) -> dict[str, float | int | None]:
    if truth.empty or predictions.empty:
        return {"precision_at_k": None, "ic": None, "rmse": None, "rows": 0}

    prediction_frame = predictions.copy()
    truth_frame = truth.copy()
    prediction_frame["asof_date"] = pd.to_datetime(prediction_frame["asof_date"]).dt.date
    truth_frame["date"] = pd.to_datetime(truth_frame["date"]).dt.date

    joined = prediction_frame.merge(
        truth_frame[
            [
                "date",
                "symbol",
                "horizon",
                "future_return",
                "excess_return",
                "is_top20pct",
            ]
        ].rename(columns={"date": "asof_date"}),
        on=["asof_date", "symbol", "horizon"],
        how="inner",
    )
    if joined.empty:
        return {"precision_at_k": None, "ic": None, "rmse": None, "rows": 0}

    precision_values = []
    for _, group in joined.groupby("asof_date"):
        top = group.sort_values("pred_prob_top20", ascending=False).head(top_k)
        if not top.empty:
            precision_values.append(float(top["is_top20pct"].mean()))

    ic = joined["pred_prob_top20"].corr(joined["excess_return"], method="spearman")
    rmse = float(np.sqrt(np.mean((joined["pred_return"] - joined["excess_return"]) ** 2)))
    return {
        "precision_at_k": float(np.mean(precision_values)) if precision_values else None,
        "ic": float(ic) if pd.notna(ic) else None,
        "rmse": rmse,
        "rows": int(len(joined)),
    }


def baseline_predictions(
    dataset: pd.DataFrame,
    horizon: str,
    model_version: str = "factor-baseline",
) -> pd.DataFrame:
    data = _prepare_dataset(dataset, horizon, [])
    if data.empty:
        return pd.DataFrame()
    score = _factor_baseline_score(data)
    pred_return = data.groupby("date")["ret_63d"].transform(lambda series: series.fillna(0).rank(pct=True))
    pred_return_rank = pred_return.fillna(0.5).clip(0.0, 1.0)
    bottom_score = (1.0 - score).clip(0.0, 1.0)
    return pd.DataFrame(
        {
            "asof_date": pd.to_datetime(data["date"]).dt.date,
            "symbol": data["symbol"].astype(str).str.zfill(6),
            "horizon": horizon,
            "pred_return": pred_return.fillna(0.0).to_numpy(dtype=float),
            "pred_prob_top20": score.clip(0.0, 1.0).to_numpy(dtype=float),
            "pred_prob_bottom20": bottom_score.to_numpy(dtype=float),
            "long_score": (0.6 * score + 0.4 * pred_return_rank)
            .clip(0.0, 1.0)
            .to_numpy(dtype=float),
            "short_score": (0.6 * bottom_score + 0.4 * (1.0 - pred_return_rank))
            .clip(0.0, 1.0)
            .to_numpy(dtype=float),
            "pred_risk": data["risk_score"].fillna(0.5).to_numpy(dtype=float),
            "confidence": np.maximum(score, 1.0 - score).clip(0.0, 1.0).to_numpy(dtype=float),
            "model_version": model_version,
        }
    )


def baseline_feature_predictions(
    features: pd.DataFrame,
    horizon: str,
    model_version: str = "factor-baseline-latest",
) -> pd.DataFrame:
    frame = features[features["horizon"] == horizon].copy()
    if frame.empty:
        return pd.DataFrame()
    frame["date"] = pd.to_datetime(frame["date"])
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    score = _factor_baseline_score(frame).clip(0.0, 1.0)
    pred_return = frame.groupby("date")["ret_63d"].transform(
        lambda series: series.fillna(0).rank(pct=True)
    )
    pred_return_rank = pred_return.fillna(0.5).clip(0.0, 1.0)
    bottom_score = (1.0 - score).clip(0.0, 1.0)
    return pd.DataFrame(
        {
            "asof_date": pd.to_datetime(frame["date"]).dt.date,
            "symbol": frame["symbol"].astype(str).str.zfill(6),
            "horizon": horizon,
            "pred_return": pred_return.fillna(0.0).to_numpy(dtype=float),
            "pred_prob_top20": score.to_numpy(dtype=float),
            "pred_prob_bottom20": bottom_score.to_numpy(dtype=float),
            "long_score": (0.6 * score + 0.4 * pred_return_rank)
            .clip(0.0, 1.0)
            .to_numpy(dtype=float),
            "short_score": (0.6 * bottom_score + 0.4 * (1.0 - pred_return_rank))
            .clip(0.0, 1.0)
            .to_numpy(dtype=float),
            "pred_risk": frame["risk_score"].fillna(0.5).to_numpy(dtype=float),
            "confidence": np.maximum(score, 1.0 - score).to_numpy(dtype=float),
            "model_version": model_version,
        }
    )


def _factor_baseline_score(frame: pd.DataFrame) -> pd.Series:
    def factor(column: str, default: float = 0.5) -> pd.Series:
        if column not in frame.columns:
            return pd.Series(default, index=frame.index, dtype=float)
        return pd.to_numeric(frame[column], errors="coerce").fillna(default).clip(0.0, 1.0)

    return (
        0.27 * factor("momentum_score")
        + 0.18 * factor("supply_demand_score")
        + 0.14 * factor("value_score")
        + 0.09 * factor("quality_score")
        + 0.09 * factor("liquidity_score")
        + 0.05 * factor("consensus_revision_score")
        + 0.03 * factor("target_upside_score")
        + 0.02 * factor("analyst_reliability_score")
        + 0.04 * factor("sentiment_score")
        + 0.09 * (1.0 - factor("risk_score"))
    ).clip(0.0, 1.0)


def save_model_bundle(bundle: dict[str, Any], path: str | Path) -> None:
    import joblib

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, path)


def load_model_bundle(path: str | Path) -> dict[str, Any]:
    import joblib

    return joblib.load(path)


def _prepare_dataset(
    dataset: pd.DataFrame,
    horizon: str,
    feature_columns: list[str],
) -> pd.DataFrame:
    frame = dataset.copy()
    frame = frame[frame["horizon"] == horizon].copy()
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"])
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    required = ["date", "symbol", "horizon", "excess_return", "is_top20pct"]
    if feature_columns:
        required += feature_columns
    required = [column for column in required if column in frame.columns]
    frame = frame.dropna(subset=["date", "symbol", "excess_return", "is_top20pct"])
    frame = frame.replace([np.inf, -np.inf], np.nan)
    return frame.sort_values(["date", "symbol"]).reset_index(drop=True)


def _fit_bundle(
    train: pd.DataFrame,
    horizon: str,
    feature_columns: list[str],
    model_config: dict[str, Any],
    model_version: str,
) -> dict[str, Any]:
    features = [column for column in feature_columns if column in train.columns]
    if not features:
        raise ValueError("No usable feature columns were found in the modeling dataset")

    x_raw = train[features].replace([np.inf, -np.inf], np.nan)
    fill_values = x_raw.median(numeric_only=True).fillna(0.0)
    x_train = x_raw.fillna(fill_values)
    y_class = train["is_top20pct"].astype(int)
    y_reg = train["excess_return"].astype(float)
    classifier, regressor = _make_estimators(y_class, y_reg, model_config)
    classifier.fit(x_train, y_class)
    regressor.fit(x_train, y_reg)
    bottom_classifier = None
    if "is_bottom20pct" in train.columns and train["is_bottom20pct"].notna().all():
        y_bottom = train["is_bottom20pct"].astype(int)
        bottom_classifier, _ = _make_estimators(y_bottom, y_reg, model_config)
        bottom_classifier.fit(x_train, y_bottom)

    return {
        "horizon": horizon,
        "feature_columns": features,
        "fill_values": fill_values.to_dict(),
        "classifier": classifier,
        "bottom_classifier": bottom_classifier,
        "regressor": regressor,
        "model_version": model_version,
    }


def _feature_matrix(frame: pd.DataFrame, feature_columns: list[str], fill_values: pd.Series) -> pd.DataFrame:
    x = frame.reindex(columns=feature_columns).replace([np.inf, -np.inf], np.nan)
    return x.fillna(fill_values).fillna(0.0)


def _make_estimators(y_class: pd.Series, y_reg: pd.Series, model_config: dict[str, Any]):
    random_state = int(model_config.get("random_state", 42))
    try:
        from lightgbm import LGBMClassifier, LGBMRegressor

        classifier = LGBMClassifier(
            objective="binary",
            n_estimators=300,
            learning_rate=0.03,
            num_leaves=31,
            subsample=0.9,
            colsample_bytree=0.9,
            random_state=random_state,
            verbose=-1,
        )
        regressor = LGBMRegressor(
            objective="regression",
            n_estimators=300,
            learning_rate=0.03,
            num_leaves=31,
            subsample=0.9,
            colsample_bytree=0.9,
            random_state=random_state,
            verbose=-1,
        )
    except Exception:
        try:
            from sklearn.ensemble import (
                HistGradientBoostingClassifier,
                HistGradientBoostingRegressor,
            )

            classifier = HistGradientBoostingClassifier(random_state=random_state)
            regressor = HistGradientBoostingRegressor(random_state=random_state)
        except Exception as exc:
            raise RuntimeError(
                "Training requires lightgbm or scikit-learn. Install project dependencies first."
            ) from exc

    if y_class.nunique(dropna=True) < 2:
        from sklearn.dummy import DummyClassifier

        classifier = DummyClassifier(strategy="constant", constant=int(y_class.iloc[0]))
    if y_reg.nunique(dropna=True) < 2:
        from sklearn.dummy import DummyRegressor

        regressor = DummyRegressor(strategy="mean")
    return classifier, regressor


def _predict_probability(classifier, x: pd.DataFrame) -> np.ndarray:
    if hasattr(classifier, "predict_proba"):
        proba = classifier.predict_proba(x)
        classes = list(getattr(classifier, "classes_", []))
        if proba.shape[1] == 1:
            return np.full(len(x), 1.0 if classes == [1] else 0.0)
        if 1 in classes:
            positive_index = classes.index(1)
            return np.asarray(proba[:, positive_index], dtype=float)
        return np.asarray(proba[:, 1], dtype=float)
    score = classifier.decision_function(x)
    return 1.0 / (1.0 + np.exp(-np.asarray(score, dtype=float)))
