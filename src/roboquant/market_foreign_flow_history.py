"""Leakage-safe KOSPI foreign cash/futures history review (sequential 7).

Cash foreign buy/sell can be used for the next KRX session after the 18:20 KST
final cutoff. Same-session correlation is diagnostic only. KOSPI200 futures
investor buy/sell is not fabricated when the official source is missing.
Production Ridge weights stay at 0 until an independent gate passes.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from roboquant.data.collectors.market_investor_flows import (
    kospi_futures_investor_source_status,
)
from roboquant.db import table_exists

FEATURE_VERSION = "foreign_flow_history_v1"
REVIEW_MARKET = "KOSPI"
FOREIGN_INVESTOR = "외국인"
MIN_REVIEW_ROWS = 60
PRODUCTION_WEIGHT = 0.0
SEQUENTIAL_STEPS = (
    "inventory",
    "timing_leakage",
    "cash_buy_sell_weights",
    "cash_next_session_skill",
    "futures_foreign_match",
    "forecast_overlay",
    "gate_decision",
)


def run_sequential7_foreign_flow_review(
    *,
    cash_flows: pd.DataFrame | None = None,
    futures_flows: pd.DataFrame | None = None,
    returns: pd.DataFrame | None = None,
    forecasts: pd.DataFrame | None = None,
    inventory: dict[str, Any] | None = None,
    market: str = REVIEW_MARKET,
) -> dict[str, Any]:
    """Run the seven-step foreign-flow feasibility review without writing production weights."""

    cash_features = build_foreign_pressure_features(cash_flows, returns, market=market)
    futures_features = build_foreign_pressure_features(
        futures_flows,
        returns,
        market=market,
        instrument="KOSPI200_FUTURES",
    )
    lead_lag = evaluate_lead_lag(cash_features, returns, market=market)
    futures_status = kospi_futures_investor_source_status()
    futures_eval = evaluate_futures_confirmation(
        cash_features,
        futures_features,
        returns,
        market=market,
        source_status=futures_status if futures_features.empty else None,
    )
    overlay = match_forecasts(cash_features, forecasts, market=market)
    gate = decide_review_gate(lead_lag, overlay, futures_eval, cash_features)
    steps = [
        _step_inventory(inventory, cash_features, forecasts, returns),
        _step_timing(),
        _step_weights(cash_features),
        _step_skill(lead_lag),
        _step_futures(futures_eval),
        _step_overlay(overlay),
        _step_gate(gate),
    ]
    return {
        "feature_version": FEATURE_VERSION,
        "market": market,
        "production_weight": PRODUCTION_WEIGHT,
        "overall_status": gate["status"],
        "steps": steps,
        "cash_feature_rows": int(len(cash_features)),
        "labeled_next_session_rows": int(lead_lag.get("n") or 0),
        "forecast_overlap_rows": int(overlay.get("n") or 0),
        "futures_status": futures_eval.get("status"),
        "gate": gate,
        "lead_lag": lead_lag,
        "overlay": overlay,
        "futures": futures_eval,
    }


def build_foreign_pressure_features(
    flows: pd.DataFrame | None,
    market_days: pd.DataFrame | None = None,
    *,
    market: str = REVIEW_MARKET,
    instrument: str | None = None,
) -> pd.DataFrame:
    """Build buy/sell weighted foreign-pressure features available only for the next session."""

    if flows is None or flows.empty:
        return _empty_features()
    required = {"trade_date", "investor", "buy_value", "sell_value"}
    if not required.issubset(flows.columns):
        return _empty_features()
    frame = flows.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.date
    if "market" in frame.columns:
        frame["market"] = frame["market"].astype(str).str.upper()
        frame = frame[frame["market"].eq(str(market).upper())]
    else:
        frame["market"] = str(market).upper()
    frame["investor"] = frame["investor"].astype(str).str.strip()
    frame = frame[frame["investor"].eq(FOREIGN_INVESTOR)].copy()
    if "is_final" in frame.columns:
        frame = frame[frame["is_final"].fillna(False).astype(bool)].copy()
    if instrument and "instrument" in frame.columns:
        frame = frame[frame["instrument"].astype(str).eq(instrument)].copy()
    if frame.empty:
        return _empty_features()
    frame["buy_value"] = pd.to_numeric(frame["buy_value"], errors="coerce")
    frame["sell_value"] = pd.to_numeric(frame["sell_value"], errors="coerce")
    if "net_value" in frame.columns:
        frame["net_value"] = pd.to_numeric(frame["net_value"], errors="coerce")
    else:
        frame["net_value"] = frame["buy_value"] - frame["sell_value"]
    frame = frame.dropna(subset=["trade_date", "buy_value", "sell_value"]).copy()
    frame = (
        frame.sort_values(["market", "trade_date", "buy_value"])
        .groupby(["trade_date", "market"], as_index=False)
        .last()
    )
    churn = (frame["buy_value"] + frame["sell_value"]).replace(0.0, np.nan)
    frame["foreign_imbalance"] = (frame["buy_value"] - frame["sell_value"]) / churn
    frame["foreign_buy_share"] = frame["buy_value"] / churn
    frame["foreign_sell_share"] = frame["sell_value"] / churn
    frame["foreign_churn"] = frame["buy_value"] + frame["sell_value"]
    frame["foreign_signed_pressure"] = frame["foreign_imbalance"] * np.log1p(frame["foreign_churn"].clip(lower=0))
    grouped = frame.groupby("market", group_keys=False)
    frame["foreign_signed_pressure_z20"] = grouped["foreign_signed_pressure"].transform(
        lambda series: _rolling_zscore(series, 20)
    )
    frame["foreign_net_z20"] = grouped["net_value"].transform(lambda series: _rolling_zscore(series, 20))
    frame["available_for_prediction_date"] = _next_session_dates(frame, market_days)
    frame["feature_version"] = FEATURE_VERSION
    return frame.sort_values(["trade_date", "market"]).reset_index(drop=True)


def evaluate_lead_lag(
    features: pd.DataFrame,
    returns: pd.DataFrame | None,
    *,
    market: str = REVIEW_MARKET,
) -> dict[str, Any]:
    aligned = _align_returns(features, returns, market=market)
    if aligned.empty:
        return {"n": 0, "status": "not_ready", "reason": "insufficient_aligned_returns"}
    predictive = aligned.dropna(subset=["next_return", "foreign_signed_pressure"])
    contemporaneous = aligned.dropna(subset=["same_session_return", "foreign_signed_pressure"])
    return {
        "n": int(len(predictive)),
        "status": "ready" if len(predictive) >= MIN_REVIEW_ROWS else "not_ready",
        "predictive_spearman": _spearman(predictive["foreign_signed_pressure"], predictive["next_return"]),
        "predictive_net_spearman": _spearman(predictive["net_value"], predictive["next_return"]),
        "predictive_direction_hit": _direction_hit(predictive["foreign_signed_pressure"], predictive["next_return"]),
        "predictive_net_direction_hit": _direction_hit(predictive["net_value"], predictive["next_return"]),
        "predictive_quintile_spread": _quintile_spread(predictive["foreign_signed_pressure"], predictive["next_return"]),
        "contemporaneous_spearman": _spearman(
            contemporaneous["foreign_signed_pressure"],
            contemporaneous["same_session_return"],
        ),
        "contemporaneous_direction_hit": _direction_hit(
            contemporaneous["foreign_signed_pressure"],
            contemporaneous["same_session_return"],
        ),
        "buy_share_next_spearman": _spearman(predictive["foreign_buy_share"], predictive["next_return"]),
        "sell_share_next_spearman": _spearman(predictive["foreign_sell_share"], predictive["next_return"]),
    }


def evaluate_futures_confirmation(
    cash_features: pd.DataFrame,
    futures_features: pd.DataFrame,
    returns: pd.DataFrame | None,
    *,
    market: str = REVIEW_MARKET,
    source_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if source_status is not None and futures_features.empty:
        return {**source_status, "n": 0, "same_sign_hit": None, "opposite_sign_hit": None}
    if cash_features.empty or futures_features.empty:
        return {
            **kospi_futures_investor_source_status(),
            "n": 0,
            "same_sign_hit": None,
            "opposite_sign_hit": None,
        }
    cash = _align_returns(cash_features, returns, market=market)
    futures = futures_features.rename(
        columns={
            "foreign_signed_pressure": "futures_signed_pressure",
            "net_value": "futures_net_value",
        }
    )
    merged = cash.merge(
        futures[["trade_date", "market", "futures_signed_pressure", "available_for_prediction_date"]],
        on=["trade_date", "market", "available_for_prediction_date"],
        how="inner",
    ).dropna(subset=["next_return", "foreign_signed_pressure", "futures_signed_pressure"])
    if merged.empty:
        return {
            "status": "not_ready",
            "reason": "no_cash_futures_overlap",
            "n": 0,
            "same_sign_hit": None,
            "opposite_sign_hit": None,
            "production_weight": PRODUCTION_WEIGHT,
        }
    cash_sign = np.sign(merged["foreign_signed_pressure"])
    futures_sign = np.sign(merged["futures_signed_pressure"])
    same = merged[cash_sign.eq(futures_sign) & cash_sign.ne(0)]
    opposite = merged[cash_sign.ne(futures_sign) & cash_sign.ne(0) & futures_sign.ne(0)]
    return {
        "status": "ready" if len(merged) >= MIN_REVIEW_ROWS else "not_ready",
        "n": int(len(merged)),
        "same_sign_rows": int(len(same)),
        "opposite_sign_rows": int(len(opposite)),
        "same_sign_hit": _direction_hit(same["foreign_signed_pressure"], same["next_return"]),
        "opposite_sign_hit": _direction_hit(opposite["foreign_signed_pressure"], opposite["next_return"]),
        "confirmation_spearman": _spearman(merged["foreign_signed_pressure"], merged["futures_signed_pressure"]),
        "production_weight": PRODUCTION_WEIGHT,
    }


def match_forecasts(
    features: pd.DataFrame,
    forecasts: pd.DataFrame | None,
    *,
    market: str = REVIEW_MARKET,
) -> dict[str, Any]:
    if features.empty or forecasts is None or forecasts.empty:
        return {"n": 0, "status": "not_ready", "reason": "missing_features_or_forecasts"}
    frame = forecasts.copy()
    if "prediction_date" not in frame.columns and "target_date" in frame.columns:
        frame["prediction_date"] = frame["target_date"]
    frame["prediction_date"] = pd.to_datetime(frame["prediction_date"], errors="coerce").dt.date
    if "market" in frame.columns:
        frame["market"] = frame["market"].astype(str).str.upper()
        frame = frame[frame["market"].eq(str(market).upper())]
    else:
        frame["market"] = str(market).upper()
    expected_col = "expected_return" if "expected_return" in frame.columns else "baseline_expected_return"
    if expected_col not in frame.columns:
        return {"n": 0, "status": "not_ready", "reason": "forecast_expected_return_missing"}
    merged = features.merge(
        frame.rename(columns={expected_col: "expected_return"}),
        left_on=["available_for_prediction_date", "market"],
        right_on=["prediction_date", "market"],
        how="inner",
    )
    if "actual_return" in merged.columns:
        merged["next_return"] = pd.to_numeric(merged["actual_return"], errors="coerce")
    merged = merged.dropna(subset=["foreign_signed_pressure", "expected_return", "next_return"])
    if merged.empty:
        return {"n": 0, "status": "not_ready", "reason": "no_forecast_overlap"}
    residual = merged["next_return"] - merged["expected_return"]
    flow_hit = _direction_hit(merged["foreign_signed_pressure"], merged["next_return"])
    forecast_hit = _direction_hit(merged["expected_return"], merged["next_return"])
    flow_sign = np.sign(merged["foreign_signed_pressure"])
    forecast_sign = np.sign(merged["expected_return"])
    disagree = merged[flow_sign.ne(forecast_sign) & flow_sign.ne(0) & forecast_sign.ne(0)]
    return {
        "n": int(len(merged)),
        "status": "ready" if len(merged) >= MIN_REVIEW_ROWS else "not_ready",
        "flow_direction_hit": flow_hit,
        "forecast_direction_hit": forecast_hit,
        "residual_spearman": _spearman(merged["foreign_signed_pressure"], residual),
        "disagreement_rows": int(len(disagree)),
        "disagreement_flow_hit": _direction_hit(disagree["foreign_signed_pressure"], disagree["next_return"])
        if not disagree.empty
        else None,
        "disagreement_forecast_hit": _direction_hit(disagree["expected_return"], disagree["next_return"])
        if not disagree.empty
        else None,
    }


def decide_review_gate(
    lead_lag: dict[str, Any],
    overlay: dict[str, Any],
    futures_eval: dict[str, Any],
    cash_features: pd.DataFrame,
) -> dict[str, Any]:
    labeled = int(lead_lag.get("n") or 0)
    if cash_features.empty or labeled == 0:
        return {
            "status": "not_ready",
            "reason": "foreign_cash_history_missing",
            "production_weight": PRODUCTION_WEIGHT,
            "candidate": None,
        }
    if labeled < MIN_REVIEW_ROWS:
        return {
            "status": "not_ready",
            "reason": "insufficient_next_session_labels",
            "production_weight": PRODUCTION_WEIGHT,
            "candidate": None,
        }
    hit = lead_lag.get("predictive_direction_hit")
    spread = lead_lag.get("predictive_quintile_spread")
    residual = overlay.get("residual_spearman")
    skill = (hit is not None and hit > 0.52) or (spread is not None and abs(spread) > 0.001)
    incremental = residual is not None and abs(residual) > 0.05
    if skill or incremental:
        return {
            "status": "accepted_shadow",
            "reason": "next_session_skill_or_forecast_residual",
            "production_weight": PRODUCTION_WEIGHT,
            "candidate": "market-foreign-flow-shadow-v1",
        }
    return {
        "status": "rejected",
        "reason": "weak_next_session_skill",
        "production_weight": PRODUCTION_WEIGHT,
        "candidate": None,
        "futures_status": futures_eval.get("status"),
    }


def load_review_inventory(conn) -> dict[str, Any]:
    inventory = {
        "benchmark_kospi_sessions": _count(
            conn,
            "SELECT COUNT(*) FROM benchmark_daily WHERE benchmark = 'KOSPI'",
            table="benchmark_daily",
        ),
        "benchmark_min_date": _scalar(
            conn,
            "SELECT MIN(date) FROM benchmark_daily WHERE benchmark = 'KOSPI'",
            table="benchmark_daily",
        ),
        "benchmark_max_date": _scalar(
            conn,
            "SELECT MAX(date) FROM benchmark_daily WHERE benchmark = 'KOSPI'",
            table="benchmark_daily",
        ),
        "investor_flows_daily_rows": _count(conn, "SELECT COUNT(*) FROM investor_flows_daily", table="investor_flows_daily"),
        "market_investor_flows_daily_rows": _count(
            conn,
            "SELECT COUNT(*) FROM market_investor_flows_daily",
            table="market_investor_flows_daily",
        ),
        "market_flow_features_daily_rows": _count(
            conn,
            "SELECT COUNT(*) FROM market_flow_features_daily",
            table="market_flow_features_daily",
        ),
        "production_today_kospi_forecasts": _count(
            conn,
            "SELECT COUNT(*) FROM market_outlook_forecasts WHERE horizon = 'TODAY' AND market = 'KOSPI'",
            table="market_outlook_forecasts",
        ),
        "us_linkage_labeled_kospi": _count(
            conn,
            """
            SELECT COUNT(*) FROM market_us_linkage_shadow_forecasts
            WHERE market = 'KOSPI' AND actual_return IS NOT NULL
            """,
            table="market_us_linkage_shadow_forecasts",
        ),
        "krx_login_configured": _krx_login_configured(),
    }
    return inventory


def load_benchmark_returns(conn, market: str = REVIEW_MARKET) -> pd.DataFrame:
    if not table_exists(conn, "benchmark_daily"):
        return pd.DataFrame(columns=["trade_date", "market", "actual_return"])
    frame = conn.execute(
        """
        SELECT date AS trade_date, benchmark AS market, close
        FROM benchmark_daily
        WHERE benchmark = ?
        ORDER BY date
        """,
        [market],
    ).fetchdf()
    if frame.empty:
        return pd.DataFrame(columns=["trade_date", "market", "actual_return"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.date
    frame["actual_return"] = pd.to_numeric(frame["close"], errors="coerce").pct_change()
    frame["market"] = market
    return frame[["trade_date", "market", "actual_return"]]


def load_shadow_forecasts(conn, market: str = REVIEW_MARKET) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    if table_exists(conn, "market_us_linkage_shadow_forecasts"):
        linkage = conn.execute(
            """
            SELECT prediction_date, asof_date, market,
                   baseline_expected_return AS expected_return, actual_return, model_version
            FROM market_us_linkage_shadow_forecasts
            WHERE market = ? AND actual_return IS NOT NULL
            """,
            [market],
        ).fetchdf()
        if not linkage.empty:
            linkage["source"] = "market_us_linkage_shadow"
            frames.append(linkage)
    if table_exists(conn, "market_outlook_forecasts"):
        production = conn.execute(
            """
            SELECT target_date AS prediction_date, asof_date, market, expected_return, model_version
            FROM market_outlook_forecasts
            WHERE horizon = 'TODAY' AND market = ?
            """,
            [market],
        ).fetchdf()
        if not production.empty:
            production["actual_return"] = np.nan
            production["source"] = "market_outlook_production"
            frames.append(production)
    if not frames:
        return pd.DataFrame(
            columns=["prediction_date", "asof_date", "market", "expected_return", "actual_return", "model_version"]
        )
    return pd.concat(frames, ignore_index=True)


def attach_actual_returns_to_forecasts(
    forecasts: pd.DataFrame,
    returns: pd.DataFrame | None,
    *,
    market: str = REVIEW_MARKET,
) -> pd.DataFrame:
    if forecasts is None or forecasts.empty:
        return pd.DataFrame(
            columns=["prediction_date", "asof_date", "market", "expected_return", "actual_return", "model_version"]
        )
    frame = forecasts.copy()
    if "prediction_date" not in frame.columns and "target_date" in frame.columns:
        frame["prediction_date"] = frame["target_date"]
    frame["prediction_date"] = pd.to_datetime(frame["prediction_date"], errors="coerce").dt.date
    if "actual_return" not in frame.columns:
        frame["actual_return"] = pd.NA
    if returns is None or returns.empty:
        return frame
    ret = returns.copy()
    ret["trade_date"] = pd.to_datetime(ret["trade_date"], errors="coerce").dt.date
    if "market" in ret.columns:
        ret["market"] = ret["market"].astype(str).str.upper()
        ret = ret[ret["market"].eq(str(market).upper())]
    lookup = ret.drop_duplicates("trade_date").set_index("trade_date")["actual_return"]
    missing = frame["actual_return"].isna()
    frame.loc[missing, "actual_return"] = frame.loc[missing, "prediction_date"].map(lookup)
    return frame


def load_cash_flows(conn, market: str = REVIEW_MARKET) -> pd.DataFrame:
    if not table_exists(conn, "market_investor_flows_daily"):
        return pd.DataFrame()
    return conn.execute(
        """
        SELECT *
        FROM market_investor_flows_daily
        WHERE market = ? AND investor = ? AND is_final = TRUE
        ORDER BY trade_date
        """,
        [market, FOREIGN_INVESTOR],
    ).fetchdf()


def _step_inventory(
    inventory: dict[str, Any] | None,
    cash_features: pd.DataFrame,
    forecasts: pd.DataFrame | None,
    returns: pd.DataFrame | None,
) -> dict[str, Any]:
    payload = dict(inventory or {})
    payload["cash_feature_rows"] = int(len(cash_features))
    payload["forecast_rows"] = 0 if forecasts is None else int(len(forecasts))
    payload["return_rows"] = 0 if returns is None else int(len(returns))
    ready = int(payload.get("cash_feature_rows") or 0) > 0
    return _step(
        "inventory",
        "ready" if ready else "not_ready",
        "Local KOSPI sessions and forecast history; cash foreign rows are required before skill tests.",
        payload,
    )


def _step_timing() -> dict[str, Any]:
    return _step(
        "timing_leakage",
        "ready",
        "Final cash flows after 18:20 KST on session T are eligible only for the T+1 08:00 forecast.",
        {
            "final_cutoff_kst": "18:20",
            "prediction_cutoff_kst": "08:00",
            "same_session_use": "diagnostic_only",
            "model_eligible_alignment": "available_for_prediction_date = next KRX session",
        },
    )


def _step_weights(cash_features: pd.DataFrame) -> dict[str, Any]:
    if cash_features.empty:
        return _step(
            "cash_buy_sell_weights",
            "not_ready",
            "Buy and sell are weighted separately via imbalance x log churn; net-only is the control.",
            {"rows": 0},
        )
    return _step(
        "cash_buy_sell_weights",
        "ready",
        "foreign_signed_pressure = ((buy-sell)/(buy+sell)) * log1p(buy+sell).",
        {
            "rows": int(len(cash_features)),
            "mean_buy_share": _mean(cash_features["foreign_buy_share"]),
            "mean_imbalance": _mean(cash_features["foreign_imbalance"]),
        },
    )


def _step_skill(lead_lag: dict[str, Any]) -> dict[str, Any]:
    status = "ready" if int(lead_lag.get("n") or 0) >= MIN_REVIEW_ROWS else "not_ready"
    return _step(
        "cash_next_session_skill",
        status,
        "Predictive skill is T-flow vs T+1 return. Same-session correlation is not a forecast score.",
        lead_lag,
    )


def _step_futures(futures_eval: dict[str, Any]) -> dict[str, Any]:
    return _step(
        "futures_foreign_match",
        str(futures_eval.get("status") or "not_ready"),
        "Match cash and KOSPI200 futures foreign signs only when both official series exist.",
        futures_eval,
    )


def _step_overlay(overlay: dict[str, Any]) -> dict[str, Any]:
    return _step(
        "forecast_overlay",
        str(overlay.get("status") or "not_ready"),
        "Join next-session foreign pressure to existing outlook/shadow forecasts; score residuals only.",
        overlay,
    )


def _step_gate(gate: dict[str, Any]) -> dict[str, Any]:
    return _step(
        "gate_decision",
        str(gate.get("status") or "not_ready"),
        "A passing candidate remains a shadow with production_weight=0 until live final observations accumulate.",
        gate,
    )


def _step(key: str, status: str, summary: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {"key": key, "status": status, "summary": summary, "detail": _json_safe(detail)}


def _align_returns(
    features: pd.DataFrame,
    returns: pd.DataFrame | None,
    *,
    market: str,
) -> pd.DataFrame:
    if features.empty or returns is None or returns.empty:
        return pd.DataFrame()
    ret = returns.copy()
    ret["trade_date"] = pd.to_datetime(ret["trade_date"], errors="coerce").dt.date
    if "market" in ret.columns:
        ret["market"] = ret["market"].astype(str).str.upper()
        ret = ret[ret["market"].eq(str(market).upper())]
    else:
        ret["market"] = str(market).upper()
    ret["actual_return"] = pd.to_numeric(ret["actual_return"], errors="coerce")
    same = ret.rename(columns={"actual_return": "same_session_return"})
    nxt = ret.rename(columns={"trade_date": "available_for_prediction_date", "actual_return": "next_return"})
    aligned = features.merge(same[["trade_date", "market", "same_session_return"]], on=["trade_date", "market"], how="left")
    return aligned.merge(
        nxt[["available_for_prediction_date", "market", "next_return"]],
        on=["available_for_prediction_date", "market"],
        how="left",
    )


def _next_session_dates(frame: pd.DataFrame, market_days: pd.DataFrame | None) -> pd.Series:
    if market_days is not None and not market_days.empty:
        days = pd.to_datetime(market_days.get("trade_date", market_days.iloc[:, 0]), errors="coerce").dt.date.dropna()
        ordered = sorted(set(days.tolist()))
    else:
        ordered = sorted(set(frame["trade_date"].dropna().tolist()))
    lookup = {current: ordered[index + 1] if index + 1 < len(ordered) else None for index, current in enumerate(ordered)}
    return pd.Series([lookup.get(value) for value in frame["trade_date"]], index=frame.index)


def _rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window, min_periods=window).mean()
    std = series.rolling(window, min_periods=window).std(ddof=0).replace(0.0, np.nan)
    return (series - mean) / std


def _spearman(left: pd.Series, right: pd.Series) -> float | None:
    pair = pd.DataFrame({"left": pd.to_numeric(left, errors="coerce"), "right": pd.to_numeric(right, errors="coerce")})
    pair = pair.replace([np.inf, -np.inf], np.nan).dropna()
    if len(pair) < 3 or pair["left"].nunique() < 2 or pair["right"].nunique() < 2:
        return None
    value = pair["left"].corr(pair["right"], method="spearman")
    return None if pd.isna(value) else float(value)


def _direction_hit(signal: pd.Series, target: pd.Series) -> float | None:
    pair = pd.DataFrame(
        {"signal": np.sign(pd.to_numeric(signal, errors="coerce")), "target": np.sign(pd.to_numeric(target, errors="coerce"))}
    ).dropna()
    pair = pair[(pair["signal"] != 0) & (pair["target"] != 0)]
    if pair.empty:
        return None
    return float((pair["signal"] == pair["target"]).mean())


def _quintile_spread(signal: pd.Series, target: pd.Series) -> float | None:
    pair = pd.DataFrame(
        {"signal": pd.to_numeric(signal, errors="coerce"), "target": pd.to_numeric(target, errors="coerce")}
    ).dropna()
    if len(pair) < 20:
        return None
    try:
        bins = pd.qcut(pair["signal"], 5, labels=False, duplicates="drop")
    except ValueError:
        return None
    if bins.nunique() < 5:
        return None
    grouped = pair.groupby(bins)["target"].mean()
    return float(grouped.loc[grouped.index.max()] - grouped.loc[grouped.index.min()])


def _mean(series: pd.Series) -> float | None:
    value = pd.to_numeric(series, errors="coerce").mean()
    return None if pd.isna(value) else float(value)


def _count(conn, sql: str, *, table: str) -> int:
    if not table_exists(conn, table):
        return 0
    row = conn.execute(sql).fetchone()
    return int(row[0] or 0) if row else 0


def _scalar(conn, sql: str, *, table: str) -> Any:
    if not table_exists(conn, table):
        return None
    row = conn.execute(sql).fetchone()
    value = row[0] if row else None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _krx_login_configured() -> bool:
    from os import getenv

    return bool(str(getenv("KRX_ID") or "").strip() and str(getenv("KRX_PW") or "").strip())


def _json_safe(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return None if not np.isfinite(number) else number
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, (bool, np.bool_, str)):
        return bool(value) if isinstance(value, (bool, np.bool_)) else value
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _empty_features() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "trade_date",
            "market",
            "buy_value",
            "sell_value",
            "net_value",
            "foreign_imbalance",
            "foreign_buy_share",
            "foreign_sell_share",
            "foreign_churn",
            "foreign_signed_pressure",
            "foreign_signed_pressure_z20",
            "foreign_net_z20",
            "available_for_prediction_date",
            "feature_version",
        ]
    )
