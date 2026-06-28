from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

KST = ZoneInfo("Asia/Seoul")
DEFAULT_FEATURE_VERSION = "domestic_plus_global_regime_v1"
DEFAULT_THRESHOLDS = {
    "sp500_return_1d": -0.02,
    "nasdaq_return_1d": -0.03,
    "sox_return_1d": -0.04,
    "vix_level": 25.0,
    "vix_change_1d": 0.20,
    "us10y_change_bp_1d": 10.0,
    "usdkrw_return_1d": 0.01,
    "nasdaq_futures_return_snapshot": -0.01,
}


def resolve_cutoff(value: str | None, cutoff_time_kst: str = "08:00") -> datetime:
    if value is None or str(value).strip().lower() == "latest":
        hour, minute = [int(part) for part in cutoff_time_kst.split(":", maxsplit=1)]
        today = datetime.now(KST).date()
        return datetime.combine(today, time(hour, minute), tzinfo=KST)
    raw = str(value).strip()
    if raw.lower() in {"now", "current"}:
        return datetime.now(KST)
    if len(raw) == 10:
        hour, minute = [int(part) for part in cutoff_time_kst.split(":", maxsplit=1)]
        return datetime.combine(date.fromisoformat(raw), time(hour, minute), tzinfo=KST)
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=KST)


def resolve_prediction_date(value: str | None, cutoff: datetime) -> date:
    if value is None or str(value).strip().lower() == "latest":
        return cutoff.astimezone(KST).date()
    return date.fromisoformat(str(value))


def build_market_regime_row(
    conn,
    *,
    prediction_date: date,
    prediction_cutoff: datetime,
    config: dict[str, Any],
) -> dict[str, Any]:
    latest = _latest_daily_frame(conn, prediction_date)
    if latest.empty:
        raise RuntimeError("global_market_daily has no rows for market regime calculation")
    thresholds = {**DEFAULT_THRESHOLDS, **config.get("regime", {}).get("thresholds", {})}
    feature_version = config.get("regime", {}).get("feature_version", DEFAULT_FEATURE_VERSION)
    snapshots = _latest_snapshot_frame(conn, prediction_cutoff)

    signals = {
        "sp500_return_1d": _daily_value(latest, "^GSPC", "return_1d"),
        "nasdaq_return_1d": _daily_value(latest, "^IXIC", "return_1d"),
        "sox_return_1d": _daily_value(latest, "^SOX", "return_1d"),
        "vix_level": _daily_value(latest, "VIXCLS", "close", fallback_symbol="^VIX"),
        "vix_change_1d": _daily_value(latest, "VIXCLS", "return_1d", fallback_symbol="^VIX"),
        "us10y_change_bp_1d": _daily_absolute_change_bp(conn, "DGS10", prediction_date),
        "usdkrw_return_1d": _daily_value(latest, "USDKRW=X", "return_1d"),
        "nasdaq_futures_return_snapshot": _snapshot_value(snapshots, "NQ=F", "change_rate"),
        "dow_return_1d": _daily_value(latest, "^DJI", "return_1d"),
        "nikkei_return_1d": _daily_value(latest, "^N225", "return_1d"),
        "taiwan_return_1d": _daily_value(latest, "^TWII", "return_1d"),
        "tsm_return_1d": _daily_value(latest, "TSM", "return_1d"),
    }

    reasons: list[str] = []
    us_equity_score = _downside_score(
        signals["sp500_return_1d"],
        thresholds["sp500_return_1d"],
        20,
        "S&P500 -2% 이하",
        reasons,
    ) + _downside_score(
        signals["nasdaq_return_1d"],
        thresholds["nasdaq_return_1d"],
        25,
        "Nasdaq -3% 이하",
        reasons,
    )
    semiconductor_score = _downside_score(
        signals["sox_return_1d"],
        thresholds["sox_return_1d"],
        25,
        "SOX -4% 이하",
        reasons,
    )
    volatility_score = _upside_score(
        signals["vix_level"],
        thresholds["vix_level"],
        15,
        "VIX 25 이상",
        reasons,
    ) + _upside_score(
        signals["vix_change_1d"],
        thresholds["vix_change_1d"],
        10,
        "VIX 일간 +20% 이상",
        reasons,
    )
    rate_score = _upside_score(
        signals["us10y_change_bp_1d"],
        thresholds["us10y_change_bp_1d"],
        10,
        "US10Y +10bp 이상",
        reasons,
    )
    fx_score = _upside_score(
        signals["usdkrw_return_1d"],
        thresholds["usdkrw_return_1d"],
        10,
        "USD/KRW +1% 이상",
        reasons,
    )
    futures_score = _downside_score(
        signals["nasdaq_futures_return_snapshot"],
        thresholds["nasdaq_futures_return_snapshot"],
        10,
        "Nasdaq futures -1% 이하",
        reasons,
    )
    asia_score = 0
    commodity_score = 0
    global_risk_score = float(
        us_equity_score
        + semiconductor_score
        + asia_score
        + volatility_score
        + rate_score
        + fx_score
        + futures_score
        + commodity_score
    )
    if not reasons:
        reasons.append("주요 글로벌 shock 조건 미충족")
    regime, cash_ratio = _regime_and_cash(global_risk_score)
    return {
        "prediction_date": prediction_date,
        "prediction_cutoff": _storage_timestamp(prediction_cutoff.astimezone(KST)),
        "us_equity_score": float(us_equity_score),
        "semiconductor_score": float(semiconductor_score),
        "asia_score": float(asia_score),
        "volatility_score": float(volatility_score),
        "rate_score": float(rate_score),
        "fx_score": float(fx_score),
        "futures_score": float(futures_score),
        "commodity_score": float(commodity_score),
        "global_risk_score": global_risk_score,
        "regime": regime,
        "recommended_cash_ratio": cash_ratio,
        "signals_json": json.dumps(_compact_signals(signals), ensure_ascii=False, allow_nan=False),
        "reasons_json": json.dumps(reasons, ensure_ascii=False, allow_nan=False),
        "feature_version": feature_version,
        "created_at": datetime.now(UTC).replace(tzinfo=None),
    }


