from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from roboquant.backtest.metrics import max_drawdown
from roboquant.db import append_dedup_table

HORIZON_DAY_MAP = {
    "1M": 20,
    "2M": 42,
    "3M": 63,
    "6M": 126,
    "9M": 189,
    "1Y": 252,
    "2Y": 504,
}


def horizon_name_from_days(horizon_days: int) -> str:
    horizon_days = int(horizon_days)
    return min(HORIZON_DAY_MAP, key=lambda key: abs(HORIZON_DAY_MAP[key] - horizon_days))


def build_backtest_results(conn, horizon_days: int, model: str | None = None) -> pd.DataFrame:
    horizon = horizon_name_from_days(horizon_days)
    predictions = _load_standard_predictions(conn, horizon)
    if predictions.empty:
        return _empty_results()
    if model:
        predictions = predictions[predictions["model_name"] == model].copy()
    labels = conn.execute(
        """
        SELECT
          asof_date AS prediction_date,
          symbol,
          horizon,
          horizon_days,
          future_return AS actual_return,
          benchmark_return,
          excess_return,
          is_top20pct AS target_is_top20pct
        FROM labels
        WHERE horizon = ?
        """,
        [horizon],
    ).fetchdf()
    if labels.empty:
        return _empty_results()
    labels["prediction_date"] = pd.to_datetime(labels["prediction_date"]).dt.date
    labels["symbol"] = labels["symbol"].astype(str).str.zfill(6)
    symbols = conn.execute("SELECT symbol, sector FROM symbols").fetchdf()
    if not symbols.empty:
        symbols["symbol"] = symbols["symbol"].astype(str).str.zfill(6)
    frame = predictions.merge(
        labels,
        on=["prediction_date", "symbol", "horizon"],
        how="inner",
    )
    if not symbols.empty:
        frame = frame.merge(symbols, on="symbol", how="left")
    else:
        frame["sector"] = None
    if frame.empty:
        return _empty_results()
    frame["rank_no"] = frame.groupby(["prediction_date", "horizon", "model_name", "model_version"])[
        "recommendation_score"
    ].rank(ascending=False, method="first")
    frame["rank_no"] = frame["rank_no"].astype(int)
    fallback_target_dates = pd.to_datetime(frame["prediction_date"]) + pd.to_timedelta(
        frame["horizon_days"].astype(int),
        unit="D",
    )
    target_dates = _target_session_dates(conn, frame)
    frame["target_date"] = target_dates.where(target_dates.notna(), fallback_target_dates.dt.date)
    frame["direction_hit"] = _direction_hits(frame["predicted_return"], frame["actual_return"])
    # Keep the legacy field, but correct its meaning to actual prediction direction hit.
    frame["is_hit"] = frame["direction_hit"]
    frame["is_outperform"] = pd.to_numeric(frame["excess_return"], errors="coerce") > 0
    # `is_top20` historically means selected by the model. Preserve that contract
    # while retaining the ground-truth label in `target_is_top20pct`.
    frame["selected_top20"] = frame["rank_no"] <= 20
    frame["is_top20"] = frame["selected_top20"]
    frame["result_id"] = frame.apply(
        lambda row: f"{row['prediction_date']}|{row['symbol']}|{row['model_name']}|{row['model_version']}|{row['horizon']}",
        axis=1,
    )
    frame["entry_price"] = np.nan
    frame["exit_price"] = np.nan
    frame["created_at"] = _utcnow()
    columns = _empty_results().columns
    return frame[columns].sort_values(["prediction_date", "model_name", "rank_no"]).reset_index(drop=True)


