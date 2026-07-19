"""Experimental A/B helpers for MarketNewsFeed local-LLM features."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from roboquant.db import append_dedup_table, table_exists
from roboquant.signals.marketnewsfeed_llm import LLM_FEATURE_COLUMNS


def evaluate_shadow_gate(
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> tuple[str, str | None]:
    """Return a shadow-only gate result; never grants a production weight."""
    required = ("precision_at_k", "ic", "avg_excess_return", "mdd", "avg_turnover")
    if any(_number(candidate.get(key)) is None or _number(baseline.get(key)) is None for key in required):
        return "not_ready", "insufficient mature A/B metrics"
    failures: list[str] = []
    if float(candidate["precision_at_k"]) < float(baseline["precision_at_k"]):
        failures.append("Precision@20 worsened")
    if float(candidate["ic"]) < float(baseline["ic"]):
        failures.append("Rank IC worsened")
    if float(candidate["avg_excess_return"]) < float(baseline["avg_excess_return"]):
        failures.append("average excess return worsened")
    max_mdd_worsening = float(settings.get("max_mdd_worsening", 0.02))
    if float(candidate["mdd"]) < float(baseline["mdd"]) - max_mdd_worsening:
        failures.append("MDD worsened beyond limit")
    baseline_turnover = max(abs(float(baseline["avg_turnover"])), 0.01)
    turnover_limit = float(settings.get("max_turnover_ratio", 1.30))
    if float(candidate["avg_turnover"]) > baseline_turnover * turnover_limit:
        failures.append("turnover exceeded limit")
    return ("rejected", "; ".join(failures)) if failures else ("accepted_shadow", None)


def build_market_range_shadow(
    conn,
    *,
    model_version: str,
    min_signal_days: int,
    calibration_window: int = 60,
    min_calibration_rows: int = 20,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Fit an expanding local-LLM range experiment for KOSPI/KOSDAQ.

    A signal is matched to the same trading date's close-to-close outcome. The
    signal is already shifted by the feature builder according to the 08:00 KST
    cutoff, and each fit only uses dates before the prediction date.
    """
    empty = pd.DataFrame(
        columns=[
            "signal_date",
            "market",
            "model_version",
            "baseline_expected_return",
            "candidate_expected_return",
            "range_low",
            "range_high",
            "actual_return",
            "calibration_count",
            "coverage_status",
            "production_weight",
            "created_at",
        ]
    )
    if not table_exists(conn, "marketnewsfeed_llm_signal_daily") or not table_exists(conn, "benchmark_daily"):
        return empty, _not_ready_market_summaries(min_signal_days, "required market data is unavailable")
    signals = conn.execute(
        """
        SELECT *
        FROM marketnewsfeed_llm_signal_daily
        WHERE scope = 'market' AND symbol = 'ALL'
        QUALIFY ROW_NUMBER() OVER (PARTITION BY signal_date ORDER BY created_at DESC) = 1
        ORDER BY signal_date
        """
    ).fetchdf()
    if signals.empty:
        return empty, _not_ready_market_summaries(min_signal_days, "no local LLM signal dates")

    rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for market in ("KOSPI", "KOSDAQ"):
        benchmark = conn.execute(
            """
            SELECT date, close
            FROM benchmark_daily
            WHERE UPPER(benchmark) = ?
            ORDER BY date
            """,
            [market],
        ).fetchdf()
        joined = _market_training_frame(signals, benchmark)
        if len(joined) < min_signal_days:
            summaries.append(
                {
                    "market": market,
                    "signal_days": int(len(joined)),
                    "status": "not_ready",
                    "reason": f"requires {min_signal_days} mature signal days",
                    "coverage": None,
                }
            )
            continue
        market_rows = _expanding_market_predictions(
            joined,
            market=market,
            model_version=model_version,
            calibration_window=calibration_window,
            min_calibration_rows=min_calibration_rows,
        )
        rows.extend(market_rows)
        ready = pd.DataFrame(market_rows)
        covered = ready[ready["coverage_status"].eq("ready")]
        summaries.append(
            {
                "market": market,
                "signal_days": int(len(joined)),
                "status": "ready" if not covered.empty else "not_ready",
                "reason": None if not covered.empty else "insufficient sequential residual calibration",
                "coverage": float(
                    ((covered["actual_return"] >= covered["range_low"]) & (covered["actual_return"] <= covered["range_high"])).mean()
                )
                if not covered.empty
                else None,
            }
        )
    frame = pd.DataFrame(rows) if rows else empty
    return frame, summaries


