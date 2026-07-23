from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

HORIZON_TRADING_DAYS = {
    "1M": 20,
    "2M": 42,
    "3M": 63,
    "6M": 126,
    "9M": 189,
    "1Y": 252,
    "2Y": 504,
}

# Calendar lookbacks are intentionally longer than their evaluation horizon:
# the newest horizon worth of predictions is still pending, so a shorter window
# cannot show any mature performance for 3M/6M forecasts.
HORIZON_LOOKBACK_CALENDAR_DAYS = {
    "1M": 180,
    "2M": 270,
    "3M": 365,
    "6M": 730,
    "9M": 1095,
    "1Y": 1460,
    "2Y": 2920,
}


def build_prediction_price_gap(
    conn,
    *,
    lookback_days: int | None = None,
    target_days: int | None = None,
    horizon: str = "3M",
    symbols: list[str] | None = None,
    market: str | None = None,
    as_of_date: str | date | None = None,
    include_pending: bool = True,
    top_k: int = 20,
    limit: int = 5000,
) -> dict[str, Any]:
    target_trading_days, target_days_source = _resolve_target_trading_days(horizon, target_days)
    resolved_lookback_days, lookback_days_source = _resolve_lookback_days(horizon, lookback_days)
    normalized_market = _normalize_market(market)
    latest_price_date = _resolve_as_of_date(conn, as_of_date)
    if latest_price_date is None:
        return _empty_payload(
            resolved_lookback_days,
            target_trading_days,
            horizon,
            "prices_daily is empty",
            market=normalized_market,
            top_k=top_k,
            target_days_source=target_days_source,
            lookback_days_source=lookback_days_source,
        )
    start_date = latest_price_date - timedelta(days=resolved_lookback_days)
    predictions = _load_predictions(conn, start_date, latest_price_date, horizon, symbols, normalized_market)
    if predictions.empty:
        return {
            **_empty_payload(
                resolved_lookback_days,
                target_trading_days,
                horizon,
                "No predictions in lookback window",
                market=normalized_market,
                top_k=top_k,
                target_days_source=target_days_source,
                lookback_days_source=lookback_days_source,
            ),
            "as_of_date": latest_price_date.isoformat(),
            "start_date": start_date.isoformat(),
        }
    predictions["rank_no"] = predictions.groupby(
        ["prediction_date", "horizon", "model_name", "model_version"], dropna=False
    )["recommendation_score"].rank(ascending=False, method="first")
    predictions["rank_no"] = predictions["rank_no"].astype(int)
    prices = _load_prices(conn, predictions["symbol"].dropna().unique().tolist(), start_date, latest_price_date)
    items = _build_items(predictions, prices, latest_price_date, target_trading_days)
    if not include_pending:
        items = [item for item in items if item["status"] == "completed"]
    summary = summarize_price_gap(items, top_k=top_k)
    display_items = sorted(
        items,
        key=lambda item: (
            item.get("status") != "completed",
            str(item.get("prediction_date") or ""),
            int(item.get("rank_no") or 999999),
        ),
        reverse=False,
    )[: int(limit)]
    return {
        "status": "ready" if items else "empty",
        "lookback_days": resolved_lookback_days,
        "lookback_days_source": lookback_days_source,
        # `target_days` remains for callers of the original endpoint, but is now
        # explicitly expressed in trading sessions rather than calendar days.
        "target_days": target_trading_days,
        "target_trading_days": target_trading_days,
        "target_days_source": target_days_source,
        "target_trading_days_by_horizon": _target_days_by_horizon(items),
        "horizon": horizon,
        "market": normalized_market or "all",
        "top_k": int(top_k),
        "as_of_date": latest_price_date.isoformat(),
        "start_date": start_date.isoformat(),
        "summary": summary,
        "items": display_items,
        "disclaimer": (
            "확정 성능은 horizon 거래일이 완료된 예측만 포함합니다. "
            "pending의 현재 가격 흐름은 중간 관측이며 정확도로 해석하지 않습니다."
        ),
    }