def summarize_model_performance(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return _empty_performance()
    frame = results.copy()
    frame["prediction_date"] = pd.to_datetime(frame["prediction_date"]).dt.date
    rows = []
    for (model_name, model_version, horizon, horizon_days), group in frame.groupby(
        ["model_name", "model_version", "horizon", "horizon_days"],
        dropna=False,
    ):
        group = group[pd.to_numeric(group["actual_return"], errors="coerce").notna()].copy()
        if group.empty:
            continue
        actual = pd.to_numeric(group["actual_return"], errors="coerce")
        benchmark = pd.to_numeric(group["benchmark_return"], errors="coerce")
        excess = pd.to_numeric(group["excess_return"], errors="coerce")
        top20 = group[group["selected_top20"].fillna(group["rank_no"] <= 20)]
        direction_hits = group["direction_hit"] if "direction_hit" in group else group["is_hit"]
        top20_direction_hits = top20["direction_hit"] if "direction_hit" in top20 else top20["is_hit"]
        target_top20 = top20.get("target_is_top20pct")
        portfolio_returns = (
            top20.assign(actual_return=pd.to_numeric(top20["actual_return"], errors="coerce"))
            .groupby("prediction_date")["actual_return"]
            .mean()
            .sort_index()
        )
        evaluation_returns = portfolio_returns.iloc[:: max(1, int(horizon_days))]
        rank_ic = calc_rank_ic(group)
        eval_date = max(group["prediction_date"]) if not group.empty else None
        rows.append(
            {
                "eval_date": eval_date,
                "model_name": model_name,
                "model_version": model_version,
                "horizon": horizon,
                "horizon_days": int(horizon_days),
                "sample_count": int(len(group)),
                "hit_ratio": _mean_bool(direction_hits),
                "direction_hit_ratio": _mean_bool(direction_hits),
                "absolute_up_rate": _mean_bool(actual > 0),
                "precision_top20": _mean_bool(target_top20),
                "top20_direction_hit_ratio": _mean_bool(top20_direction_hits),
                "top20_absolute_up_rate": _mean_bool(pd.to_numeric(top20["actual_return"], errors="coerce") > 0),
                "top20_sample_count": int(len(top20)),
                "avg_actual_return": _mean(actual),
                "avg_benchmark_return": _mean(benchmark),
                "avg_excess_return": _mean(excess),
                "median_actual_return": _median(actual),
                "win_rate": _mean_bool(group["is_outperform"]),
                "mdd": _none_if_nan(
                    max_drawdown((1.0 + evaluation_returns.fillna(0.0)).cumprod())
                ),
                "sharpe": _horizon_sharpe(evaluation_returns, int(horizon_days)),
                "rank_ic": rank_ic,
                "production_weight": _production_weight(model_name),
                "gate_status": "candidate",
                "created_at": _utcnow(),
            }
        )
    return pd.DataFrame(rows)[_empty_performance().columns]


def run_backtest_job(
    conn,
    horizon_days: int,
    model: str | None = None,
    version: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    results = build_backtest_results(conn, horizon_days, model=model)
    if version:
        results = results[results["model_version"] == version].copy()
    if from_date:
        results = results[pd.to_datetime(results["prediction_date"]) >= pd.Timestamp(from_date)].copy()
    if to_date:
        results = results[pd.to_datetime(results["prediction_date"]) <= pd.Timestamp(to_date)].copy()
    performance = summarize_model_performance(results)
    if not results.empty:
        for model_name in results["model_name"].dropna().unique():
            conn.execute(
                "DELETE FROM model_performance_daily WHERE horizon = ? AND model_name = ?",
                [horizon_name_from_days(horizon_days), model_name],
            )
    append_dedup_table(
        conn,
        "backtest_results",
        results,
        ["result_id"],
    )
    append_dedup_table(
        conn,
        "model_performance_daily",
        performance,
        ["eval_date", "model_name", "model_version", "horizon"],
    )
    return results, performance


def calc_rank_ic(df: pd.DataFrame) -> float | None:
    valid = df.dropna(subset=["recommendation_score", "actual_return"])
    if len(valid) < 5:
        return None
    corr = valid["recommendation_score"].rank().corr(valid["actual_return"].rank(), method="spearman")
    return _none_if_nan(corr)


def calc_precision_at_k(df: pd.DataFrame, k: int = 20) -> float | None:
    if df.empty:
        return None
    top = df.sort_values("recommendation_score", ascending=False).head(k)
    if "target_is_top20pct" not in top:
        return None
    return _mean_bool(top["target_is_top20pct"])


def _load_standard_predictions(conn, horizon: str) -> pd.DataFrame:
    production = conn.execute(
        """
        SELECT
          asof_date AS prediction_date,
          symbol,
          horizon,
          'lightgbm' AS model_name,
          COALESCE(model_version, 'unknown') AS model_version,
          pred_return AS predicted_return,
          pred_prob_top20 AS predicted_probability,
          pred_risk AS risk_score,
          pred_prob_top20 AS recommendation_score
        FROM predictions
        WHERE horizon = ?
        """,
        [horizon],
    ).fetchdf()
    shadow = conn.execute(
        """
        SELECT
          date AS prediction_date,
          symbol,
          horizon,
          model_name,
          COALESCE(model_version, model_name) AS model_version,
          pred_score AS predicted_return,
          pred_prob AS predicted_probability,
          risk_score,
          COALESCE(recommendation_score, pred_prob) AS recommendation_score
        FROM model_predictions
        WHERE horizon = ?
        """,
        [horizon],
    ).fetchdf()
    frame = pd.concat([production, shadow], ignore_index=True)
    if frame.empty:
        return frame
    frame["prediction_date"] = pd.to_datetime(frame["prediction_date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    frame["recommendation_score"] = pd.to_numeric(frame["recommendation_score"], errors="coerce")
    return frame.dropna(subset=["prediction_date", "symbol", "recommendation_score"])


def _production_weight(model_name: str) -> float:
    return 1.0 if model_name == "lightgbm" else 0.0


def _mean(series: pd.Series) -> float | None:
    value = pd.to_numeric(series, errors="coerce").mean()
    return _none_if_nan(value)


def _median(series: pd.Series) -> float | None:
    value = pd.to_numeric(series, errors="coerce").median()
    return _none_if_nan(value)


def _mean_bool(series: pd.Series | None) -> float | None:
    if series is None or series.empty:
        return None
    value = series.astype("boolean").astype("Float64").mean()
    return _none_if_nan(value)


def _none_if_nan(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _horizon_sharpe(returns: pd.Series, horizon_days: int) -> float | None:
    values = pd.to_numeric(returns, errors="coerce").dropna()
    if len(values) < 2 or np.isclose(values.std(ddof=0), 0.0):
        return None
    annualization = np.sqrt(252.0 / max(1, int(horizon_days)))
    return float(values.mean() / values.std(ddof=0) * annualization)


def _direction_hits(predicted: pd.Series, actual: pd.Series) -> pd.Series:
    predicted_values = pd.to_numeric(predicted, errors="coerce")
    actual_values = pd.to_numeric(actual, errors="coerce")
    valid = predicted_values.notna() & actual_values.notna()
    result = pd.Series(pd.NA, index=predicted.index, dtype="boolean")
    result.loc[valid] = np.sign(predicted_values.loc[valid]) == np.sign(actual_values.loc[valid])
    return result


def _target_session_dates(conn, frame: pd.DataFrame) -> pd.Series:
    """Resolve label horizons to the actual per-symbol trading session date."""
    if frame.empty:
        return pd.Series(dtype=object)
    symbols = sorted(frame["symbol"].dropna().astype(str).str.zfill(6).unique().tolist())
    if not symbols:
        return pd.Series(index=frame.index, dtype=object)
    placeholders = ", ".join(["?"] * len(symbols))
    first_prediction_date = pd.to_datetime(frame["prediction_date"]).min().date()
    prices = conn.execute(
        f"""
        SELECT DISTINCT date, symbol
        FROM prices_daily
        WHERE symbol IN ({placeholders})
          AND date >= ?
        ORDER BY symbol, date
        """,
        [*symbols, first_prediction_date],
    ).fetchdf()
    if prices.empty:
        return pd.Series(index=frame.index, dtype=object)
    prices["date"] = pd.to_datetime(prices["date"]).dt.date
    prices["symbol"] = prices["symbol"].astype(str).str.zfill(6)
    sessions_by_symbol = {
        symbol: group["date"].tolist()
        for symbol, group in prices.groupby("symbol", sort=False)
    }
    target_dates: list[object] = []
    for row in frame[["prediction_date", "symbol", "horizon_days"]].itertuples(index=False):
        sessions = sessions_by_symbol.get(str(row.symbol).zfill(6), [])
        prediction_date = pd.to_datetime(row.prediction_date).date()
        start_index = bisect_right(sessions, prediction_date)
        target_index = start_index + int(row.horizon_days) - 1
        target_dates.append(sessions[target_index] if target_index < len(sessions) else None)
    return pd.Series(target_dates, index=frame.index, dtype=object)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _empty_results() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "result_id",
            "prediction_date",
            "target_date",
            "symbol",
            "model_name",
            "model_version",
            "horizon",
            "horizon_days",
            "entry_price",
            "exit_price",
            "actual_return",
            "predicted_return",
            "predicted_probability",
            "recommendation_score",
            "benchmark_return",
            "excess_return",
            "is_hit",
            "direction_hit",
            "is_outperform",
            "is_top20",
            "target_is_top20pct",
            "selected_top20",
            "rank_no",
            "sector",
            "created_at",
        ]
    )


def _empty_performance() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "eval_date",
            "model_name",
            "model_version",
            "horizon",
            "horizon_days",
            "sample_count",
            "hit_ratio",
            "direction_hit_ratio",
            "absolute_up_rate",
            "precision_top20",
            "top20_direction_hit_ratio",
            "top20_absolute_up_rate",
            "top20_sample_count",
            "avg_actual_return",
            "avg_benchmark_return",
            "avg_excess_return",
            "median_actual_return",
            "win_rate",
            "mdd",
            "sharpe",
            "rank_ic",
            "production_weight",
            "gate_status",
            "created_at",
        ]
    )
