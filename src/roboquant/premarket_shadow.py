from __future__ import annotations

import json
import warnings
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from roboquant.db import append_dedup_table, table_exists
from roboquant.market_outlook import (
    BREADTH_FEATURE_COLUMNS,
    INDEX_FEATURE_COLUMNS,
    _fit_market_horizon_spec,
    _predict_subset,
    build_market_outlook_dataset,
)

KST = ZoneInfo("Asia/Seoul")
FEATURE_VERSION = "premarket_signed_global_v1"
MODEL_VERSION_PREFIX = "premarket-shadow-v1"
PRELIMINARY_STAGE = "preliminary_after_close"
FINAL_STAGE = "final_premarket"
FORECAST_STAGES = {PRELIMINARY_STAGE, FINAL_STAGE}

GLOBAL_FEATURE_MAP = {
    "^GSPC": "us_sp500_return_1d",
    "^IXIC": "us_nasdaq_return_1d",
    "^SOX": "us_sox_return_1d",
    "^VIX": "us_vix_return_1d",
    "EWY": "us_ewy_return_1d",
    "KORU": "us_koru_return_1d",
    "USDKRW=X": "us_usdkrw_return_1d",
}
CORE_FINAL_FEATURES = (
    "us_sp500_return_1d",
    "us_nasdaq_return_1d",
    "us_sox_return_1d",
    "us_ewy_return_1d",
    "us_koru_return_1d",
)
SIGNED_GLOBAL_FEATURE_COLUMNS = [*GLOBAL_FEATURE_MAP.values(), "vix_close"]
DOMESTIC_FEATURE_COLUMNS = [*INDEX_FEATURE_COLUMNS[:7], *BREADTH_FEATURE_COLUMNS]
MODEL_FEATURE_COLUMNS = [*DOMESTIC_FEATURE_COLUMNS, *SIGNED_GLOBAL_FEATURE_COLUMNS]
FEATURE_STORE_COLUMNS = [
    "prediction_date",
    "asof_date",
    "market",
    "forecast_stage",
    "prediction_cutoff",
    "available_at",
    "global_trade_date",
    "global_age_days",
    "is_final_close",
    *MODEL_FEATURE_COLUMNS,
    "global_risk_score",
    "regime",
    "recommended_cash_ratio",
    "risk_gate_status",
    "gap_return",
    "intraday_return",
    "label_return",
    "source_timestamps_json",
    "data_quality_json",
    "feature_version",
    "created_at",
]


def build_premarket_feature_history(
    conn,
    config: dict[str, Any],
    *,
    stage: str,
    asof_date: str | date = "latest",
) -> pd.DataFrame:
    stage = _validate_stage(stage)
    dataset = build_market_outlook_dataset(conn, config=config, asof_date=asof_date)
    if dataset.empty:
        return pd.DataFrame()
    frame = dataset[dataset["horizon"].astype(str).eq("TODAY")].copy()
    if frame.empty:
        return frame

    frame["asof_date"] = pd.to_datetime(frame["asof_date"]).dt.date
    frame["prediction_date"] = pd.to_datetime(frame["target_date"]).dt.date
    benchmark = _benchmark_open_close(conn)
    if not benchmark.empty:
        frame = frame.merge(
            benchmark,
            left_on=["prediction_date", "market"],
            right_on=["date", "market"],
            how="left",
        ).drop(columns=["date"], errors="ignore")
    frame["gap_return"] = pd.to_numeric(frame.get("target_open"), errors="coerce") / pd.to_numeric(
        frame.get("index_close"), errors="coerce"
    ) - 1.0
    frame["intraday_return"] = pd.to_numeric(
        frame.get("target_close"), errors="coerce"
    ) / pd.to_numeric(frame.get("target_open"), errors="coerce") - 1.0

    global_frame = _global_feature_frame(conn)
    if global_frame.empty:
        for column in SIGNED_GLOBAL_FEATURE_COLUMNS:
            frame[column] = None
        frame["global_trade_date"] = None
        frame["global_available_at"] = None
        frame["source_timestamps_json"] = "{}"
    else:
        alignment_column = "prediction_date" if stage == FINAL_STAGE else "asof_date"
        frame["alignment_date"] = pd.to_datetime(frame[alignment_column]).astype("datetime64[ns]")
        frame = pd.merge_asof(
            frame.sort_values("alignment_date"),
            global_frame.sort_values("global_trade_date"),
            left_on="alignment_date",
            right_on="global_trade_date",
            direction="backward",
            allow_exact_matches=False,
        ).drop(columns=["alignment_date"], errors="ignore")

    frame["forecast_stage"] = stage
    frame["feature_version"] = FEATURE_VERSION
    frame["prediction_cutoff"] = frame.apply(
        lambda row: _prediction_cutoff(
            stage,
            row["asof_date"],
            row["prediction_date"],
            config,
        ),
        axis=1,
    )
    global_dates = pd.to_datetime(frame.get("global_trade_date"), errors="coerce")
    prediction_dates = pd.to_datetime(frame["prediction_date"], errors="coerce")
    frame["global_age_days"] = (prediction_dates - global_dates).dt.days
    core_ready = frame.reindex(columns=CORE_FINAL_FEATURES).notna().all(axis=1)
    frame["is_final_close"] = (
        (stage == FINAL_STAGE)
        & core_ready
        & frame["global_age_days"].between(1, 3, inclusive="both")
    )
    frame = _attach_risk_gate(conn, frame, stage)
    frame["available_at"] = pd.to_datetime(frame.get("global_available_at"), errors="coerce")
    frame["data_quality_json"] = frame.apply(_data_quality_json, axis=1)
    return (
        frame.sort_values(["asof_date", "market"])
        .drop_duplicates(
            ["prediction_date", "market", "forecast_stage", "feature_version"],
            keep="last",
        )
        .reset_index(drop=True)
    )


