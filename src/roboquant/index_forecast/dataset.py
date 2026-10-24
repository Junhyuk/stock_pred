"""Point-in-time feature frame for KOSPI / KOSDAQ / KOSPI100 / KOSDAQ100."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from roboquant.index_forecast.indexes import flow_market_for

FEATURE_VERSION = "index_forecast_v1"
PRICE_FEATURES = [
    "return_1d",
    "return_5d",
    "return_20d",
    "volatility_5d",
    "volatility_20d",
    "ma_gap_5d",
    "ma_gap_20d",
]
OVERNIGHT_FEATURES = [
    "us_nasdaq_return_1d",
    "us_sox_return_1d",
    "us_vix_return_1d",
    "sox_nasdaq_spread_1d",
]
FX_FEATURES = ["usdkrw_return_1d"]
FLOW_FEATURES = [
    "foreign_imbalance",
    "foreign_signed_pressure",
    "foreign_net_value",
]


def build_index_forecast_dataset(
    sqlite_conn,
    *,
    index_code: str = "KOSPI",
    horizon_days: int = 1,
    include_unlabeled: bool = False,
) -> pd.DataFrame:
    prices = pd.read_sql_query(
        """
        SELECT session_date, close, high, low, volume, turnover
        FROM index_eod
        WHERE index_code = ?
        ORDER BY session_date
        """,
        sqlite_conn,
        params=[index_code],
    )
    if prices.empty:
        return pd.DataFrame()
    prices["session_date"] = pd.to_datetime(prices["session_date"], errors="coerce").dt.date
    prices = prices.dropna(subset=["session_date", "close"]).sort_values("session_date").reset_index(drop=True)
    close = pd.to_numeric(prices["close"], errors="coerce")
    prices["return_1d"] = close.pct_change()
    prices["return_5d"] = close.pct_change(5)
    prices["return_20d"] = close.pct_change(20)
    log_ret = np.log(close / close.shift(1))
    prices["volatility_5d"] = log_ret.rolling(5, min_periods=5).std()
    prices["volatility_20d"] = log_ret.rolling(20, min_periods=20).std()
    prices["ma_gap_5d"] = close / close.rolling(5, min_periods=5).mean() - 1.0
    prices["ma_gap_20d"] = close / close.rolling(20, min_periods=20).mean() - 1.0
    steps = max(1, int(horizon_days))
    prices["label_return"] = close.shift(-steps) / close - 1.0
    prices["target_date"] = prices["session_date"].shift(-steps)
    prices["asof_date"] = prices["session_date"]
    prices["index_code"] = index_code
    prices["horizon_days"] = steps

    overnight = _latest_macro_asof(
        sqlite_conn,
        asof_dates=prices["asof_date"],
        series_map={
            "US_NASDAQ": "us_nasdaq_return_1d",
            "US_SOX": "us_sox_return_1d",
            "US_VIX": "us_vix_return_1d",
        },
        strict_before=True,
    )
    fx = _latest_macro_asof(
        sqlite_conn,
        asof_dates=prices["asof_date"],
        series_map={"USDKRW": "usdkrw_return_1d"},
        strict_before=False,
    )
    flow = _foreign_flow(sqlite_conn, prices["asof_date"], market=flow_market_for(index_code))
    frame = prices.merge(overnight, on="asof_date", how="left")
    frame = frame.merge(fx, on="asof_date", how="left")
    frame = frame.merge(flow, on="asof_date", how="left")
    frame["sox_nasdaq_spread_1d"] = frame["us_sox_return_1d"] - frame["us_nasdaq_return_1d"]
    frame["feature_version"] = FEATURE_VERSION
    if include_unlabeled:
        return frame.reset_index(drop=True)
    return frame.dropna(subset=["label_return", "target_date"]).reset_index(drop=True)


def active_feature_groups(frame: pd.DataFrame) -> dict[str, list[str]]:
    def _present(columns: list[str]) -> list[str]:
        return [
            column
            for column in columns
            if column in frame.columns and pd.to_numeric(frame[column], errors="coerce").notna().any()
        ]

    groups: dict[str, list[str]] = {}
    price = _present(PRICE_FEATURES)
    if len(price) < 3:
        return groups
    groups["price"] = price
    overnight = _present(OVERNIGHT_FEATURES)
    if overnight:
        groups["price_overnight"] = [*price, *overnight]
    fx = _present(FX_FEATURES)
    if overnight and fx:
        groups["price_overnight_fx"] = [*price, *overnight, *fx]
    flow = _present(FLOW_FEATURES)
    if overnight and fx and flow:
        groups["price_overnight_fx_flow"] = [*price, *overnight, *fx, *flow]
    elif flow:
        groups["price_flow"] = [*price, *flow]
    return groups


def _latest_macro_asof(
    sqlite_conn,
    *,
    asof_dates: pd.Series,
    series_map: dict[str, str],
    strict_before: bool,
) -> pd.DataFrame:
    codes = list(series_map)
    placeholders = ", ".join("?" for _ in codes)
    raw = pd.read_sql_query(
        f"""
        SELECT series_code, observation_date, value
        FROM macro_series
        WHERE series_code IN ({placeholders})
        ORDER BY observation_date
        """,
        sqlite_conn,
        params=codes,
    )
    output = pd.DataFrame({"asof_date": pd.to_datetime(asof_dates, errors="coerce").dt.date})
    if raw.empty:
        for column in series_map.values():
            output[column] = np.nan
        return output.drop_duplicates("asof_date")
    raw["observation_date"] = pd.to_datetime(raw["observation_date"], errors="coerce").dt.date
    raw["value"] = pd.to_numeric(raw["value"], errors="coerce")
    asof_unique = sorted(set(output["asof_date"].dropna().tolist()))
    for code, column in series_map.items():
        series = raw[raw["series_code"] == code].dropna(subset=["observation_date", "value"])
        lookup: dict[Any, float] = {}
        values = list(zip(series["observation_date"].tolist(), series["value"].tolist(), strict=True))
        pointer = -1
        last = np.nan
        for asof in asof_unique:
            while pointer + 1 < len(values):
                obs_date, obs_value = values[pointer + 1]
                if obs_date < asof or (not strict_before and obs_date <= asof):
                    pointer += 1
                    last = obs_value
                else:
                    break
            lookup[asof] = last
        output[column] = output["asof_date"].map(lookup)
    return output.drop_duplicates("asof_date")


def _foreign_flow(sqlite_conn, asof_dates: pd.Series, *, market: str = "KOSPI") -> pd.DataFrame:
    output = pd.DataFrame({"asof_date": pd.to_datetime(asof_dates, errors="coerce").dt.date})
    raw = pd.read_sql_query(
        """
        SELECT session_date, buy_value, sell_value, net_value
        FROM investor_flow
        WHERE market = ? AND investor_type = '외국인'
        ORDER BY session_date
        """,
        sqlite_conn,
        params=[market],
    )
    empty = output.drop_duplicates("asof_date")
    for column in FLOW_FEATURES:
        empty[column] = np.nan
    if raw.empty:
        return empty
    raw["asof_date"] = pd.to_datetime(raw["session_date"], errors="coerce").dt.date
    buy = pd.to_numeric(raw["buy_value"], errors="coerce")
    sell = pd.to_numeric(raw["sell_value"], errors="coerce")
    net = pd.to_numeric(raw["net_value"], errors="coerce")
    churn = (buy + sell).replace(0.0, np.nan)
    raw["foreign_imbalance"] = (buy - sell) / churn
    raw["foreign_signed_pressure"] = raw["foreign_imbalance"] * np.log1p((buy + sell).clip(lower=0))
    raw["foreign_net_value"] = net
    merged = output.merge(raw[["asof_date", *FLOW_FEATURES]], on="asof_date", how="left")
    return merged.drop_duplicates("asof_date")
