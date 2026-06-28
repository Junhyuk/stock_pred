from __future__ import annotations

import numpy as np
import pandas as pd


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return float("nan")
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    return float(drawdown.min())


def periods_per_year(dates: pd.Series) -> float:
    parsed = pd.to_datetime(dates).sort_values()
    if len(parsed) < 2:
        return 12.0
    median_days = parsed.diff().dt.days.dropna().median()
    if pd.isna(median_days) or median_days <= 0:
        return 12.0
    return float(365.25 / median_days)


def cagr(equity: pd.Series, dates: pd.Series) -> float:
    if equity.empty or len(equity) < 2:
        return float("nan")
    parsed = pd.to_datetime(dates).sort_values()
    years = (parsed.iloc[-1] - parsed.iloc[0]).days / 365.25
    if years <= 0:
        return float("nan")
    return float(equity.iloc[-1] ** (1.0 / years) - 1.0)


def sharpe_ratio(returns: pd.Series, dates: pd.Series) -> float:
    returns = returns.dropna()
    if returns.empty or np.isclose(returns.std(ddof=0), 0.0):
        return float("nan")
    return float(returns.mean() / returns.std(ddof=0) * np.sqrt(periods_per_year(dates)))


def summarize_equity(curve: pd.DataFrame) -> dict[str, float | int | None]:
    if curve.empty:
        return {
            "periods": 0,
            "cagr": None,
            "mdd": None,
            "sharpe": None,
            "hit_ratio": None,
            "avg_excess_return": None,
            "avg_turnover": None,
        }

    equity = curve["equity"]
    returns = curve["net_return"]
    return {
        "periods": int(len(curve)),
        "cagr": _none_if_nan(cagr(equity, curve["asof_date"])),
        "mdd": _none_if_nan(max_drawdown(equity)),
        "sharpe": _none_if_nan(sharpe_ratio(returns, curve["asof_date"])),
        "hit_ratio": _none_if_nan(float((curve["net_return"] > 0).mean())),
        "avg_excess_return": _none_if_nan(float(curve["excess_return"].mean())),
        "avg_turnover": _none_if_nan(float(curve["turnover"].mean())),
    }


def _none_if_nan(value: float) -> float | None:
    if pd.isna(value):
        return None
    return float(value)