def summarize_price_gap(items: list[dict[str, Any]], *, top_k: int = 20) -> dict[str, Any]:
    frame = pd.DataFrame(items)
    if frame.empty:
        return {
            "sample_count": 0,
            "completed_count": 0,
            "pending_count": 0,
            "missing_count": 0,
            "mae_latest": None,
            "bias_latest": None,
            "direction_accuracy_latest": None,
            "mae_30d": None,
            "bias_30d": None,
            "direction_accuracy_30d": None,
            "top20_mae_latest": None,
            "mature_sample_count": 0,
            "mature_mae": None,
            "mature_bias": None,
            "mature_direction_accuracy": None,
            "mature_top_k_count": 0,
            "mature_top_k_direction_accuracy": None,
            "interim_sample_count": 0,
            "interim_observed_count": 0,
            "interim_mae": None,
            "interim_bias": None,
            "interim_direction_accuracy": None,
        }
    completed = frame[frame["status"].eq("completed")]
    pending = frame[frame["status"].eq("pending")]
    latest_valid = frame[pd.to_numeric(frame["return_gap_latest"], errors="coerce").notna()]
    completed_valid = completed[pd.to_numeric(completed["return_gap_30d"], errors="coerce").notna()]
    pending_valid = pending[pd.to_numeric(pending["return_gap_latest"], errors="coerce").notna()]
    mature_top_k = completed_valid[pd.to_numeric(completed_valid["rank_no"], errors="coerce") <= int(top_k)]
    top20 = latest_valid[pd.to_numeric(latest_valid["rank_no"], errors="coerce") <= 20]
    return {
        "sample_count": int(len(frame)),
        "completed_count": int(frame["status"].eq("completed").sum()),
        "pending_count": int(frame["status"].eq("pending").sum()),
        "missing_count": int(frame["status"].astype(str).str.startswith("missing").sum()),
        "mae_latest": _mean_abs(latest_valid.get("return_gap_latest")),
        "bias_latest": _mean(latest_valid.get("return_gap_latest")),
        "direction_accuracy_latest": _mean_bool(latest_valid.get("direction_hit_latest")),
        "mae_30d": _mean_abs(completed_valid.get("return_gap_30d")),
        "bias_30d": _mean(completed_valid.get("return_gap_30d")),
        "direction_accuracy_30d": _mean_bool(completed_valid.get("direction_hit_30d")),
        "top20_mae_latest": _mean_abs(top20.get("return_gap_latest")),
        # Canonical performance metrics: only matured horizons are accuracy.
        "mature_sample_count": int(len(completed_valid)),
        "mature_mae": _mean_abs(completed_valid.get("return_gap_30d")),
        "mature_bias": _mean(completed_valid.get("return_gap_30d")),
        "mature_direction_accuracy": _mean_bool(completed_valid.get("direction_hit_30d")),
        "mature_top_k_count": int(len(mature_top_k)),
        "mature_top_k_direction_accuracy": _mean_bool(mature_top_k.get("direction_hit_30d")),
        # Pending observations are deliberately separated from confirmed accuracy.
        "interim_sample_count": int(len(pending)),
        "interim_observed_count": int(len(pending_valid)),
        "interim_mae": _mean_abs(pending_valid.get("return_gap_latest")),
        "interim_bias": _mean(pending_valid.get("return_gap_latest")),
        "interim_direction_accuracy": _mean_bool(pending_valid.get("direction_hit_latest")),
    }


