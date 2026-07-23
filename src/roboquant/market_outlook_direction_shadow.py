"""Purged walk-forward diagnostics for the KOSPI next-session direction.

This module is deliberately shadow-only.  It makes the implicit 65:35
index/breadth blend and direction threshold comparable with an always-up
baseline and a domestic-index-only Ridge while keeping the production
``market-outlook-v2`` artifact untouched.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd

from roboquant.db import append_dedup_table, table_exists
from roboquant.market_outlook import (
    DOMESTIC_INDEX_FEATURE_COLUMNS,
    _fit_market_horizon_spec,
    _linear_predict,
    _market_outlook_settings,
    build_market_outlook_dataset,
)

MODEL_NAME = "market-outlook-direction-wfo-shadow-v1"
MODEL_VERSION = "market-outlook-direction-wfo-shadow-v1"
MARKET = "KOSPI"
HORIZON = "TODAY"

OBSERVATION_COLUMNS = [
    "run_id",
    "asof_date",
    "target_date",
    "candidate_name",
    "candidate_source",
    "selected",
    "selection_source",
    "train_end_date",
    "purge_days",
    "expected_return",
    "predicted_direction",
    "actual_return",
    "actual_direction",
    "direction_hit",
    "actionable",
    "index_weight",
    "breadth_weight",
    "direction_threshold",
    "rolling_direction_accuracy",
    "rolling_action_coverage",
    "conformal_low",
    "conformal_high",
    "conformal_covered",
    "conformal_width",
    "conformal_calibration_count",
    "created_at",
]


def market_outlook_direction_shadow_settings(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Normalize only the diagnostics settings; never return a production weight."""
    raw = dict((config or {}).get("market_outlook_direction_shadow") or {})
    production = _market_outlook_settings(dict(config or {}))
    defaults: dict[str, Any] = {
        "enabled": True,
        "initial_train_days": 252,
        "purge_days": 1,
        "retrain_step_days": 21,
        "selection_window": 126,
        "min_selection_observations": 60,
        "min_oos_rows": 252,
        "rolling_window": 60,
        "conformal_window": 252,
        "min_conformal_observations": 60,
        "conformal_coverage": 0.80,
        "min_action_coverage": 0.75,
        # Keep interval gates aligned with the existing Nasdaq/SOX shadow
        # policy.  Passing direction alone never makes a candidate eligible
        # for promotion.
        "coverage_lower_bound": 0.75,
        "coverage_upper_bound": 0.85,
        "max_average_width_ratio": 1.10,
        "candidate_index_weights": [1.0, 0.75, 0.65, 0.50, 0.25, 0.0],
        "candidate_direction_thresholds": [0.0, 0.0025, 0.005],
        "production_index_weight": production["index_weight"],
        "production_direction_threshold": production["direction_return_threshold"],
    }
    values = {**defaults, **raw}
    for key, minimum in {
        "initial_train_days": 30,
        "purge_days": 1,
        "retrain_step_days": 1,
        "selection_window": 10,
        "min_selection_observations": 10,
        "min_oos_rows": 30,
        "rolling_window": 10,
        "conformal_window": 10,
        "min_conformal_observations": 10,
    }.items():
        values[key] = max(minimum, int(values[key]))
    values["conformal_coverage"] = min(0.99, max(0.50, float(values["conformal_coverage"])))
    values["min_action_coverage"] = min(1.0, max(0.0, float(values["min_action_coverage"])))
    values["coverage_lower_bound"] = min(1.0, max(0.0, float(values["coverage_lower_bound"])))
    values["coverage_upper_bound"] = min(1.0, max(0.0, float(values["coverage_upper_bound"])))
    if values["coverage_lower_bound"] > values["coverage_upper_bound"]:
        values["coverage_lower_bound"], values["coverage_upper_bound"] = (
            values["coverage_upper_bound"],
            values["coverage_lower_bound"],
        )
    values["max_average_width_ratio"] = max(1.0, float(values["max_average_width_ratio"]))
    values["production_index_weight"] = _weight(values["production_index_weight"])
    values["production_direction_threshold"] = max(0.0, float(values["production_direction_threshold"]))
    values["candidate_index_weights"] = _weights(values["candidate_index_weights"])
    values["candidate_direction_thresholds"] = _thresholds(values["candidate_direction_thresholds"])
    return values


