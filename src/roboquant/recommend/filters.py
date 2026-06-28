from __future__ import annotations

import pandas as pd

DEFAULT_THRESHOLDS = {
    "min_trading_value_20d": 1_000_000_000,
    "min_listing_age_days": 180,
    "max_volatility_20d": 1.2,
    "max_ret_5d": 0.45,
    "retail_overheat_score": 0.7,
    "credit_balance_change_20d": 0.3,
    "short_ratio_change_5d": 0.5,
}


def build_exclusion_flags(
    row: pd.Series | dict,
    thresholds: dict[str, float] | None = None,
) -> list[str]:
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    flags: list[str] = []

    trading_value = _number(row, "trading_value_ma20", default=0.0)
    if trading_value < thresholds["min_trading_value_20d"]:
        flags.append("low_liquidity")
    if bool(_value(row, "is_managed", False)):
        flags.append("managed_stock")
    if bool(_value(row, "is_suspended", False)):
        flags.append("trading_suspended")
    listing_age = _number(row, "listing_age_days", default=float("inf"))
    if listing_age < thresholds["min_listing_age_days"]:
        flags.append("newly_listed")
    volatility_20d = _number(row, "volatility_20d", default=0.0)
    if volatility_20d > thresholds["max_volatility_20d"]:
        flags.append("high_volatility")
    ret_5d = _number(row, "ret_5d", default=0.0)
    if ret_5d > thresholds["max_ret_5d"]:
        flags.append("short_term_spike")
    if (
        _number(row, "credit_balance_change_20d", default=0.0)
        > thresholds["credit_balance_change_20d"]
        and _number(row, "retail_overheat_score", default=0.0)
        > thresholds["retail_overheat_score"]
    ):
        flags.append("retail_credit_overheat")
    if (
        _number(row, "short_ratio_change_5d", default=0.0) > thresholds["short_ratio_change_5d"]
        and _number(row, "foreign_net_value_5d_sum", default=0.0) < 0
    ):
        flags.append("short_foreign_sell_risk")
    return flags


def build_risk_flags(row: pd.Series | dict) -> list[str]:
    flags = []
    if _number(row, "risk_score", default=0.0) >= 0.8:
        flags.append("고변동성 또는 유동성 리스크")
    if _number(row, "rsi_14", default=0.0) >= 70:
        flags.append("RSI 단기 과열")
    if _number(row, "liquidity_score", default=1.0) < 0.2:
        flags.append("상대적 유동성 낮음")
    if _number(row, "retail_overheat_score", default=0.0) > 0.7:
        flags.append("개인 순매수 과열 가능성")
    if _number(row, "foreign_net_value_5d_sum", default=0.0) < 0 and _number(
        row, "institution_net_value_5d_sum", default=0.0
    ) < 0:
        flags.append("외국인/기관 단기 동반 순매도")
    flags.extend([f"exclude:{flag}" for flag in build_exclusion_flags(row)])
    return flags


def _value(row: pd.Series | dict, key: str, default=None):
    if isinstance(row, pd.Series):
        return row.get(key, default)
    return row.get(key, default)


def _number(row: pd.Series | dict, key: str, default: float = 0.0) -> float:
    value = _value(row, key, default)
    if pd.isna(value):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

