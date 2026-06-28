from __future__ import annotations

import numpy as np
import pandas as pd

from roboquant.utils import safe_rank_pct

FLOW_FEATURE_COLUMNS = [
    "foreign_net_value_1d_sum",
    "foreign_net_value_5d_sum",
    "foreign_net_value_20d_sum",
    "foreign_net_value_60d_sum",
    "institution_net_value_1d_sum",
    "institution_net_value_5d_sum",
    "institution_net_value_20d_sum",
    "institution_net_value_60d_sum",
    "retail_net_value_1d_sum",
    "retail_net_value_5d_sum",
    "retail_net_value_20d_sum",
    "retail_net_value_60d_sum",
    "foreign_net_20d_to_mcap",
    "institution_net_20d_to_value",
    "retail_overheat_score",
    "foreign_consecutive_buy_days",
    "institution_consecutive_buy_days",
    "supply_demand_score",
]


def compute_flow_features(
    flows: pd.DataFrame,
    prices: pd.DataFrame | None = None,
    market_metrics: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if flows is None or flows.empty:
        return pd.DataFrame(columns=["date", "symbol", *FLOW_FEATURE_COLUMNS])

    frame = _prepare_flows(flows)
    frame = _attach_price_liquidity(frame, prices)
    frame = _attach_market_cap(frame, market_metrics)
    grouped = frame.groupby("symbol", group_keys=False)

    for base_column in ("foreign_net_value", "institution_net_value", "retail_net_value"):
        for window in (1, 5, 20, 60):
            frame[f"{base_column}_{window}d_sum"] = grouped[base_column].transform(
                lambda series, w=window: series.rolling(w, min_periods=1).sum()
            )

    frame["foreign_net_20d_to_mcap"] = _safe_divide(
        frame["foreign_net_value_20d_sum"],
        frame["market_cap"],
    )
    frame["institution_net_20d_to_value"] = _safe_divide(
        frame["institution_net_value_20d_sum"],
        frame["trading_value_20d_sum"],
    )
    frame["retail_overheat_score"] = _safe_divide(
        frame["retail_net_value_20d_sum"].clip(lower=0),
        frame["trading_value_20d_sum"],
    )
    frame["foreign_consecutive_buy_days"] = grouped["foreign_net_value"].transform(
        _consecutive_positive
    )
    frame["institution_consecutive_buy_days"] = grouped["institution_net_value"].transform(
        _consecutive_positive
    )

    foreign_rank = frame.groupby("date")["foreign_net_20d_to_mcap"].transform(safe_rank_pct)
    institution_rank = frame.groupby("date")["institution_net_20d_to_value"].transform(safe_rank_pct)
    retail_overheat_rank = frame.groupby("date")["retail_overheat_score"].transform(safe_rank_pct)
    frame["supply_demand_score"] = (
        0.45 * foreign_rank.fillna(0.5)
        + 0.45 * institution_rank.fillna(0.5)
        + 0.10 * (1.0 - retail_overheat_rank.fillna(0.5))
    ).clip(0.0, 1.0)

    return frame[["date", "symbol", *FLOW_FEATURE_COLUMNS]].sort_values(["symbol", "date"])


def _prepare_flows(flows: pd.DataFrame) -> pd.DataFrame:
    frame = flows.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    for column in ("foreign_net_value", "institution_net_value", "retail_net_value"):
        frame[column] = pd.to_numeric(frame.get(column, 0.0), errors="coerce").fillna(0.0)
    return frame.sort_values(["symbol", "date"]).reset_index(drop=True)


def _attach_price_liquidity(frame: pd.DataFrame, prices: pd.DataFrame | None) -> pd.DataFrame:
    if prices is None or prices.empty:
        frame["trading_value_20d_sum"] = np.nan
        return frame
    price_frame = prices[["date", "symbol", "trading_value"]].copy()
    price_frame["date"] = pd.to_datetime(price_frame["date"]).dt.date
    price_frame["symbol"] = price_frame["symbol"].astype(str).str.zfill(6)
    price_frame["trading_value"] = pd.to_numeric(price_frame["trading_value"], errors="coerce")
    price_frame = price_frame.sort_values(["symbol", "date"])
    price_frame["trading_value_20d_sum"] = price_frame.groupby("symbol")["trading_value"].transform(
        lambda series: series.rolling(20, min_periods=1).sum()
    )
    return frame.merge(
        price_frame[["date", "symbol", "trading_value_20d_sum"]],
        on=["date", "symbol"],
        how="left",
    )


def _attach_market_cap(frame: pd.DataFrame, market_metrics: pd.DataFrame | None) -> pd.DataFrame:
    if market_metrics is None or market_metrics.empty:
        frame["market_cap"] = np.nan
        return frame
    metrics = market_metrics[["date", "symbol", "market_cap"]].copy()
    metrics["date"] = pd.to_datetime(metrics["date"]).dt.date
    metrics["symbol"] = metrics["symbol"].astype(str).str.zfill(6)
    metrics["market_cap"] = pd.to_numeric(metrics["market_cap"], errors="coerce")
    return frame.merge(metrics, on=["date", "symbol"], how="left")


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = pd.to_numeric(denominator, errors="coerce").replace(0, np.nan)
    return pd.to_numeric(numerator, errors="coerce") / denominator


def _consecutive_positive(series: pd.Series) -> pd.Series:
    count = 0
    out: list[int] = []
    for value in series.fillna(0.0):
        if value > 0:
            count += 1
        else:
            count = 0
        out.append(count)
    return pd.Series(out, index=series.index, dtype="int64")

