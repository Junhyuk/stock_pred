from __future__ import annotations

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
          is_top20pct AS is_top20
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
    frame["target_date"] = pd.to_datetime(frame["prediction_date"]) + pd.to_timedelta(
        frame["horizon_days"].astype(int),
        unit="D",
    )
    frame["target_date"] = frame["target_date"].dt.date
    frame["is_hit"] = pd.to_numeric(frame["actual_return"], errors="coerce") > 0
    frame["is_outperform"] = pd.to_numeric(frame["excess_return"], errors="coerce") > 0
    frame["is_top20"] = frame["rank_no"] <= 20
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
        top20 = group[group["rank_no"] <= 20]
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
                "hit_ratio": _mean_bool(group["is_hit"]),
                "precision_top20": _mean_bool(top20["is_hit"]),
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
    return _mean_bool(top["actual_return"] > 0)


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


def _mean_bool(series: pd.Series) -> float | None:
    if series.empty:
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
            "is_outperform",
            "is_top20",
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
            "precision_top20",
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
