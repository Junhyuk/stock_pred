from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from roboquant.labels.make_labels import build_equal_weight_benchmark

HORIZON_MONTHS = {
    "1m": 1,
    "3m": 3,
    "6m": 6,
    "12m": 12,
    "24m": 24,
}


def compute_target_price_features(reports: pd.DataFrame) -> pd.DataFrame:
    if reports.empty:
        return pd.DataFrame()

    frame = reports.copy()
    frame["report_date"] = pd.to_datetime(frame["report_date"], errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    for column in ("target_price", "previous_target_price", "current_price_at_report"):
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")

    frame["target_change_pct"] = pd.to_numeric(frame.get("target_change_pct"), errors="coerce")
    frame["target_change_pct"] = frame["target_change_pct"].where(
        frame["target_change_pct"].notna(),
        (frame["target_price"] / frame["previous_target_price"] - 1.0) * 100.0,
    )
    frame["upside_pct_at_report"] = pd.to_numeric(frame.get("upside_pct_at_report"), errors="coerce")
    frame["upside_pct_at_report"] = frame["upside_pct_at_report"].where(
        frame["upside_pct_at_report"].notna(),
        (frame["target_price"] / frame["current_price_at_report"] - 1.0) * 100.0,
    )
    frame["target_upgrade_flag"] = frame["target_change_pct"] >= 5.0
    frame["target_downgrade_flag"] = frame["target_change_pct"] <= -5.0
    frame["new_coverage_flag"] = frame["previous_target_price"].isna() | _contains_new_coverage(frame)
    frame["large_upside_flag"] = frame["upside_pct_at_report"] >= 30.0
    frame["extreme_upside_flag"] = frame["upside_pct_at_report"] >= 70.0
    frame["target_upside_score"] = _scale(frame["upside_pct_at_report"], low=-20.0, high=60.0)
    frame["target_revision_score"] = _scale(frame["target_change_pct"], low=-30.0, high=30.0)
    frame.loc[frame["extreme_upside_flag"], "target_upside_score"] *= 0.75
    return frame


def compute_analyst_report_outcomes(
    reports: pd.DataFrame,
    prices: pd.DataFrame,
    benchmark: pd.DataFrame | None = None,
) -> pd.DataFrame:
    reports_frame = compute_target_price_features(reports)
    price_frame = _prepare_prices(prices)
    if reports_frame.empty or price_frame.empty:
        return _empty_outcomes()

    benchmark_frame = _prepare_benchmark(benchmark, price_frame)
    price_by_symbol = {
        symbol: group.sort_values("date").reset_index(drop=True)
        for symbol, group in price_frame.groupby("symbol")
    }
    outcome_rows: list[dict] = []

    for _, report in reports_frame.dropna(subset=["report_id", "report_date", "symbol"]).iterrows():
        symbol = str(report["symbol"]).zfill(6)
        symbol_prices = price_by_symbol.get(symbol)
        if symbol_prices is None or symbol_prices.empty:
            continue

        report_date = pd.Timestamp(report["report_date"])
        base_price, base_date = _nearest_price(symbol_prices, report_date)
        if not np.isfinite(base_price) or base_price <= 0:
            base_price = _safe_float(report.get("current_price_at_report"))
            base_date = report_date
        target_price = _safe_float(report.get("target_price"))

        row = {
            "report_id": report["report_id"],
            "symbol": symbol,
            "report_date": report_date.date(),
            "target_price": target_price if np.isfinite(target_price) else np.nan,
            "updated_at": _utcnow(),
        }
        future_prices: dict[str, float] = {}
        future_returns: dict[str, float] = {}
        benchmark_returns: dict[str, float] = {}

        for horizon, months in HORIZON_MONTHS.items():
            future_date = report_date + pd.DateOffset(months=months)
            future_price, _ = _nearest_price(symbol_prices, future_date)
            future_prices[horizon] = future_price
            future_return = future_price / base_price - 1.0 if base_price > 0 and np.isfinite(future_price) else np.nan
            future_returns[horizon] = future_return
            row[f"price_{horizon}"] = future_price
            row[f"return_{horizon}"] = future_return

            bench_return = _benchmark_return(benchmark_frame, report_date, future_date)
            benchmark_returns[horizon] = bench_return
            if horizon in {"3m", "6m", "12m"}:
                row[f"benchmark_return_{horizon}"] = bench_return
                row[f"excess_return_{horizon}"] = future_return - bench_return

        row["max_drawdown_3m"] = _forward_drawdown(symbol_prices, pd.Timestamp(base_date), report_date + pd.DateOffset(months=3))
        row["max_drawdown_6m"] = _forward_drawdown(symbol_prices, pd.Timestamp(base_date), report_date + pd.DateOffset(months=6))
        hit_date = _target_hit_date(symbol_prices, report_date, target_price)
        row["target_hit_date"] = hit_date.date() if hit_date is not None else None
        row["target_hit_days"] = (hit_date - report_date).days if hit_date is not None else None
        row["target_hit_6m"] = hit_date is not None and hit_date <= report_date + pd.DateOffset(months=6)
        row["target_hit_12m"] = hit_date is not None and hit_date <= report_date + pd.DateOffset(months=12)
        outcome_rows.append(row)

    if not outcome_rows:
        return _empty_outcomes()
    return pd.DataFrame(outcome_rows)[_empty_outcomes().columns]


def compute_analyst_scores(
    reports: pd.DataFrame,
    outcomes: pd.DataFrame,
    as_of_date: str | pd.Timestamp | None = None,
    min_reports: int = 5,
    recent_window_days: int = 365,
) -> pd.DataFrame:
    if reports.empty or outcomes.empty:
        return _empty_scores()

    reports_frame = compute_target_price_features(reports)
    outcome_frame = outcomes.copy()
    outcome_frame["report_date"] = pd.to_datetime(outcome_frame["report_date"], errors="coerce")
    merged = reports_frame.merge(
        outcome_frame.drop(columns=["symbol", "report_date", "target_price"], errors="ignore"),
        on="report_id",
        how="inner",
    )
    if merged.empty:
        return _empty_scores()

    merged["report_date"] = pd.to_datetime(merged["report_date"], errors="coerce")
    as_of = pd.Timestamp(as_of_date) if as_of_date else pd.to_datetime(merged["report_date"]).max()
    cutoff = as_of - pd.Timedelta(days=int(recent_window_days))

    merged["target_error_12m"] = (
        pd.to_numeric(merged["target_price"], errors="coerce")
        / pd.to_numeric(merged["price_12m"], errors="coerce")
        - 1.0
    )
    for horizon in ("6m", "12m"):
        predicted_direction = pd.to_numeric(merged["upside_pct_at_report"], errors="coerce") >= 0
        actual_direction = pd.to_numeric(merged[f"return_{horizon}"], errors="coerce") >= 0
        merged[f"direction_correct_{horizon}"] = predicted_direction == actual_direction

    rows: list[dict] = []
    group_columns = ["analyst_name", "broker_name"]
    for (analyst_name, broker_name), group in merged.dropna(subset=group_columns).groupby(group_columns):
        report_count = int(len(group))
        recent_count = int((group["report_date"] >= cutoff).sum())
        if report_count < int(min_reports) or recent_count == 0:
            continue

        error = pd.to_numeric(group["target_error_12m"], errors="coerce")
        rmse = float(np.sqrt(np.nanmean(np.square(error)))) if error.notna().any() else np.nan
        mae = float(np.nanmean(np.abs(error))) if error.notna().any() else np.nan
        bias = float(np.nanmean(error)) if error.notna().any() else np.nan
        std_error = float(np.nanstd(error)) if error.notna().any() else np.nan
        direction_6m = _mean_bool(group["direction_correct_6m"])
        direction_12m = _mean_bool(group["direction_correct_12m"])
        hit_6m = _mean_bool(group["target_hit_6m"])
        hit_12m = _mean_bool(group["target_hit_12m"])
        excess_6m = _mean_numeric(group["excess_return_6m"])
        excess_12m = _mean_numeric(group["excess_return_12m"])
        reliability = _reliability_score(
            direction_6m=direction_6m,
            direction_12m=direction_12m,
            hit_6m=hit_6m,
            hit_12m=hit_12m,
            excess_12m=excess_12m,
            mae=mae,
        )
        rows.append(
            {
                "analyst_name": analyst_name,
                "broker_name": broker_name,
                "as_of_date": as_of.date(),
                "report_count": report_count,
                "recent_report_count_1y": recent_count,
                "rmse_12m": rmse,
                "mae_12m": mae,
                "bias_12m": bias,
                "std_error_12m": std_error,
                "direction_accuracy_6m": direction_6m,
                "direction_accuracy_12m": direction_12m,
                "target_hit_rate_6m": hit_6m,
                "target_hit_rate_12m": hit_12m,
                "avg_excess_return_6m": excess_6m,
                "avg_excess_return_12m": excess_12m,
                "reliability_score": reliability,
                "updated_at": _utcnow(),
            }
        )

    if not rows:
        return _empty_scores()
    return pd.DataFrame(rows)[_empty_scores().columns]


def _prepare_prices(prices: pd.DataFrame) -> pd.DataFrame:
    if prices.empty:
        return pd.DataFrame()
    required = {"date", "symbol", "close"}
    missing = required.difference(prices.columns)
    if missing:
        raise ValueError(f"prices is missing required columns: {sorted(missing)}")
    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    base_close = pd.to_numeric(frame["close"], errors="coerce")
    if "adj_close" in frame.columns:
        adj_close = pd.to_numeric(frame["adj_close"], errors="coerce")
        frame["close"] = adj_close.where(adj_close.notna(), base_close)
    else:
        frame["close"] = base_close
    high = pd.to_numeric(frame["high"], errors="coerce") if "high" in frame.columns else frame["close"]
    frame["high"] = high.where(high.notna(), frame["close"])
    return frame.dropna(subset=["date", "symbol", "close"]).sort_values(["symbol", "date"]).reset_index(drop=True)


def _prepare_benchmark(benchmark: pd.DataFrame | None, prices: pd.DataFrame) -> pd.DataFrame:
    if benchmark is None or benchmark.empty or "close" not in benchmark.columns:
        benchmark = build_equal_weight_benchmark(prices[["date", "symbol", "close"]])
    frame = benchmark.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame.dropna(subset=["date", "close"]).drop_duplicates("date").sort_values("date").reset_index(drop=True)


def _nearest_price(frame: pd.DataFrame, target_date: pd.Timestamp) -> tuple[float, pd.Timestamp]:
    future = frame[frame["date"] >= target_date]
    if future.empty:
        return np.nan, pd.NaT
    row = future.iloc[0]
    return _safe_float(row.get("close")), pd.Timestamp(row["date"])


def _benchmark_return(benchmark: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float:
    start_price, _ = _nearest_price(benchmark, start)
    end_price, _ = _nearest_price(benchmark, end)
    if not np.isfinite(start_price) or not np.isfinite(end_price) or start_price <= 0:
        return np.nan
    return float(end_price / start_price - 1.0)


def _forward_drawdown(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float:
    window = frame[(frame["date"] >= start) & (frame["date"] <= end)].copy()
    close = pd.to_numeric(window["close"], errors="coerce").dropna()
    if close.empty:
        return np.nan
    running_max = close.cummax()
    return float((close / running_max - 1.0).min())


def _target_hit_date(frame: pd.DataFrame, report_date: pd.Timestamp, target_price: float) -> pd.Timestamp | None:
    if not np.isfinite(target_price) or target_price <= 0:
        return None
    window = frame[(frame["date"] > report_date) & (frame["date"] <= report_date + pd.DateOffset(months=24))]
    hits = window[pd.to_numeric(window["high"], errors="coerce") >= target_price]
    if hits.empty:
        return None
    return pd.Timestamp(hits.iloc[0]["date"])


def _contains_new_coverage(frame: pd.DataFrame) -> pd.Series:
    title = frame.get("report_title", pd.Series("", index=frame.index)).astype("string").fillna("")
    rating = frame.get("investment_rating", pd.Series("", index=frame.index)).astype("string").fillna("")
    text = title.str.cat(rating, sep=" ")
    return text.str.contains("신규|개시|new coverage|initiat", case=False, regex=True, na=False)


def _scale(series: pd.Series, low: float, high: float) -> pd.Series:
    value = pd.to_numeric(series, errors="coerce")
    return ((value - low) / (high - low)).clip(0.0, 1.0)


def _safe_float(value) -> float:
    try:
        if pd.isna(value):
            return np.nan
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _mean_bool(series: pd.Series) -> float:
    if series.empty:
        return np.nan
    numeric = series.astype("boolean").astype("Float64")
    return float(numeric.mean()) if numeric.notna().any() else np.nan


def _mean_numeric(series: pd.Series) -> float:
    numeric = pd.to_numeric(series, errors="coerce")
    return float(numeric.mean()) if numeric.notna().any() else np.nan


def _reliability_score(
    direction_6m: float,
    direction_12m: float,
    hit_6m: float,
    hit_12m: float,
    excess_12m: float,
    mae: float,
) -> float:
    direction_6m = 0.5 if pd.isna(direction_6m) else direction_6m
    direction_12m = 0.5 if pd.isna(direction_12m) else direction_12m
    hit_6m = 0.0 if pd.isna(hit_6m) else hit_6m
    hit_12m = 0.0 if pd.isna(hit_12m) else hit_12m
    excess_component = 0.5 if pd.isna(excess_12m) else np.clip((excess_12m + 0.2) / 0.4, 0.0, 1.0)
    error_penalty = 0.5 if pd.isna(mae) else np.clip(mae / 0.5, 0.0, 1.0)
    score = (
        0.30 * direction_12m
        + 0.15 * direction_6m
        + 0.20 * hit_12m
        + 0.10 * hit_6m
        + 0.15 * excess_component
        + 0.10 * (1.0 - error_penalty)
    )
    return float(np.clip(score, 0.0, 1.0))


def _empty_outcomes() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "report_id",
            "symbol",
            "report_date",
            "target_price",
            "price_1m",
            "price_3m",
            "price_6m",
            "price_12m",
            "price_24m",
            "return_1m",
            "return_3m",
            "return_6m",
            "return_12m",
            "return_24m",
            "benchmark_return_3m",
            "benchmark_return_6m",
            "benchmark_return_12m",
            "excess_return_3m",
            "excess_return_6m",
            "excess_return_12m",
            "max_drawdown_3m",
            "max_drawdown_6m",
            "target_hit_date",
            "target_hit_days",
            "target_hit_6m",
            "target_hit_12m",
            "updated_at",
        ]
    )


def _empty_scores() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "analyst_name",
            "broker_name",
            "as_of_date",
            "report_count",
            "recent_report_count_1y",
            "rmse_12m",
            "mae_12m",
            "bias_12m",
            "std_error_12m",
            "direction_accuracy_6m",
            "direction_accuracy_12m",
            "target_hit_rate_6m",
            "target_hit_rate_12m",
            "avg_excess_return_6m",
            "avg_excess_return_12m",
            "reliability_score",
            "updated_at",
        ]
    )
