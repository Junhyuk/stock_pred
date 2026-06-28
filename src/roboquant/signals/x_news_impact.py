from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from roboquant.config import get_horizons
from roboquant.data.loaders import load_latest_features
from roboquant.db import append_dedup_table, table_exists
from roboquant.market_outlook import (
    _load_model as _load_market_outlook_model,
    _predict_row as _predict_market_outlook_row,
    build_market_outlook_dataset,
    default_market_outlook_model_path,
    fit_market_outlook_model,
    resolve_market_outlook_asof,
)
from roboquant.models.predict import predict_from_model_path
from roboquant.models.train import baseline_feature_predictions
from roboquant.signals.news_signals import X_NEWS_TRAINING_FEATURE_COLUMNS


X_NEWS_NEUTRAL_DEFAULTS = {
    "x_news_count_24h": 0.0,
    "x_news_count_3d": 0.0,
    "x_news_negative_count_3d": 0.0,
    "x_news_negative_attention_score": 0.0,
    "x_news_bias_adjusted_sentiment_score": 0.5,
}


def refresh_x_news_prediction_impact(
    conn,
    config: dict[str, Any],
    *,
    asof_date: str = "latest",
    horizons: Sequence[str] | None = None,
) -> pd.DataFrame:
    selected = list(horizons or _impact_horizons(config))
    frames = []
    for horizon in selected:
        features = load_latest_features(conn, str(horizon), asof_date)
        if features.empty:
            continue
        target_date = pd.to_datetime(features["date"].max()).date()
        conn.execute(
            "DELETE FROM x_news_prediction_impact_daily WHERE asof_date = ? AND horizon = ?",
            [target_date, str(horizon)],
        )
        frame = build_x_news_prediction_impact_for_features(conn, config, features, str(horizon))
        if not frame.empty:
            append_dedup_table(
                conn,
                "x_news_prediction_impact_daily",
                frame,
                ["asof_date", "horizon", "symbol", "model_version"],
            )
            frames.append(frame)
    if not frames:
        return _empty_prediction_impact_frame()
    return pd.concat(frames, ignore_index=True)


def build_x_news_prediction_impact_for_features(
    conn,
    config: dict[str, Any],
    features: pd.DataFrame,
    horizon: str,
) -> pd.DataFrame:
    if features.empty or not _has_x_news_activity(features):
        return _empty_prediction_impact_frame()
    features_with_x = features.copy()
    features_without_x = neutralize_x_news_features(features_with_x)
    predictions_with_x = _predict_stock_features(config, features_with_x, horizon, suffix="with-x")
    predictions_without_x = _predict_stock_features(config, features_without_x, horizon, suffix="without-x")
    if predictions_with_x.empty or predictions_without_x.empty:
        return _empty_prediction_impact_frame()

    left = _rank_predictions(predictions_with_x, "with_x")
    right = _rank_predictions(predictions_without_x, "without_x")
    merged = left.merge(right, on=["asof_date", "symbol", "horizon"], how="inner")
    if merged.empty:
        return _empty_prediction_impact_frame()
    metadata = _symbol_metadata(conn, merged["symbol"].astype(str).str.zfill(6).unique().tolist())
    merged = merged.merge(metadata, on="symbol", how="left")
    feature_evidence = _feature_evidence(features_with_x)
    merged = merged.merge(feature_evidence, on=["asof_date", "symbol", "horizon"], how="left")

    merged["rank_delta"] = merged["rank_without_x"] - merged["rank_with_x"]
    merged["pred_prob_delta"] = merged["pred_prob_with_x"] - merged["pred_prob_without_x"]
    merged["pred_return_delta"] = merged["pred_return_with_x"] - merged["pred_return_without_x"]
    merged["long_score_delta"] = merged["long_score_with_x"] - merged["long_score_without_x"]
    merged["top20_with_x"] = merged["rank_with_x"] <= 20
    merged["top20_without_x"] = merged["rank_without_x"] <= 20
    merged["impact_level"] = merged.apply(_stock_impact_level, axis=1)
    merged["evidence_json"] = merged["x_evidence_json"].fillna("{}")
    merged["model_version"] = merged["model_version_with_x"]
    merged["created_at"] = _utcnow()

    columns = _prediction_impact_columns()
    return merged.reindex(columns=columns).sort_values(
        ["asof_date", "horizon", "impact_level", "rank_with_x", "symbol"],
        ascending=[True, True, True, True, True],
    )