def refresh_premarket_feature_store(conn, frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    output = frame.copy()
    output["created_at"] = datetime.now(UTC).replace(tzinfo=None)
    for column in FEATURE_STORE_COLUMNS:
        if column not in output.columns:
            output[column] = None
    output = output[FEATURE_STORE_COLUMNS]
    append_dedup_table(
        conn,
        "premarket_features_daily",
        output,
        ["prediction_date", "market", "forecast_stage", "feature_version"],
    )
    return output


def fit_premarket_shadow_model(
    frame: pd.DataFrame,
    *,
    stage: str,
    random_state: int = 42,
) -> dict[str, Any]:
    stage = _validate_stage(stage)
    data = frame[frame["forecast_stage"].astype(str).eq(stage)].copy()
    data = data.dropna(subset=["label_return", "gap_return", "intraday_return"])
    data = data[data.reindex(columns=CORE_FINAL_FEATURES).notna().all(axis=1)]
    if stage == FINAL_STAGE:
        data = data[data["is_final_close"].fillna(False).astype(bool)]
    if data.empty:
        raise ValueError(f"No labeled premarket rows for stage={stage}")
    bundle: dict[str, Any] = {
        "model_version": f"{MODEL_VERSION_PREFIX}-{stage}-{date.today().isoformat()}",
        "feature_version": FEATURE_VERSION,
        "forecast_stage": stage,
        "feature_columns": MODEL_FEATURE_COLUMNS,
        "production_weight": 0.0,
        "models": {},
        "metrics": {},
    }
    for market, subset in data.groupby("market"):
        subset = subset.sort_values("prediction_date").reset_index(drop=True)
        if len(subset) < 30:
            continue
        split_index = min(len(subset) - 1, max(20, int(len(subset) * 0.8)))
        train = subset.iloc[:split_index].copy()
        validation = subset.iloc[split_index:].copy()
        validation_models = _fit_model_set(train, random_state=random_state)
        candidate = _predict_model_set(validation_models, validation)
        baseline_spec = _fit_market_horizon_spec(train)
        baseline = _predict_subset(validation, baseline_spec).to_numpy(dtype=float)
        metrics = _comparison_metrics(
            validation["label_return"].to_numpy(dtype=float),
            baseline,
            candidate["ridge_expected_return"],
            candidate["up_probability"],
        )
        bundle["models"][str(market)] = _fit_model_set(
            subset,
            random_state=random_state,
        )
        bundle["metrics"][str(market)] = {
            **metrics,
            "train_rows": int(len(train)),
            "validation_rows": int(len(validation)),
            "total_rows": int(len(subset)),
            "accepted": bool(
                metrics["candidate_mae"] <= metrics["baseline_mae"]
                and metrics["candidate_rmse"] <= metrics["baseline_rmse"]
                and metrics["classifier_direction_accuracy"]
                >= metrics["baseline_direction_accuracy"]
            ),
            "production_weight": 0.0,
        }
    if not bundle["models"]:
        raise ValueError(f"Insufficient premarket training rows for stage={stage}")
    return bundle


def build_premarket_shadow_forecasts(
    frame: pd.DataFrame,
    bundle: dict[str, Any],
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    stage = str(bundle["forecast_stage"])
    latest_asof = pd.to_datetime(frame["asof_date"]).max().date()
    latest = frame[pd.to_datetime(frame["asof_date"]).dt.date.eq(latest_asof)].copy()
    if stage == FINAL_STAGE:
        latest = latest[latest["is_final_close"].fillna(False).astype(bool)]
    rows: list[dict[str, Any]] = []
    created_at = datetime.now(UTC).replace(tzinfo=None)
    for _, item in latest.iterrows():
        market = str(item["market"])
        model_set = bundle.get("models", {}).get(market)
        if not model_set:
            continue
        prediction = _predict_model_set(model_set, item.to_frame().T)
        ridge_expected = float(prediction["ridge_expected_return"][0])
        ridge_gap = float(prediction["ridge_gap_return"][0])
        ridge_intraday = float(prediction["ridge_intraday_return"][0])
        component_expected = float((1.0 + ridge_gap) * (1.0 + ridge_intraday) - 1.0)
        up_probability = float(prediction["up_probability"][0])
        classifier_direction = _probability_direction(up_probability)
        ridge_direction = _return_direction(ridge_expected)
        rows.append(
            {
                "prediction_date": item["prediction_date"],
                "asof_date": item["asof_date"],
                "market": market,
                "forecast_stage": stage,
                "prediction_cutoff": item["prediction_cutoff"],
                "available_at": item.get("available_at"),
                "is_final_close": bool(item.get("is_final_close")),
                "ridge_expected_return": ridge_expected,
                "ridge_gap_return": ridge_gap,
                "ridge_intraday_return": ridge_intraday,
                "component_expected_return": component_expected,
                "up_probability": up_probability,
                "classifier_direction": classifier_direction,
                "ridge_direction": ridge_direction,
                "direction_agreement": classifier_direction == ridge_direction,
                "global_risk_score": _number(item.get("global_risk_score")),
                "regime": item.get("regime"),
                "recommended_cash_ratio": _number(item.get("recommended_cash_ratio")),
                "risk_gate_status": item.get("risk_gate_status") or "unavailable",
                "model_version": bundle["model_version"],
                "feature_version": bundle["feature_version"],
                "metrics_json": json.dumps(
                    bundle.get("metrics", {}).get(market, {}),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                "data_quality_json": item.get("data_quality_json"),
                "created_at": created_at,
            }
        )
    return pd.DataFrame(rows)


def refresh_premarket_shadow_forecasts(conn, frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    append_dedup_table(
        conn,
        "premarket_shadow_forecasts",
        frame,
        ["prediction_date", "market", "forecast_stage", "model_version"],
    )
    return frame


def save_premarket_shadow_model(bundle: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, target)


def load_premarket_shadow_model(path: str | Path) -> dict[str, Any]:
    return joblib.load(path)


def default_premarket_shadow_model_path(config: dict[str, Any], stage: str) -> Path:
    return Path(config["paths"]["model_dir"]) / "premarket_shadow" / stage / "model.joblib"


def _fit_model_set(frame: pd.DataFrame, *, random_state: int) -> dict[str, Any]:
    features = frame.reindex(columns=MODEL_FEATURE_COLUMNS)
    total_model = _ridge_pipeline()
    gap_model = _ridge_pipeline()
    intraday_model = _ridge_pipeline()
    total_model.fit(features, frame["label_return"].astype(float))
    gap_model.fit(features, frame["gap_return"].astype(float))
    intraday_model.fit(features, frame["intraday_return"].astype(float))
    direction = (frame["label_return"].astype(float) > 0.0).astype(int)
    classifier = None
    classifier_imputer = None
    constant_probability = float(direction.mean())
    if direction.nunique() > 1:
        classifier_imputer = SimpleImputer(strategy="median", keep_empty_features=True)
        classifier_features = np.asarray(
            classifier_imputer.fit_transform(_numeric_matrix(features)),
            dtype=float,
        )
        classifier = LGBMClassifier(
            objective="binary",
            n_estimators=160,
            learning_rate=0.025,
            num_leaves=7,
            max_depth=3,
            min_child_samples=20,
            reg_lambda=8.0,
            reg_alpha=1.0,
            verbosity=-1,
            random_state=random_state,
            n_jobs=4,
        )
        classifier.fit(classifier_features, direction.to_numpy(dtype=int))
    return {
        "total_regressor": total_model,
        "gap_regressor": gap_model,
        "intraday_regressor": intraday_model,
        "direction_imputer": classifier_imputer,
        "direction_classifier": classifier,
        "constant_up_probability": constant_probability,
    }


def _ridge_pipeline():
    return make_pipeline(
        SimpleImputer(strategy="median", keep_empty_features=True),
        StandardScaler(),
        Ridge(alpha=10.0),
    )


def _predict_model_set(model_set: dict[str, Any], frame: pd.DataFrame) -> dict[str, np.ndarray]:
    features = frame.reindex(columns=MODEL_FEATURE_COLUMNS)
    classifier = model_set.get("direction_classifier")
    if classifier is None:
        up_probability = np.full(
            len(frame),
            float(model_set.get("constant_up_probability", 0.5)),
        )
    else:
        classifier_features = np.asarray(
            model_set["direction_imputer"].transform(_numeric_matrix(features)),
            dtype=float,
        )
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=(
                    "X does not have valid feature names, but LGBMClassifier was fitted "
                    "with feature names"
                ),
            )
            up_probability = classifier.predict_proba(classifier_features)[:, 1]
    return {
        "ridge_expected_return": np.asarray(
            model_set["total_regressor"].predict(features),
            dtype=float,
        ),
        "ridge_gap_return": np.asarray(model_set["gap_regressor"].predict(features), dtype=float),
        "ridge_intraday_return": np.asarray(
            model_set["intraday_regressor"].predict(features),
            dtype=float,
        ),
        "up_probability": np.asarray(up_probability, dtype=float),
    }


def _comparison_metrics(
    actual: np.ndarray,
    baseline: np.ndarray,
    candidate: np.ndarray,
    up_probability: np.ndarray,
) -> dict[str, float | int]:
    tail = np.abs(actual) >= 0.03
    baseline_error = actual - baseline
    candidate_error = actual - candidate
    return {
        "rows": int(len(actual)),
        "baseline_mae": float(np.mean(np.abs(baseline_error))),
        "candidate_mae": float(np.mean(np.abs(candidate_error))),
        "baseline_rmse": float(np.sqrt(np.mean(baseline_error**2))),
        "candidate_rmse": float(np.sqrt(np.mean(candidate_error**2))),
        "baseline_direction_accuracy": float(np.mean(np.sign(baseline) == np.sign(actual))),
        "ridge_direction_accuracy": float(np.mean(np.sign(candidate) == np.sign(actual))),
        "classifier_direction_accuracy": float(
            np.mean((up_probability >= 0.5) == (actual > 0.0))
        ),
        "tail_rows": int(tail.sum()),
        "baseline_tail_mae": _masked_mae(baseline_error, tail),
        "candidate_tail_mae": _masked_mae(candidate_error, tail),
    }


def _numeric_matrix(frame: pd.DataFrame) -> np.ndarray:
    return frame.apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)


def _masked_mae(error: np.ndarray, mask: np.ndarray) -> float:
    return float(np.mean(np.abs(error[mask]))) if mask.any() else 0.0


def _probability_direction(probability: float) -> str:
    if probability >= 0.55:
        return "BULLISH"
    if probability <= 0.45:
        return "BEARISH"
    return "NEUTRAL"


def _return_direction(value: float) -> str:
    if value >= 0.005:
        return "BULLISH"
    if value <= -0.005:
        return "BEARISH"
    return "NEUTRAL"


def _attach_risk_gate(conn, frame: pd.DataFrame, stage: str) -> pd.DataFrame:
    output = frame.drop(
        columns=["global_risk_score", "regime", "recommended_cash_ratio"],
        errors="ignore",
    ).copy()
    if not table_exists(conn, "market_regime_daily"):
        output["global_risk_score"] = None
        output["regime"] = None
        output["recommended_cash_ratio"] = None
        output["risk_gate_status"] = "unavailable"
        return output
    gates = conn.execute(
        """
        SELECT prediction_date AS risk_gate_date,
               global_risk_score,
               regime,
               recommended_cash_ratio
        FROM market_regime_daily
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY prediction_date
          ORDER BY prediction_cutoff DESC, created_at DESC
        ) = 1
        """
    ).fetchdf()
    if gates.empty:
        output["global_risk_score"] = None
        output["regime"] = None
        output["recommended_cash_ratio"] = None
        output["risk_gate_status"] = "unavailable"
        return output
    gates["risk_gate_date"] = pd.to_datetime(gates["risk_gate_date"]).dt.date
    output["risk_gate_date"] = (
        output["prediction_date"] if stage == FINAL_STAGE else output["asof_date"]
    )
    output = output.merge(gates, on="risk_gate_date", how="left").drop(
        columns=["risk_gate_date"],
        errors="ignore",
    )
    output["risk_gate_status"] = output["regime"].fillna("unavailable").astype(str)
    return output


def _global_feature_frame(conn) -> pd.DataFrame:
    raw = conn.execute(
        """
        SELECT
          trade_date,
          symbol,
          return_1d,
          close,
          source_timestamp,
          ingested_at
        FROM global_market_daily
        WHERE symbol IN ('^GSPC', '^IXIC', '^SOX', '^VIX', 'EWY', 'KORU', 'USDKRW=X')
        ORDER BY trade_date, symbol, ingested_at
        """
    ).fetchdf()
    if raw.empty:
        return raw
    raw["trade_date"] = pd.to_datetime(raw["trade_date"]).dt.date
    raw = raw.drop_duplicates(["trade_date", "symbol"], keep="last")
    rows: list[dict[str, Any]] = []
    for trade_date, group in raw.groupby("trade_date", sort=True):
        values: dict[str, Any] = {
            "global_trade_date": pd.Timestamp(trade_date),
            "global_available_at": _inferred_global_available_at(trade_date),
        }
        timestamps: dict[str, Any] = {}
        for _, item in group.iterrows():
            symbol = str(item["symbol"])
            feature = GLOBAL_FEATURE_MAP.get(symbol)
            if feature:
                values[feature] = _number(item.get("return_1d"))
            if symbol == "^VIX":
                values["vix_close"] = _number(item.get("close"))
            timestamps[symbol] = {
                "trade_date": str(trade_date),
                "source_timestamp": _timestamp_string(item.get("source_timestamp")),
                "ingested_at": _timestamp_string(item.get("ingested_at")),
            }
        values["source_timestamps_json"] = json.dumps(
            timestamps,
            ensure_ascii=False,
            allow_nan=False,
        )
        rows.append(values)
    frame = pd.DataFrame(rows)
    for column in SIGNED_GLOBAL_FEATURE_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    core_ready = frame.reindex(columns=CORE_FINAL_FEATURES).notna().all(axis=1)
    return frame[core_ready].sort_values("global_trade_date").reset_index(drop=True)


def _benchmark_open_close(conn) -> pd.DataFrame:
    frame = conn.execute(
        """
        SELECT date, benchmark AS market, open AS target_open, close AS target_close
        FROM benchmark_daily
        WHERE benchmark IN ('KOSPI', 'KOSDAQ')
        ORDER BY date, benchmark
        """
    ).fetchdf()
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def _prediction_cutoff(
    stage: str,
    asof_date: date,
    prediction_date: date,
    config: dict[str, Any],
) -> datetime:
    cutoff_date = prediction_date if stage == FINAL_STAGE else asof_date
    raw = config.get("premarket_shadow", {})
    key = "final_cutoff_kst" if stage == FINAL_STAGE else "preliminary_cutoff_kst"
    default = "08:00" if stage == FINAL_STAGE else "18:00"
    hour, minute = [int(part) for part in str(raw.get(key, default)).split(":", maxsplit=1)]
    cutoff_time = time(hour, minute)
    return datetime.combine(cutoff_date, cutoff_time, tzinfo=KST).replace(tzinfo=None)


def _inferred_global_available_at(trade_date: date) -> datetime:
    return datetime.combine(trade_date + timedelta(days=1), time(6, 30), tzinfo=KST).replace(
        tzinfo=None
    )


def _data_quality_json(row: pd.Series) -> str:
    missing = [column for column in MODEL_FEATURE_COLUMNS if pd.isna(row.get(column))]
    stage_ready = row.get("forecast_stage") == PRELIMINARY_STAGE or bool(
        row.get("is_final_close")
    )
    payload = {
        "status": "ready" if not missing and stage_ready else "partial_ready",
        "forecast_stage": row.get("forecast_stage"),
        "is_final_close": bool(row.get("is_final_close")),
        "global_trade_date": _date_string(row.get("global_trade_date")),
        "global_age_days": _number(row.get("global_age_days")),
        "missing_features": missing,
        "availability_basis": "US daily close inferred available at 06:30 KST; live source timestamps retained",
    }
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def _validate_stage(stage: str) -> str:
    value = str(stage or "").strip().lower()
    if value not in FORECAST_STAGES:
        raise ValueError(f"Unsupported forecast stage: {stage}")
    return value


def _number(value: Any) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(parsed) else float(parsed)


def _timestamp_string(value: Any) -> str | None:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.isoformat()


def _date_string(value: Any) -> str | None:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date().isoformat()