def evaluate_kospi_direction_walk_forward(
    dataset: pd.DataFrame,
    settings: Mapping[str, Any],
) -> pd.DataFrame:
    """Evaluate candidates with an expanding training set separated by a purge.

    The online selector sees only outcomes from *earlier* out-of-sample rows.
    It therefore selects a blend/threshold without using the row being scored.
    """
    data = _kospi_today_rows(dataset)
    if data.empty:
        return _empty_observations()
    initial = int(settings["initial_train_days"])
    purge = int(settings["purge_days"])
    if len(data) < initial + purge + 1:
        return _empty_observations()

    candidate_specs = _candidate_specs(settings)
    candidate_history: dict[str, list[dict[str, Any]]] = {name: [] for name in candidate_specs}
    selected_history: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    index_spec: dict[str, Any] | None = None
    breadth_spec: dict[str, Any] | None = None
    train_end_date: date | None = None
    created_at = _utcnow()

    for position in range(initial + purge, len(data)):
        train_end = position - purge
        if index_spec is None or (position - (initial + purge)) % int(settings["retrain_step_days"]) == 0:
            train = data.iloc[:train_end].copy()
            fitted = _fit_market_horizon_spec(train, index_weight=1.0, breadth_weight=0.0)
            index_spec = dict(fitted.get("index") or {})
            breadth_spec = dict(fitted.get("breadth") or {})
            train_end_date = _date_or_none(train.iloc[-1].get("asof_date")) if not train.empty else None
        if index_spec is None or breadth_spec is None:
            continue
        item = data.iloc[position]
        actual = _number(item.get("label_return"))
        if actual is None:
            continue
        index_expected = _linear_predict(
            item.to_dict(),
            index_spec,
            allowed_features=DOMESTIC_INDEX_FEATURE_COLUMNS,
        )
        breadth_expected = _linear_predict(item.to_dict(), breadth_spec)
        if not math.isfinite(index_expected):
            index_expected = 0.0
        if not math.isfinite(breadth_expected):
            breadth_expected = 0.0

        point_rows: dict[str, dict[str, Any]] = {}
        for name, spec in candidate_specs.items():
            expected, direction = _candidate_prediction(
                name,
                spec,
                index_expected=index_expected,
                breadth_expected=breadth_expected,
            )
            point = _observation(
                item,
                name=name,
                source="walk_forward",
                selected=False,
                selection_source=None,
                train_end_date=train_end_date,
                purge_days=purge,
                expected_return=expected,
                predicted_direction=direction,
                actual_return=actual,
                index_weight=spec["index_weight"],
                breadth_weight=spec["breadth_weight"],
                threshold=spec["threshold"],
                created_at=created_at,
            )
            # Candidate-specific intervals are needed for the same MAE /
            # coverage / width gate used by the existing linkage shadow.
            # The history contains only earlier OOS rows for this candidate.
            _attach_rolling_and_conformal(point, candidate_history[name], settings)
            point_rows[name] = point
            rows.append(point)

        selected_name = _select_candidate(candidate_history, settings)
        selected_source = selected_name if selected_name is not None else "production_ridge"
        selected_point = dict(point_rows[selected_source])
        selected_point["candidate_name"] = "online_selected"
        selected_point["selected"] = True
        selected_point["selection_source"] = selected_source
        _attach_rolling_and_conformal(selected_point, selected_history, settings)
        rows.append(selected_point)
        selected_history.append(selected_point)
        for name, point in point_rows.items():
            candidate_history[name].append(point)

    return pd.DataFrame(rows, columns=OBSERVATION_COLUMNS)


