from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from roboquant.data.collectors.market_investor_flows import INVESTOR_LABELS
from roboquant.db import append_dedup_table, table_exists

FEATURE_VERSION = "market_flow_v1"
TURNOVER_INVESTORS = tuple(investor for investor in INVESTOR_LABELS if investor != "기관합계")
MARKET_FLOW_FEATURE_COLUMNS = [
    "total_trading_value",
    "foreign_net_to_turnover",
    "institution_net_to_turnover",
    "pension_net_to_turnover",
    "retail_net_to_turnover",
    "foreign_retail_imbalance_to_turnover",
    "foreign_institution_net_to_turnover",
    "foreign_net_5d_zscore",
    "foreign_net_20d_zscore",
    "institution_net_5d_zscore",
    "institution_net_20d_zscore",
    "foreign_buy_streak",
    "institution_buy_streak",
    "foreign_institution_sell_pressure",
    "flow_regime",
]


def build_market_flow_features(
    flows: pd.DataFrame,
    *,
    market_days: pd.DataFrame | None = None,
    feature_version: str = FEATURE_VERSION,
) -> pd.DataFrame:
    """Build leakage-safe daily market-flow features from final raw flow rows."""

    if flows is None or flows.empty:
        return _empty_features()
    required = {"trade_date", "market", "investor", "buy_value", "net_value", "is_final"}
    if not required.issubset(flows.columns):
        return _empty_features()

    frame = flows.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.date
    frame["market"] = frame["market"].astype(str).str.upper()
    frame["investor"] = frame["investor"].astype(str).str.strip()
    frame = frame[frame["is_final"].fillna(False).astype(bool)].copy()
    frame = frame[frame["investor"].isin(INVESTOR_LABELS)].copy()
    if frame.empty:
        return _empty_features()

    buy = _pivot(frame, "buy_value")
    net = _pivot(frame, "net_value")
    valid_index = buy.index.intersection(net.index)
    if valid_index.empty:
        return _empty_features()
    buy = buy.reindex(valid_index)
    net = net.reindex(valid_index)
    for investor in INVESTOR_LABELS:
        if investor not in buy.columns or investor not in net.columns:
            return _empty_features()
    if buy[INVESTOR_LABELS].isna().any().any() or net[INVESTOR_LABELS].isna().any().any():
        return _empty_features()

    output = pd.DataFrame(index=valid_index).reset_index()
    output["total_trading_value"] = buy[list(TURNOVER_INVESTORS)].sum(axis=1).to_numpy(dtype=float)
    denominator = output["total_trading_value"].replace(0.0, np.nan)
    output["foreign_net_to_turnover"] = _divide(net["외국인"].to_numpy(dtype=float), denominator)
    output["institution_net_to_turnover"] = _divide(net["기관합계"].to_numpy(dtype=float), denominator)
    output["pension_net_to_turnover"] = _divide(net["연기금"].to_numpy(dtype=float), denominator)
    output["retail_net_to_turnover"] = _divide(net["개인"].to_numpy(dtype=float), denominator)
    output["foreign_retail_imbalance_to_turnover"] = _divide(
        (net["외국인"] - net["개인"]).to_numpy(dtype=float), denominator
    )
    output["foreign_institution_net_to_turnover"] = _divide(
        (net["외국인"] + net["기관합계"]).to_numpy(dtype=float), denominator
    )
    output = output.sort_values(["market", "trade_date"]).reset_index(drop=True)
    grouped = output.groupby("market", group_keys=False)
    for prefix in ("foreign_net", "institution_net"):
        source = f"{prefix}_to_turnover"
        for window in (5, 20):
            output[f"{prefix}_{window}d_zscore"] = grouped[source].transform(
                lambda series, period=window: _rolling_zscore(series, period)
            )
    output["foreign_buy_streak"] = grouped["foreign_net_to_turnover"].transform(_positive_streak)
    output["institution_buy_streak"] = grouped["institution_net_to_turnover"].transform(_positive_streak)
    output["foreign_institution_sell_pressure"] = (
        (output["foreign_net_20d_zscore"] <= -0.5)
        & (output["institution_net_20d_zscore"] <= -0.5)
    ).astype(float)
    output["flow_regime"] = np.select(
        [
            (output["foreign_net_20d_zscore"] <= -0.5)
            & (output["institution_net_20d_zscore"] <= -0.5),
            (output["foreign_net_20d_zscore"] >= 0.5)
            & (output["institution_net_20d_zscore"] >= 0.5),
        ],
        ["risk_off", "supportive"],
        default="neutral",
    )
    output["source_run_id"] = _latest_run_ids(frame, output)
    output["is_final"] = True
    output["available_for_prediction_date"] = _next_market_dates(output, market_days)
    output["feature_version"] = feature_version
    output["created_at"] = datetime.now(UTC).replace(tzinfo=None)
    return output[
        [
            "trade_date",
            "market",
            *MARKET_FLOW_FEATURE_COLUMNS[:-1],
            "flow_regime",
            "source_run_id",
            "is_final",
            "available_for_prediction_date",
            "feature_version",
            "created_at",
        ]
    ].sort_values(["trade_date", "market"]).reset_index(drop=True)


