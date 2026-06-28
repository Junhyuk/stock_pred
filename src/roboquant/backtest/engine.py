from __future__ import annotations

import pandas as pd

from roboquant.backtest.metrics import summarize_equity


def run_topk_backtest(
    scored: pd.DataFrame,
    horizon: str,
    top_k: int,
    transaction_cost_bps: float = 30.0,
    rebalance_frequency: str = "M",
    score_column: str | None = None,
) -> tuple[pd.DataFrame, dict[str, float | int | None]]:
    """Backtest top-K equal-weight picks from out-of-sample predictions."""
    if scored.empty:
        return pd.DataFrame(), summarize_equity(pd.DataFrame())

    frame = scored.copy()
    score_column = score_column or ("final_score" if "final_score" in frame.columns else "pred_prob_top20")
    required = {
        "asof_date",
        "symbol",
        "horizon",
        score_column,
        "future_return",
        "benchmark_return",
        "is_top20pct",
    }
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"backtest input is missing columns: {sorted(missing)}")

    frame = frame[frame["horizon"] == horizon].copy()
    frame["asof_date"] = pd.to_datetime(frame["asof_date"]).dt.date
    frame = frame.dropna(subset=[score_column, "future_return", "benchmark_return"])
    if frame.empty:
        return pd.DataFrame(), summarize_equity(pd.DataFrame())

    selected_dates = _select_rebalance_dates(frame["asof_date"], rebalance_frequency)
    frame = frame[frame["asof_date"].isin(selected_dates)].copy()

    rows: list[dict[str, object]] = []
    previous_symbols: set[str] | None = None
    equity = 1.0
    cost_rate = float(transaction_cost_bps) / 10000.0

    for asof_date, group in frame.groupby("asof_date", sort=True):
        top = group.sort_values(score_column, ascending=False).head(int(top_k)).copy()
        if top.empty:
            continue
        symbols = set(top["symbol"].astype(str))
        turnover = 1.0 if previous_symbols is None else _turnover(previous_symbols, symbols, top_k)
        cost = cost_rate * turnover
        gross_return = float(top["future_return"].mean())
        benchmark_return = float(top["benchmark_return"].mean())
        net_return = gross_return - cost
        excess_return = net_return - benchmark_return
        equity *= 1.0 + net_return
        rows.append(
            {
                "asof_date": asof_date,
                "horizon": horizon,
                "top_k": int(top_k),
                "gross_return": gross_return,
                "transaction_cost": cost,
                "net_return": net_return,
                "benchmark_return": benchmark_return,
                "excess_return": excess_return,
                "precision_at_k": float(top["is_top20pct"].mean()),
                "turnover": float(turnover),
                "equity": float(equity),
                "symbols": ",".join(sorted(symbols)),
            }
        )
        previous_symbols = symbols

    curve = pd.DataFrame(rows)
    summary = summarize_equity(curve)
    if not curve.empty:
        summary["precision_at_k"] = float(curve["precision_at_k"].mean())
        summary["final_equity"] = float(curve["equity"].iloc[-1])
        summary["top_k"] = int(top_k)
    return curve, summary


def attach_forward_returns(predictions: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    labels_for_join = labels.rename(columns={"asof_date": "asof_date"})
    label_columns = [
        "asof_date",
        "symbol",
        "horizon",
        "future_return",
        "benchmark_return",
        "excess_return",
        "is_top20pct",
    ]
    if "is_bottom20pct" in labels_for_join.columns:
        label_columns.append("is_bottom20pct")
    return predictions.merge(
        labels_for_join[label_columns],
        on=["asof_date", "symbol", "horizon"],
        how="left",
    )


def _select_rebalance_dates(dates: pd.Series, frequency: str) -> set:
    date_series = pd.to_datetime(pd.Series(dates).dropna().unique()).sort_values()
    if date_series.empty:
        return set()
    frequency = frequency.upper()
    if frequency in {"D", "DAILY"}:
        return set(date_series.dt.date)
    if frequency in {"W", "WEEKLY"}:
        selected = date_series.to_series().groupby(date_series.to_period("W")).max()
    elif frequency in {"Q", "QUARTERLY"}:
        selected = date_series.to_series().groupby(date_series.to_period("Q")).max()
    else:
        selected = date_series.to_series().groupby(date_series.to_period("M")).max()
    return set(selected.dt.date)


def _turnover(previous: set[str], current: set[str], top_k: int) -> float:
    if top_k <= 0:
        return 0.0
    changed = len(current.difference(previous))
    return min(1.0, changed / float(top_k))