def run_market_outlook_direction_shadow(
    conn,
    config: Mapping[str, Any] | None = None,
    *,
    asof_date: str | date = "latest",
) -> dict[str, Any]:
    """Run and persist the KOSPI-only direction diagnostic as shadow evidence."""
    settings = market_outlook_direction_shadow_settings(config)
    if not bool(settings.get("enabled", True)):
        return {"status": "disabled", "production_weight": 0.0, "items": []}
    dataset = build_market_outlook_dataset(conn, config=dict(config or {}), asof_date=asof_date)
    observations = evaluate_kospi_direction_walk_forward(dataset, settings)
    if observations.empty:
        return {
            "status": "not_ready",
            "production_weight": 0.0,
            "settings": _json_safe(settings),
            "items": [],
        }
    run_id = f"market-outlook-direction-shadow:{uuid4()}"
    observations = observations.copy()
    observations["run_id"] = run_id
    external = _external_shadow_observations(conn, observations, settings)
    if not external.empty:
        external["run_id"] = run_id
        observations = pd.concat([observations, external], ignore_index=True)
    summaries = _summaries(observations, settings)
    _persist_shadow_run(conn, run_id, observations, summaries, settings)
    selected = next((item for item in summaries if item["candidate_name"] == "online_selected"), None)
    baseline = next((item for item in summaries if item["candidate_name"] == "always_up"), None)
    production = next((item for item in summaries if item["candidate_name"] == "production_ridge"), None)
    status, reason = _shadow_status(selected, baseline, production, settings)
    return {
        "status": status,
        "reason": reason,
        "run_id": run_id,
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "market": MARKET,
        "horizon": HORIZON,
        "production_weight": 0.0,
        "settings": _json_safe(settings),
        "items": summaries,
    }


def _kospi_today_rows(dataset: pd.DataFrame) -> pd.DataFrame:
    if dataset.empty:
        return pd.DataFrame()
    output = dataset.copy()
    output["asof_date"] = pd.to_datetime(output.get("asof_date"), errors="coerce").dt.date
    output["target_date"] = pd.to_datetime(output.get("target_date"), errors="coerce").dt.date
    output["label_return"] = pd.to_numeric(output.get("label_return"), errors="coerce")
    output = output[
        output.get("market", pd.Series(dtype=str)).astype(str).str.upper().eq(MARKET)
        & output.get("horizon", pd.Series(dtype=str)).astype(str).str.upper().eq(HORIZON)
        & output["asof_date"].notna()
        & output["target_date"].notna()
        & output["label_return"].notna()
    ].copy()
    return output.sort_values(["asof_date", "target_date"]).drop_duplicates("asof_date", keep="last").reset_index(drop=True)