def _load_predictions(
    conn,
    start_date: date,
    end_date: date,
    horizon: str,
    symbols: list[str] | None,
    market: str | None,
) -> pd.DataFrame:
    production_horizon_filter = ""
    shadow_horizon_filter = ""
    params: list[Any] = [start_date, end_date]
    if horizon.lower() != "all":
        production_horizon_filter = "AND p.horizon = ?"
        shadow_horizon_filter = "AND mp.horizon = ?"
        params.append(horizon)
    production_symbol_filter = ""
    shadow_symbol_filter = ""
    if symbols:
        normalized = [str(symbol).zfill(6) for symbol in symbols]
        placeholders = ", ".join(["?"] * len(normalized))
        production_symbol_filter = f"AND p.symbol IN ({placeholders})"
        shadow_symbol_filter = f"AND mp.symbol IN ({placeholders})"
        params.extend(normalized)
    market_filter = ""
    if market:
        market_filter = "AND UPPER(COALESCE(s.market, '')) = ?"
        params.append(market)
    production = conn.execute(
        f"""
        SELECT
          p.asof_date AS prediction_date,
          p.symbol,
          p.horizon,
          'lightgbm' AS model_name,
          COALESCE(p.model_version, 'unknown') AS model_version,
          p.pred_return AS predicted_return,
          p.pred_prob_top20 AS predicted_probability,
          p.pred_risk AS risk_score,
          p.pred_prob_top20 AS recommendation_score,
          s.market,
          'production' AS source
        FROM predictions AS p
        LEFT JOIN symbols AS s ON p.symbol = s.symbol
        WHERE p.asof_date BETWEEN ? AND ?
          {production_horizon_filter}
          {production_symbol_filter}
          {market_filter}
        """,
        params,
    ).fetchdf()
    shadow = conn.execute(
        f"""
        SELECT
          mp.date AS prediction_date,
          mp.symbol,
          mp.horizon,
          mp.model_name,
          COALESCE(mp.model_version, mp.model_name) AS model_version,
          mp.pred_score AS predicted_return,
          mp.pred_prob AS predicted_probability,
          mp.risk_score,
          COALESCE(mp.recommendation_score, mp.pred_prob) AS recommendation_score,
          s.market,
          'shadow' AS source
        FROM model_predictions AS mp
        LEFT JOIN symbols AS s ON mp.symbol = s.symbol
        WHERE mp.date BETWEEN ? AND ?
          {shadow_horizon_filter}
          {shadow_symbol_filter}
          {market_filter}
        """,
        params,
    ).fetchdf()
    frame = pd.concat([production, shadow], ignore_index=True)
    if frame.empty:
        return frame
    frame["prediction_date"] = pd.to_datetime(frame["prediction_date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    for column in ("predicted_return", "predicted_probability", "risk_score", "recommendation_score"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["prediction_date", "symbol", "recommendation_score"])


def _load_prices(conn, symbols: list[str], start_date: date, end_date: date) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    placeholders = ", ".join(["?"] * len(symbols))
    frame = conn.execute(
        f"""
        SELECT p.date, p.symbol, p.close, s.name, s.market, s.sector
        FROM prices_daily AS p
        LEFT JOIN symbols AS s ON p.symbol = s.symbol
        WHERE p.symbol IN ({placeholders})
          AND p.date BETWEEN ? AND ?
        ORDER BY p.symbol, p.date
        """,
        [*symbols, start_date, end_date],
    ).fetchdf()
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame.dropna(subset=["date", "symbol", "close"])


def _build_items(
    predictions: pd.DataFrame,
    prices: pd.DataFrame,
    latest_price_date: date,
    target_days: int | None,
) -> list[dict[str, Any]]:
    prices_by_symbol = {symbol: group.sort_values("date") for symbol, group in prices.groupby("symbol")}
    rows = []
    for raw in predictions.sort_values(["prediction_date", "horizon", "rank_no"]).to_dict(orient="records"):
        symbol = str(raw["symbol"]).zfill(6)
        symbol_prices = prices_by_symbol.get(symbol, pd.DataFrame())
        prediction_date = pd.to_datetime(raw["prediction_date"]).date()
        item_target_days = target_days if target_days is not None else _horizon_trading_days(raw.get("horizon"))
        entry = _first_on_or_after(symbol_prices, prediction_date)
        latest = _last_on_or_before(symbol_prices, latest_price_date)
        target = _trading_session_after(symbol_prices, entry, item_target_days)
        status = "pending"
        if entry is None or latest is None:
            status = "missing_price"
        elif target is not None:
            status = "completed"
        actual_latest = _return(entry, latest)
        actual_30d = _return(entry, target) if status == "completed" else None
        predicted = _safe_float(raw.get("predicted_return"))
        target_date = _date_value(target)
        rows.append(
            {
                "prediction_date": prediction_date.isoformat(),
                # Kept for existing clients. It now contains the realised target
                # session date, not an imprecise calendar-day approximation.
                "target_calendar_date": target_date,
                "target_trading_date": target_date,
                "target_trading_days": int(item_target_days),
                "status": status,
                "symbol": symbol,
                "name": _value_from_price(entry, "name"),
                "market": _value_from_price(entry, "market"),
                "sector": _value_from_price(entry, "sector"),
                "horizon": raw.get("horizon"),
                "model_name": raw.get("model_name"),
                "model_version": raw.get("model_version"),
                "source": raw.get("source"),
                "rank_no": int(raw["rank_no"]),
                "predicted_return": predicted,
                "predicted_probability": _safe_float(raw.get("predicted_probability")),
                "recommendation_score": _safe_float(raw.get("recommendation_score")),
                "risk_score": _safe_float(raw.get("risk_score")),
                "entry_date": _date_value(entry),
                "entry_price": _price_value(entry),
                "latest_price_date": _date_value(latest),
                "latest_price": _price_value(latest),
                "target_price_date": target_date if status == "completed" else None,
                "target_price": _price_value(target) if status == "completed" else None,
                "elapsed_days": None if entry is None else (latest_price_date - entry["date"]).days,
                "elapsed_trading_days": _elapsed_trading_days(symbol_prices, entry, latest),
                "actual_return_latest": actual_latest,
                "actual_return_30d": actual_30d,
                "return_gap_latest": _gap(actual_latest, predicted),
                "return_gap_30d": _gap(actual_30d, predicted),
                "abs_gap_latest": _abs_gap(actual_latest, predicted),
                "abs_gap_30d": _abs_gap(actual_30d, predicted),
                "direction_hit_latest": _direction_hit(predicted, actual_latest),
                "direction_hit_30d": _direction_hit(predicted, actual_30d),
            }
        )
    return rows


def _resolve_as_of_date(conn, value: str | date | None) -> date | None:
    if value:
        return value if isinstance(value, date) else pd.to_datetime(value).date()
    row = conn.execute("SELECT MAX(date) FROM prices_daily").fetchone()
    return None if not row or row[0] is None else pd.to_datetime(row[0]).date()


def _resolve_target_trading_days(horizon: str, requested_days: int | None) -> tuple[int | None, str]:
    if requested_days is not None:
        days = int(requested_days)
        if days <= 0:
            raise ValueError("target_days must be positive")
        return days, "explicit"
    if str(horizon).strip().lower() == "all":
        return None, "horizon"
    return _horizon_trading_days(horizon), "horizon"


def _resolve_lookback_days(horizon: str, requested_days: int | None) -> tuple[int, str]:
    if requested_days is not None:
        days = int(requested_days)
        if days <= 0:
            raise ValueError("lookback_days must be positive")
        return days, "explicit"
    normalized_horizon = str(horizon).strip().upper()
    if normalized_horizon == "ALL":
        return max(HORIZON_LOOKBACK_CALENDAR_DAYS.values()), "horizon"
    return HORIZON_LOOKBACK_CALENDAR_DAYS.get(normalized_horizon, 365), "horizon"


def _horizon_trading_days(horizon: Any) -> int:
    return HORIZON_TRADING_DAYS.get(str(horizon).strip().upper(), 63)


def _target_days_by_horizon(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        str(item["horizon"]): int(item["target_trading_days"])
        for item in items
        if item.get("horizon") is not None and item.get("target_trading_days") is not None
    }


def _normalize_market(value: str | None) -> str | None:
    if value is None or str(value).strip().lower() in {"", "all"}:
        return None
    return str(value).strip().upper()


def _first_on_or_after(frame: pd.DataFrame, target: date) -> dict[str, Any] | None:
    if frame.empty:
        return None
    selected = frame[frame["date"] >= target]
    if selected.empty:
        return None
    return selected.iloc[0].to_dict()


def _trading_session_after(
    frame: pd.DataFrame,
    entry: dict[str, Any] | None,
    target_days: int,
) -> dict[str, Any] | None:
    """Return the close exactly N observed trading sessions after entry."""
    if frame.empty or entry is None:
        return None
    sessions = frame[frame["date"] > entry["date"]]
    offset = max(1, int(target_days)) - 1
    if len(sessions) <= offset:
        return None
    return sessions.iloc[offset].to_dict()


def _elapsed_trading_days(
    frame: pd.DataFrame,
    entry: dict[str, Any] | None,
    latest: dict[str, Any] | None,
) -> int | None:
    if frame.empty or entry is None or latest is None:
        return None
    return int(((frame["date"] > entry["date"]) & (frame["date"] <= latest["date"])).sum())


def _last_on_or_before(frame: pd.DataFrame, target: date) -> dict[str, Any] | None:
    if frame.empty:
        return None
    selected = frame[frame["date"] <= target]
    if selected.empty:
        return None
    return selected.iloc[-1].to_dict()


def _return(entry: dict[str, Any] | None, exit_row: dict[str, Any] | None) -> float | None:
    entry_price = _price_value(entry)
    exit_price = _price_value(exit_row)
    if entry_price is None or exit_price is None or entry_price <= 0:
        return None
    return float(exit_price / entry_price - 1.0)


def _gap(actual: float | None, predicted: float | None) -> float | None:
    if actual is None or predicted is None:
        return None
    return float(actual - predicted)


def _abs_gap(actual: float | None, predicted: float | None) -> float | None:
    value = _gap(actual, predicted)
    return None if value is None else abs(value)


def _direction_hit(predicted: float | None, actual: float | None) -> bool | None:
    if predicted is None or actual is None:
        return None
    return _direction(predicted) == _direction(actual)


def _direction(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _price_value(row: dict[str, Any] | None) -> float | None:
    if row is None:
        return None
    return _safe_float(row.get("close"))


def _date_value(row: dict[str, Any] | None) -> str | None:
    if row is None:
        return None
    return pd.to_datetime(row.get("date")).date().isoformat()


def _value_from_price(row: dict[str, Any] | None, key: str) -> Any:
    if row is None:
        return None
    value = row.get(key)
    if pd.isna(value):
        return None
    return value


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(series) -> float | None:
    if series is None:
        return None
    value = pd.to_numeric(series, errors="coerce").mean()
    return None if pd.isna(value) else float(value)


def _mean_abs(series) -> float | None:
    if series is None:
        return None
    value = pd.to_numeric(series, errors="coerce").abs().mean()
    return None if pd.isna(value) else float(value)


def _mean_bool(series) -> float | None:
    if series is None or len(series) == 0:
        return None
    value = pd.Series(series).dropna().astype(bool).mean()
    return None if pd.isna(value) else float(value)


def _empty_payload(
    lookback_days: int,
    target_days: int | None,
    horizon: str,
    message: str,
    *,
    market: str | None = None,
    top_k: int = 20,
    target_days_source: str = "horizon",
    lookback_days_source: str = "horizon",
) -> dict[str, Any]:
    return {
        "status": "empty",
        "lookback_days": int(lookback_days),
        "lookback_days_source": lookback_days_source,
        "target_days": None if target_days is None else int(target_days),
        "target_trading_days": None if target_days is None else int(target_days),
        "target_days_source": target_days_source,
        "horizon": horizon,
        "market": market or "all",
        "top_k": int(top_k),
        "as_of_date": None,
        "start_date": None,
        "summary": summarize_price_gap([]),
        "items": [],
        "message": message,
        "disclaimer": "최근 가격 괴리 backtest는 연구·정보제공용입니다.",
    }