def refresh_x_market_outlook_impact(
    conn,
    config: dict[str, Any],
    *,
    asof_date: str = "latest",
    model_path: str | Path | None = None,
) -> pd.DataFrame:
    target_asof = resolve_market_outlook_asof(conn, asof_date)
    conn.execute("DELETE FROM x_market_outlook_impact_daily WHERE asof_date = ?", [target_asof])
    frame = build_x_market_outlook_impact(conn, config, asof_date=asof_date, model_path=model_path)
    if not frame.empty:
        append_dedup_table(
            conn,
            "x_market_outlook_impact_daily",
            frame,
            ["asof_date", "horizon", "market", "model_version"],
        )
    return frame


def build_x_market_outlook_impact(
    conn,
    config: dict[str, Any],
    *,
    asof_date: str = "latest",
    model_path: str | Path | None = None,
) -> pd.DataFrame:
    dataset = build_market_outlook_dataset(conn, config=config, asof_date=asof_date)
    if dataset.empty:
        return _empty_market_impact_frame()
    target_asof = resolve_market_outlook_asof(conn, asof_date)
    latest = dataset[pd.to_datetime(dataset["asof_date"]).dt.date.eq(target_asof)].copy()
    if latest.empty or not _has_x_news_activity(latest):
        return _empty_market_impact_frame()

    path = Path(model_path) if model_path else default_market_outlook_model_path(config)
    model = _load_market_outlook_model(path) if path.exists() else None
    if model is None:
        model = fit_market_outlook_model(dataset)
    model_version = str(model.get("model_version") or "market-outlook-x-impact")
    rows = []
    for _, row in latest.iterrows():
        with_row = row.to_dict()
        without_row = neutralize_x_news_features(pd.DataFrame([with_row])).iloc[0].to_dict()
        forecast_with_x = _predict_market_outlook_row(with_row, model)
        forecast_without_x = _predict_market_outlook_row(without_row, model)
        rows.append(_market_impact_row(with_row, forecast_with_x, forecast_without_x, model_version))
    return pd.DataFrame(rows, columns=_market_impact_columns())