def _candidate_specs(settings: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    specs: dict[str, dict[str, float]] = {
        "always_up": {"index_weight": 0.0, "breadth_weight": 0.0, "threshold": 0.0},
        "domestic_index_only": {"index_weight": 1.0, "breadth_weight": 0.0, "threshold": 0.0},
        "production_ridge": {
            "index_weight": float(settings["production_index_weight"]),
            "breadth_weight": 1.0 - float(settings["production_index_weight"]),
            "threshold": float(settings["production_direction_threshold"]),
        },
    }
    for weight in settings["candidate_index_weights"]:
        for threshold in settings["candidate_direction_thresholds"]:
            name = f"blend_i{float(weight):.3f}_t{float(threshold):.4f}"
            specs.setdefault(
                name,
                {"index_weight": float(weight), "breadth_weight": 1.0 - float(weight), "threshold": float(threshold)},
            )
    return specs


def _candidate_prediction(
    name: str,
    spec: Mapping[str, float],
    *,
    index_expected: float,
    breadth_expected: float,
) -> tuple[float, str]:
    if name == "always_up":
        return 0.0, "BULLISH"
    expected = float(spec["index_weight"] * index_expected + spec["breadth_weight"] * breadth_expected)
    return expected, _direction(expected, float(spec["threshold"]))


def _observation(
    item: pd.Series,
    *,
    name: str,
    source: str,
    selected: bool,
    selection_source: str | None,
    train_end_date: date | None,
    purge_days: int,
    expected_return: float,
    predicted_direction: str,
    actual_return: float,
    index_weight: float,
    breadth_weight: float,
    threshold: float,
    created_at: datetime,
) -> dict[str, Any]:
    actual_direction = _direction(actual_return, 0.0)
    actionable = predicted_direction != "NEUTRAL"
    return {
        "run_id": None,
        "asof_date": _date_or_none(item.get("asof_date")),
        "target_date": _date_or_none(item.get("target_date")),
        "candidate_name": name,
        "candidate_source": source,
        "selected": bool(selected),
        "selection_source": selection_source,
        "train_end_date": train_end_date,
        "purge_days": int(purge_days),
        "expected_return": float(expected_return),
        "predicted_direction": predicted_direction,
        "actual_return": float(actual_return),
        "actual_direction": actual_direction,
        "direction_hit": None if not actionable else predicted_direction == actual_direction,
        "actionable": actionable,
        "index_weight": float(index_weight),
        "breadth_weight": float(breadth_weight),
        "direction_threshold": float(threshold),
        "rolling_direction_accuracy": None,
        "rolling_action_coverage": None,
        "conformal_low": None,
        "conformal_high": None,
        "conformal_covered": None,
        "conformal_width": None,
        "conformal_calibration_count": 0,
        "created_at": created_at,
    }


def _select_candidate(
    history: Mapping[str, list[dict[str, Any]]],
    settings: Mapping[str, Any],
) -> str | None:
    ranked: list[tuple[float, float, float, str]] = []
    for name, values in history.items():
        recent = values[-int(settings["selection_window"]) :]
        actionable = [item for item in recent if bool(item.get("actionable"))]
        hits = [bool(item["direction_hit"]) for item in actionable if item.get("direction_hit") is not None]
        if len(hits) < int(settings["min_selection_observations"]):
            continue
        coverage = len(actionable) / max(1, len(recent))
        if coverage < float(settings["min_action_coverage"]):
            continue
        accuracy = float(np.mean(hits))
        mae = float(np.mean([abs(float(item["actual_return"]) - float(item["expected_return"])) for item in recent]))
        ranked.append((accuracy, -mae, coverage, name))
    if not ranked:
        return None
    return max(ranked)[3]


def _attach_rolling_and_conformal(
    point: dict[str, Any],
    history: list[dict[str, Any]],
    settings: Mapping[str, Any],
) -> None:
    recent = history[-int(settings["rolling_window"]) :]
    actionable = [item for item in recent if bool(item.get("actionable"))]
    hits = [bool(item["direction_hit"]) for item in actionable if item.get("direction_hit") is not None]
    point["rolling_direction_accuracy"] = None if not hits else float(np.mean(hits))
    point["rolling_action_coverage"] = None if not recent else float(len(actionable) / len(recent))
    calibration = history[-int(settings["conformal_window"]) :]
    residuals = [
        abs(float(item["actual_return"]) - float(item["expected_return"]))
        for item in calibration
        if _number(item.get("actual_return")) is not None and _number(item.get("expected_return")) is not None
    ]
    point["conformal_calibration_count"] = int(len(residuals))
    if len(residuals) < int(settings["min_conformal_observations"]):
        return
    # Finite-sample split-conformal quantile: only mature prior residuals are used.
    n = len(residuals)
    rank = min(n, int(math.ceil((n + 1) * float(settings["conformal_coverage"]))))
    radius = float(np.partition(np.asarray(residuals, dtype=float), rank - 1)[rank - 1])
    expected = float(point["expected_return"])
    low, high = expected - radius, expected + radius
    point["conformal_low"] = low
    point["conformal_high"] = high
    point["conformal_width"] = high - low
    point["conformal_covered"] = bool(low <= float(point["actual_return"]) <= high)


def _external_shadow_observations(
    conn,
    base_observations: pd.DataFrame,
    settings: Mapping[str, Any],
) -> pd.DataFrame:
    """Put mature premarket/Nasdaq-SOX shadows on the same target-date slice."""
    target_dates = {
        _date_or_none(value)
        for value in base_observations.loc[base_observations["candidate_name"].eq("online_selected"), "target_date"]
    }
    target_dates.discard(None)
    if not target_dates:
        return _empty_observations()
    rows: list[dict[str, Any]] = []
    created_at = _utcnow()
    if table_exists(conn, "market_us_linkage_shadow_forecasts"):
        linkage = conn.execute(
            """
            SELECT prediction_date AS target_date, asof_date, candidate_expected_return AS expected_return, actual_return
            FROM market_us_linkage_shadow_forecasts
            WHERE market = 'KOSPI' AND is_backtest = TRUE AND actual_return IS NOT NULL
            """
        ).fetchdf()
        rows.extend(
            _external_rows(linkage, "nasdaq_sox_linkage", "nasdaq_sox_shadow", target_dates, settings, created_at)
        )
    if table_exists(conn, "premarket_shadow_forecasts") and table_exists(conn, "premarket_features_daily"):
        premarket = conn.execute(
            """
            SELECT f.prediction_date AS target_date, p.asof_date,
                   p.ridge_expected_return AS expected_return, f.label_return AS actual_return
            FROM premarket_shadow_forecasts AS p
            INNER JOIN premarket_features_daily AS f
              ON f.prediction_date = p.prediction_date
             AND f.market = p.market
             AND f.forecast_stage = p.forecast_stage
             AND f.feature_version = p.feature_version
            WHERE p.market = 'KOSPI'
              AND p.forecast_stage = 'final_close'
              AND f.label_return IS NOT NULL
            """
        ).fetchdf()
        rows.extend(_external_rows(premarket, "premarket_shadow", "premarket_shadow", target_dates, settings, created_at))
    return pd.DataFrame(rows, columns=OBSERVATION_COLUMNS) if rows else _empty_observations()


def _external_rows(
    frame: pd.DataFrame,
    name: str,
    source: str,
    target_dates: set[date],
    settings: Mapping[str, Any],
    created_at: datetime,
) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    rows: list[dict[str, Any]] = []
    for record in frame.to_dict(orient="records"):
        target = _date_or_none(record.get("target_date"))
        actual = _number(record.get("actual_return"))
        expected = _number(record.get("expected_return"))
        if target not in target_dates or actual is None or expected is None:
            continue
        item = pd.Series({"asof_date": record.get("asof_date"), "target_date": target})
        rows.append(
            _observation(
                item,
                name=name,
                source=source,
                selected=False,
                selection_source=None,
                train_end_date=None,
                purge_days=int(settings["purge_days"]),
                expected_return=expected,
                predicted_direction=_direction(expected, 0.0),
                actual_return=actual,
                index_weight=0.0,
                breadth_weight=0.0,
                threshold=0.0,
                created_at=created_at,
            )
        )
    return rows


def _summaries(observations: pd.DataFrame, settings: Mapping[str, Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for name, group in observations.groupby("candidate_name", dropna=False):
        actions = group[group["actionable"].fillna(False).astype(bool)].copy()
        valid = actions[actions["direction_hit"].notna()]
        count = int(len(valid))
        accuracy = None if not count else float(valid["direction_hit"].astype(bool).mean())
        interval = _wilson_interval(int(valid["direction_hit"].astype(bool).sum()), count)
        rolling = pd.to_numeric(group["rolling_direction_accuracy"], errors="coerce").dropna()
        conformal = group[group["conformal_covered"].notna()]
        coverage = None if conformal.empty else float(conformal["conformal_covered"].astype(bool).mean())
        width = _mean_numeric(conformal.get("conformal_width"))
        summaries.append(
            {
                "candidate_name": str(name),
                "candidate_source": str(group["candidate_source"].iloc[0]),
                "oos_count": int(len(group)),
                "direction_sample_count": count,
                "direction_accuracy": accuracy,
                "direction_confidence_interval": interval,
                "action_coverage": float(len(actions) / len(group)) if len(group) else None,
                "mae": _mean_numeric(abs(group["actual_return"] - group["expected_return"])),
                "rolling_window": int(settings["rolling_window"]),
                "last_rolling_direction_accuracy": None if rolling.empty else float(rolling.iloc[-1]),
                "conformal_target_coverage": float(settings["conformal_coverage"]),
                "conformal_sample_count": int(len(conformal)),
                "conformal_coverage": coverage,
                "conformal_average_width": width,
                "production_weight": 0.0,
            }
        )
    return sorted(summaries, key=lambda item: item["candidate_name"])


def _shadow_status(
    selected: dict[str, Any] | None,
    always_up: dict[str, Any] | None,
    production: dict[str, Any] | None,
    settings: Mapping[str, Any],
) -> tuple[str, str | None]:
    if selected is None or selected.get("direction_sample_count", 0) < int(settings["min_oos_rows"]):
        return "not_ready", "insufficient purged OOS observations"
    if selected.get("action_coverage") is None or selected["action_coverage"] < float(settings["min_action_coverage"]):
        return "rejected", "selected action coverage below gate"
    for baseline in (always_up, production):
        if baseline and baseline.get("direction_accuracy") is not None and selected.get("direction_accuracy") is not None:
            if float(selected["direction_accuracy"]) <= float(baseline["direction_accuracy"]):
                return "rejected", f"does not beat {baseline['candidate_name']} direction baseline"
    if production is None:
        return "not_ready", "production Ridge comparator unavailable"
    selected_mae = _number(selected.get("mae"))
    production_mae = _number(production.get("mae"))
    if selected_mae is None or production_mae is None:
        return "not_ready", "insufficient MAE observations"
    if selected_mae > production_mae + 1e-12:
        return "rejected", "selected MAE is worse than production Ridge"
    selected_coverage = _number(selected.get("conformal_coverage"))
    if selected_coverage is None:
        return "not_ready", "insufficient conformal observations"
    if not float(settings["coverage_lower_bound"]) <= selected_coverage <= float(settings["coverage_upper_bound"]):
        return "rejected", "selected interval coverage outside gate"
    selected_width = _number(selected.get("conformal_average_width"))
    production_width = _number(production.get("conformal_average_width"))
    if selected_width is None or production_width is None:
        return "not_ready", "production interval comparator unavailable"
    if selected_width > production_width * float(settings["max_average_width_ratio"]) + 1e-12:
        return "rejected", "selected interval width exceeds production Ridge gate"
    return "accepted_shadow", None


def _persist_shadow_run(
    conn,
    run_id: str,
    observations: pd.DataFrame,
    summaries: list[dict[str, Any]],
    settings: Mapping[str, Any],
) -> None:
    if table_exists(conn, "market_outlook_direction_shadow_observations"):
        append_dedup_table(
            conn,
            "market_outlook_direction_shadow_observations",
            observations.reindex(columns=OBSERVATION_COLUMNS),
            ["run_id", "asof_date", "candidate_name"],
        )
    if not table_exists(conn, "market_outlook_direction_shadow_runs"):
        return
    created_at = _utcnow()
    run_rows = []
    for summary in summaries:
        group = observations[observations["candidate_name"].eq(summary["candidate_name"])]
        run_rows.append(
            {
                "run_id": run_id,
                "model_name": MODEL_NAME,
                "model_version": MODEL_VERSION,
                "market": MARKET,
                "horizon": HORIZON,
                "candidate_name": summary["candidate_name"],
                "start_date": _date_or_none(group["asof_date"].min()) if not group.empty else None,
                "end_date": _date_or_none(group["asof_date"].max()) if not group.empty else None,
                "oos_count": int(summary["oos_count"]),
                "purge_days": int(settings["purge_days"]),
                "rolling_window": int(settings["rolling_window"]),
                "metrics_json": json.dumps(_json_safe(summary), ensure_ascii=False, allow_nan=False),
                "selection_json": json.dumps(_json_safe(settings), ensure_ascii=False, allow_nan=False),
                "status": "shadow_only",
                "production_weight": 0.0,
                "created_at": created_at,
            }
        )
    append_dedup_table(conn, "market_outlook_direction_shadow_runs", pd.DataFrame(run_rows), ["run_id", "candidate_name"])


def _direction(value: float, threshold: float) -> str:
    if value >= threshold and (threshold > 0.0 or value > 0.0):
        return "BULLISH"
    if value <= -threshold and (threshold > 0.0 or value < 0.0):
        return "BEARISH"
    return "NEUTRAL"


def _wilson_interval(successes: int, count: int, z: float = 1.96) -> dict[str, float] | None:
    if count <= 0:
        return None
    p = successes / count
    denominator = 1.0 + z * z / count
    centre = (p + z * z / (2.0 * count)) / denominator
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * count)) / count) / denominator
    return {"lower": max(0.0, centre - margin), "upper": min(1.0, centre + margin)}


def _weight(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.65


def _weights(value: Any) -> list[float]:
    raw = value if isinstance(value, (list, tuple)) else [value]
    values = sorted({_weight(item) for item in raw}, reverse=True)
    return values or [1.0]


def _thresholds(value: Any) -> list[float]:
    raw = value if isinstance(value, (list, tuple)) else [value]
    values: set[float] = set()
    for item in raw:
        try:
            values.add(max(0.0, float(item)))
        except (TypeError, ValueError):
            continue
    return sorted(values) or [0.0]


def _mean_numeric(values: Any) -> float | None:
    series = pd.to_numeric(values, errors="coerce").dropna()
    return None if series.empty else float(series.mean())


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, float):
        return None if not math.isfinite(value) else value
    return value


def _empty_observations() -> pd.DataFrame:
    return pd.DataFrame(columns=OBSERVATION_COLUMNS)