def regime_row_to_frame(row: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame([row])


def _latest_daily_frame(conn, prediction_date: date) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT *
        FROM (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol
                       ORDER BY trade_date DESC, ingested_at DESC NULLS LAST
                   ) AS row_number
            FROM global_market_daily
            WHERE trade_date <= ?
        )
        WHERE row_number = 1
        """,
        [prediction_date],
    ).fetchdf()


def _latest_snapshot_frame(conn, prediction_cutoff: datetime) -> pd.DataFrame:
    cutoff = _storage_timestamp(prediction_cutoff.astimezone(KST))
    return conn.execute(
        """
        SELECT *
        FROM (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol
                       ORDER BY snapshot_at DESC, source_timestamp DESC NULLS LAST
                   ) AS row_number
            FROM global_market_intraday_snapshot
            WHERE snapshot_at <= ?
        )
        WHERE row_number = 1
        """,
        [cutoff],
    ).fetchdf()


def _daily_value(frame: pd.DataFrame, symbol: str, column: str, fallback_symbol: str | None = None):
    for candidate in [symbol, fallback_symbol]:
        if not candidate:
            continue
        selected = frame[frame["symbol"].astype(str).eq(candidate)]
        if not selected.empty and column in selected.columns:
            value = selected.iloc[0].get(column)
            if pd.notna(value):
                return float(value)
    return None


def _snapshot_value(frame: pd.DataFrame, symbol: str, column: str):
    if frame.empty:
        return None
    selected = frame[frame["symbol"].astype(str).eq(symbol)]
    if selected.empty or column not in selected.columns:
        return None
    value = selected.iloc[0].get(column)
    return None if pd.isna(value) else float(value)


def _daily_absolute_change_bp(conn, symbol: str, prediction_date: date) -> float | None:
    frame = conn.execute(
        """
        SELECT close
        FROM global_market_daily
        WHERE symbol = ? AND trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT 2
        """,
        [symbol, prediction_date],
    ).fetchdf()
    if len(frame) < 2:
        return None
    latest, previous = float(frame.iloc[0]["close"]), float(frame.iloc[1]["close"])
    return (latest - previous) * 100.0


def _downside_score(value, threshold: float, score: int, reason: str, reasons: list[str]) -> int:
    if value is not None and float(value) <= float(threshold):
        reasons.append(reason)
        return score
    return 0


def _upside_score(value, threshold: float, score: int, reason: str, reasons: list[str]) -> int:
    if value is not None and float(value) >= float(threshold):
        reasons.append(reason)
        return score
    return 0


def _regime_and_cash(global_risk_score: float) -> tuple[str, float]:
    if global_risk_score >= 70:
        return "panic", 0.50
    if global_risk_score >= 45:
        return "risk_off", 0.30
    if global_risk_score >= 20:
        return "neutral", 0.15
    return "risk_on", 0.05


def _compact_signals(signals: dict[str, Any]) -> dict[str, float]:
    return {key: float(value) for key, value in signals.items() if value is not None and pd.notna(value)}


def _storage_timestamp(value: datetime) -> datetime:
    return value.replace(tzinfo=None)