def neutralize_x_news_features(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    for column in X_NEWS_TRAINING_FEATURE_COLUMNS:
        output[column] = X_NEWS_NEUTRAL_DEFAULTS.get(column, 0.0)
    return output


def _predict_stock_features(config: dict[str, Any], features: pd.DataFrame, horizon: str, *, suffix: str) -> pd.DataFrame:
    model_path = Path(config["paths"]["model_dir"]) / horizon / "model.pkl"
    if model_path.exists():
        return predict_from_model_path(model_path, features)
    return baseline_feature_predictions(features, horizon, model_version=f"factor-baseline-{suffix}")


def _rank_predictions(predictions: pd.DataFrame, suffix: str) -> pd.DataFrame:
    frame = predictions.copy()
    frame["asof_date"] = pd.to_datetime(frame["asof_date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    frame[f"rank_{suffix}"] = (
        frame.groupby("asof_date")["pred_prob_top20"].rank(ascending=False, method="first").astype(int)
    )
    return frame.rename(
        columns={
            "pred_prob_top20": f"pred_prob_{suffix}",
            "pred_return": f"pred_return_{suffix}",
            "long_score": f"long_score_{suffix}",
            "model_version": f"model_version_{suffix}",
        }
    )[
        [
            "asof_date",
            "symbol",
            "horizon",
            f"rank_{suffix}",
            f"pred_prob_{suffix}",
            f"pred_return_{suffix}",
            f"long_score_{suffix}",
            f"model_version_{suffix}",
        ]
    ]


def _symbol_metadata(conn, symbols: list[str]) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["symbol", "market", "name"])
    base = pd.DataFrame({"symbol": [str(symbol).zfill(6) for symbol in symbols]})
    symbols = base["symbol"].tolist()
    placeholders = ", ".join(["?"] * len(symbols))
    frames = []
    try:
        frames.append(
            conn.execute(
                f"""
                SELECT symbol, market, name
                FROM current_prediction_universe
                WHERE universe_rule = 'prediction_top_market_cap'
                  AND symbol IN ({placeholders})
                """,
                symbols,
            ).fetchdf()
        )
    except Exception:
        pass
    if table_exists(conn, "symbols"):
        frames.append(
            conn.execute(
                f"SELECT symbol, market, name FROM symbols WHERE symbol IN ({placeholders})",
                symbols,
            ).fetchdf()
        )
    metadata = base.copy()
    metadata["market"] = None
    metadata["name"] = None
    for frame in frames:
        if frame.empty:
            continue
        frame = frame.copy()
        frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
        metadata = metadata.merge(frame.drop_duplicates("symbol"), on="symbol", how="left", suffixes=("", "_next"))
        for column in ("market", "name"):
            metadata[column] = metadata[column].combine_first(metadata[f"{column}_next"])
            metadata = metadata.drop(columns=[f"{column}_next"])
    return metadata.drop_duplicates("symbol")


def _feature_evidence(features: pd.DataFrame) -> pd.DataFrame:
    frame = features.copy()
    frame["asof_date"] = pd.to_datetime(frame["date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    for column in X_NEWS_TRAINING_FEATURE_COLUMNS:
        if column not in frame.columns:
            frame[column] = X_NEWS_NEUTRAL_DEFAULTS.get(column, 0.0)
    frame["x_evidence_json"] = frame.apply(
        lambda row: _json({column: _safe_float(row.get(column)) for column in X_NEWS_TRAINING_FEATURE_COLUMNS}),
        axis=1,
    )
    return frame[["asof_date", "symbol", "horizon", "x_evidence_json"]]


def _market_impact_row(
    row: dict[str, Any],
    forecast_with_x: dict[str, Any],
    forecast_without_x: dict[str, Any],
    model_version: str,
) -> dict[str, Any]:
    expected_delta = _safe_float(forecast_with_x.get("expected_return")) - _safe_float(
        forecast_without_x.get("expected_return")
    )
    up_delta = _safe_float(forecast_with_x.get("up_probability")) - _safe_float(
        forecast_without_x.get("up_probability")
    )
    evidence = {column: _safe_float(row.get(column)) for column in X_NEWS_TRAINING_FEATURE_COLUMNS}
    return {
        "asof_date": row.get("asof_date"),
        "target_date": row.get("target_date"),
        "horizon": row.get("horizon"),
        "market": row.get("market"),
        "expected_return_with_x": forecast_with_x.get("expected_return"),
        "expected_return_without_x": forecast_without_x.get("expected_return"),
        "expected_return_delta": expected_delta,
        "range_low_with_x": forecast_with_x.get("range_low"),
        "range_low_without_x": forecast_without_x.get("range_low"),
        "range_low_delta": _safe_float(forecast_with_x.get("range_low")) - _safe_float(forecast_without_x.get("range_low")),
        "range_high_with_x": forecast_with_x.get("range_high"),
        "range_high_without_x": forecast_without_x.get("range_high"),
        "range_high_delta": _safe_float(forecast_with_x.get("range_high")) - _safe_float(forecast_without_x.get("range_high")),
        "up_probability_with_x": forecast_with_x.get("up_probability"),
        "up_probability_without_x": forecast_without_x.get("up_probability"),
        "up_probability_delta": up_delta,
        "down_probability_with_x": forecast_with_x.get("down_probability"),
        "down_probability_without_x": forecast_without_x.get("down_probability"),
        "down_probability_delta": _safe_float(forecast_with_x.get("down_probability")) - _safe_float(forecast_without_x.get("down_probability")),
        "shock_probability_with_x": forecast_with_x.get("shock_probability"),
        "shock_probability_without_x": forecast_without_x.get("shock_probability"),
        "shock_probability_delta": _safe_float(forecast_with_x.get("shock_probability")) - _safe_float(forecast_without_x.get("shock_probability")),
        "impact_level": _market_impact_level(expected_delta, up_delta),
        "evidence_json": _json(evidence),
        "model_version": model_version,
        "created_at": _utcnow(),
    }


def _has_x_news_activity(frame: pd.DataFrame) -> bool:
    for column in ("x_news_count_24h", "x_news_count_3d", "x_news_negative_count_3d", "x_news_negative_attention_score"):
        if column in frame.columns and pd.to_numeric(frame[column], errors="coerce").fillna(0.0).sum() > 0:
            return True
    return False


def _impact_horizons(config: dict[str, Any]) -> list[str]:
    configured = config.get("pipeline", {}).get("report_horizons") or ["2M", "3M"]
    horizons = get_horizons(config)
    selected = [str(horizon) for horizon in configured if str(horizon) in horizons and int(horizons[str(horizon)]) < 126]
    return selected or [horizon for horizon, days in horizons.items() if int(days) < 126]


def _stock_impact_level(row: pd.Series) -> str:
    prob_delta = abs(_safe_float(row.get("pred_prob_delta")))
    rank_delta = abs(int(row.get("rank_delta") or 0))
    if prob_delta >= 0.05 or rank_delta >= 5:
        return "high"
    if prob_delta >= 0.02 or rank_delta >= 2:
        return "medium"
    return "low"


def _market_impact_level(expected_delta: float, up_delta: float) -> str:
    if abs(expected_delta) >= 0.005 or abs(up_delta) >= 0.05:
        return "high"
    if abs(expected_delta) >= 0.002 or abs(up_delta) >= 0.02:
        return "medium"
    return "low"


def _prediction_impact_columns() -> list[str]:
    return [
        "asof_date",
        "horizon",
        "symbol",
        "market",
        "name",
        "rank_with_x",
        "rank_without_x",
        "rank_delta",
        "pred_prob_with_x",
        "pred_prob_without_x",
        "pred_prob_delta",
        "pred_return_with_x",
        "pred_return_without_x",
        "pred_return_delta",
        "long_score_with_x",
        "long_score_without_x",
        "long_score_delta",
        "top20_with_x",
        "top20_without_x",
        "impact_level",
        "evidence_json",
        "model_version",
        "created_at",
    ]


def _market_impact_columns() -> list[str]:
    return [
        "asof_date",
        "target_date",
        "horizon",
        "market",
        "expected_return_with_x",
        "expected_return_without_x",
        "expected_return_delta",
        "range_low_with_x",
        "range_low_without_x",
        "range_low_delta",
        "range_high_with_x",
        "range_high_without_x",
        "range_high_delta",
        "up_probability_with_x",
        "up_probability_without_x",
        "up_probability_delta",
        "down_probability_with_x",
        "down_probability_without_x",
        "down_probability_delta",
        "shock_probability_with_x",
        "shock_probability_without_x",
        "shock_probability_delta",
        "impact_level",
        "evidence_json",
        "model_version",
        "created_at",
    ]


def _empty_prediction_impact_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_prediction_impact_columns())


def _empty_market_impact_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_market_impact_columns())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    if pd.isna(result):
        return float(default)
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