def persist_market_range_shadow(conn, frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    append_dedup_table(
        conn,
        "marketnewsfeed_llm_market_shadow",
        frame,
        ["signal_date", "market", "model_version"],
    )
    return frame


def _market_training_frame(signals: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    if benchmark.empty:
        return pd.DataFrame()
    indexes = benchmark.copy()
    indexes["date"] = pd.to_datetime(indexes["date"], errors="coerce").dt.date
    indexes["close"] = pd.to_numeric(indexes["close"], errors="coerce")
    indexes["actual_return"] = indexes["close"].pct_change()
    source_columns = [column.replace("_score", "") for column in LLM_FEATURE_COLUMNS]
    frame = signals.copy()
    frame["signal_date"] = pd.to_datetime(frame["signal_date"], errors="coerce").dt.date
    frame = frame.merge(indexes[["date", "actual_return"]], left_on="signal_date", right_on="date", how="inner")
    for source in source_columns:
        if source not in frame.columns:
            frame[source] = 0.0
        frame[source] = pd.to_numeric(frame[source], errors="coerce").fillna(0.0)
    return frame.dropna(subset=["signal_date", "actual_return"]).sort_values("signal_date").reset_index(drop=True)


def _expanding_market_predictions(
    frame: pd.DataFrame,
    *,
    market: str,
    model_version: str,
    calibration_window: int,
    min_calibration_rows: int,
) -> list[dict[str, Any]]:
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    feature_columns = [column.replace("_score", "") for column in LLM_FEATURE_COLUMNS]
    min_train = min(max(30, len(frame) // 2), max(30, len(frame) - 1))
    prior_residuals: list[float] = []
    output: list[dict[str, Any]] = []
    for index in range(min_train, len(frame)):
        train = frame.iloc[:index]
        test = frame.iloc[index]
        baseline = float(train["actual_return"].mean())
        model = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=10.0))
        model.fit(train[feature_columns], train["actual_return"])
        candidate = float(model.predict(pd.DataFrame([test[feature_columns].to_dict()]))[0])
        calibration = prior_residuals[-int(calibration_window) :]
        if len(calibration) >= int(min_calibration_rows):
            lower = candidate + float(np.quantile(calibration, 0.10))
            upper = candidate + float(np.quantile(calibration, 0.90))
            status = "ready"
        else:
            lower = None
            upper = None
            status = "not_ready"
        actual = float(test["actual_return"])
        prior_residuals.append(actual - candidate)
        output.append(
            {
                "signal_date": test["signal_date"],
                "market": market,
                "model_version": model_version,
                "baseline_expected_return": baseline,
                "candidate_expected_return": candidate,
                "range_low": lower,
                "range_high": upper,
                "actual_return": actual,
                "calibration_count": int(len(calibration)),
                "coverage_status": status,
                "production_weight": 0.0,
                "created_at": datetime.now(UTC).replace(tzinfo=None),
            }
        )
    return output


def _not_ready_market_summaries(min_signal_days: int, reason: str) -> list[dict[str, Any]]:
    return [
        {"market": market, "signal_days": 0, "status": "not_ready", "reason": reason, "coverage": None}
        for market in ("KOSPI", "KOSDAQ")
    ]


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
