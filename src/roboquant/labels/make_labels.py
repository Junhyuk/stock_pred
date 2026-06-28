from __future__ import annotations

import numpy as np
import pandas as pd


def compute_labels(
    prices: pd.DataFrame,
    benchmark: pd.DataFrame | None,
    horizons: dict[str, int],
) -> pd.DataFrame:
    """Create future-return labels for each horizon without altering features."""
    price_frame = _prepare_price_frame(prices)
    if price_frame.empty:
        return _empty_labels()

    benchmark_frame = _prepare_benchmark(benchmark, price_frame)
    label_frames: list[pd.DataFrame] = []

    for horizon, days in horizons.items():
        frame = price_frame.copy()
        grouped = frame.groupby("symbol", group_keys=False)
        frame["future_close"] = grouped["close"].shift(-int(days))
        frame["future_return"] = frame["future_close"] / frame["close"] - 1.0
        frame["max_drawdown_forward"] = grouped["close"].apply(
            lambda series, h=int(days): _forward_max_drawdown(series, h)
        )

        bench = benchmark_frame.copy()
        bench["benchmark_future_close"] = bench["close"].shift(-int(days))
        bench["benchmark_return"] = bench["benchmark_future_close"] / bench["close"] - 1.0
        frame = frame.merge(
            bench[["date", "benchmark_return"]],
            on="date",
            how="left",
            validate="many_to_one",
        )
        frame["excess_return"] = frame["future_return"] - frame["benchmark_return"]
        frame["rank_quantile"] = frame.groupby("date")["excess_return"].transform(
            lambda series: series.rank(pct=True)
        )
        top_rank = frame.groupby("date")["excess_return"].transform(
            lambda series: series.rank(ascending=False, method="first")
        )
        bottom_rank = frame.groupby("date")["excess_return"].transform(
            lambda series: series.rank(ascending=True, method="first")
        )
        valid_count = frame.groupby("date")["excess_return"].transform("count")
        top_cutoff = np.ceil(valid_count * 0.2).clip(lower=1)
        bottom_cutoff = np.ceil(valid_count * 0.2).clip(lower=1)
        frame["is_top20pct"] = top_rank <= top_cutoff
        frame["is_bottom20pct"] = bottom_rank <= bottom_cutoff
        frame["horizon"] = horizon
        frame["horizon_days"] = int(days)
        frame = frame.rename(columns={"date": "asof_date"})
        label_frames.append(
            frame[
                [
                    "asof_date",
                    "symbol",
                    "horizon",
                    "horizon_days",
                    "future_return",
                    "benchmark_return",
                    "excess_return",
                    "rank_quantile",
                    "is_top20pct",
                    "is_bottom20pct",
                    "max_drawdown_forward",
                ]
            ]
        )

    labels = pd.concat(label_frames, ignore_index=True)
    return labels.sort_values(["asof_date", "symbol", "horizon"]).reset_index(drop=True)


def build_equal_weight_benchmark(prices: pd.DataFrame) -> pd.DataFrame:
    """Create a simple equal-weight benchmark when no index data is available."""
    if {"date", "symbol", "close"}.issubset(prices.columns) and "adj_close" not in prices.columns:
        price_frame = prices.copy()
        price_frame["date"] = pd.to_datetime(price_frame["date"]).dt.date
        price_frame["symbol"] = price_frame["symbol"].astype(str).str.zfill(6)
        price_frame["close"] = pd.to_numeric(price_frame["close"], errors="coerce")
        price_frame = price_frame.dropna(subset=["date", "symbol", "close"])
    else:
        price_frame = _prepare_price_frame(prices)
    pivot = price_frame.pivot(index="date", columns="symbol", values="close").sort_index()
    returns = pivot.pct_change(fill_method=None)
    equal_weight_return = returns.mean(axis=1, skipna=True).fillna(0.0)
    benchmark_close = (1.0 + equal_weight_return).cumprod() * 100.0
    return pd.DataFrame(
        {
            "date": benchmark_close.index,
            "benchmark": "EQUAL_WEIGHT_UNIVERSE",
            "close": benchmark_close.to_numpy(),
        }
    )


def _prepare_price_frame(prices: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "symbol", "close", "adj_close"}
    missing = required.difference(prices.columns)
    if missing:
        raise ValueError(f"prices is missing required columns: {sorted(missing)}")
    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    frame["close"] = pd.to_numeric(frame["adj_close"], errors="coerce").where(
        frame["adj_close"].notna(), pd.to_numeric(frame["close"], errors="coerce")
    )
    frame = frame.dropna(subset=["date", "symbol", "close"])
    frame = frame[frame["close"] > 0]
    return frame[["date", "symbol", "close"]].sort_values(["symbol", "date"]).reset_index(
        drop=True
    )


def _prepare_benchmark(benchmark: pd.DataFrame | None, prices: pd.DataFrame) -> pd.DataFrame:
    if benchmark is None or benchmark.empty or "close" not in benchmark.columns:
        return build_equal_weight_benchmark(prices)
    frame = benchmark.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    if frame.empty:
        return build_equal_weight_benchmark(prices)
    return frame[["date", "close"]].drop_duplicates("date").sort_values("date").reset_index(
        drop=True
    )


def _forward_max_drawdown(close: pd.Series, horizon: int) -> pd.Series:
    values = close.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    for idx in range(0, max(0, len(values) - horizon)):
        window = values[idx : idx + horizon + 1]
        if not np.isfinite(window).all():
            continue
        running_max = np.maximum.accumulate(window)
        drawdown = window / running_max - 1.0
        out[idx] = float(np.min(drawdown))
    return pd.Series(out, index=close.index)


def _empty_labels() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "asof_date",
            "symbol",
            "horizon",
            "horizon_days",
            "future_return",
            "benchmark_return",
            "excess_return",
            "rank_quantile",
            "is_top20pct",
            "is_bottom20pct",
            "max_drawdown_forward",
        ]
    )