def build_market_flow_features_from_database(
    conn,
    *,
    feature_version: str = FEATURE_VERSION,
) -> pd.DataFrame:
    if not table_exists(conn, "market_investor_flows_daily"):
        return _empty_features()
    flows = conn.execute(
        """
        SELECT *
        FROM market_investor_flows_daily
        WHERE is_final = TRUE
        ORDER BY trade_date, market, investor, collected_at
        """
    ).fetchdf()
    market_days = _market_days(conn)
    return build_market_flow_features(
        flows,
        market_days=market_days,
        feature_version=feature_version,
    )


def refresh_market_flow_features(conn, features: pd.DataFrame) -> None:
    append_dedup_table(
        conn,
        "market_flow_features_daily",
        features,
        ["trade_date", "market", "feature_version"],
    )


def _pivot(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    prepared = frame[["trade_date", "market", "investor", column]].copy()
    prepared[column] = pd.to_numeric(prepared[column], errors="coerce")
    return prepared.pivot_table(
        index=["trade_date", "market"],
        columns="investor",
        values=column,
        aggfunc="last",
    )


def _divide(values: np.ndarray, denominator: pd.Series) -> np.ndarray:
    return np.asarray(values, dtype=float) / denominator.to_numpy(dtype=float)


def _rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window, min_periods=window).mean()
    std = series.rolling(window, min_periods=window).std(ddof=0).replace(0.0, np.nan)
    return (series - mean) / std


def _positive_streak(series: pd.Series) -> pd.Series:
    count = 0
    values: list[int] = []
    for value in pd.to_numeric(series, errors="coerce").fillna(0.0):
        count = count + 1 if value > 0 else 0
        values.append(count)
    return pd.Series(values, index=series.index, dtype="int64")


def _latest_run_ids(flows: pd.DataFrame, output: pd.DataFrame) -> pd.Series:
    if "run_id" not in flows.columns:
        return pd.Series(None, index=output.index, dtype="object")
    latest = (
        flows.sort_values("collected_at")
        .groupby(["trade_date", "market"], as_index=False)["run_id"]
        .last()
    )
    merged = output[["trade_date", "market"]].merge(
        latest,
        on=["trade_date", "market"],
        how="left",
    )
    return merged["run_id"]


def _next_market_dates(output: pd.DataFrame, market_days: pd.DataFrame | None) -> pd.Series:
    if market_days is None or market_days.empty:
        return pd.Series(pd.NaT, index=output.index, dtype="object")
    days = market_days.copy()
    days["trade_date"] = pd.to_datetime(days["trade_date"], errors="coerce").dt.date
    days["market"] = days["market"].astype(str).str.upper()
    lookup: dict[tuple[date, str], date | None] = {}
    for market, group in days.groupby("market"):
        ordered = sorted(set(group["trade_date"].dropna()))
        for index, current in enumerate(ordered):
            lookup[(current, market)] = ordered[index + 1] if index + 1 < len(ordered) else None
    return pd.Series(
        [lookup.get((row.trade_date, row.market)) for row in output.itertuples(index=False)],
        index=output.index,
        dtype="object",
    )


def _market_days(conn) -> pd.DataFrame:
    if not table_exists(conn, "benchmark_daily"):
        return pd.DataFrame(columns=["trade_date", "market"])
    frame = conn.execute(
        """
        SELECT date AS trade_date,
               CASE WHEN benchmark = '1001' THEN 'KOSPI'
                    WHEN benchmark = '2001' THEN 'KOSDAQ'
                    ELSE benchmark END AS market
        FROM benchmark_daily
        WHERE benchmark IN ('KOSPI', 'KOSDAQ', '1001', '2001')
        GROUP BY date, market
        ORDER BY date, market
        """
    ).fetchdf()
    return frame


def _empty_features() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "trade_date",
            "market",
            *MARKET_FLOW_FEATURE_COLUMNS[:-1],
            "flow_regime",
            "source_run_id",
            "is_final",
            "available_for_prediction_date",
            "feature_version",
            "created_at",
        ]
    )
