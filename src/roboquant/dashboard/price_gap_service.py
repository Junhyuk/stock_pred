from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd


def build_prediction_price_gap(
    conn,
    *,
    lookback_days: int = 30,
    target_days: int = 30,
    horizon: str = "3M",
    symbols: list[str] | None = None,
    as_of_date: str | date | None = None,
    include_pending: bool = True,
    limit: int = 5000,
) -> dict[str, Any]:
    latest_price_date = _resolve_as_of_date(conn, as_of_date)
    if latest_price_date is None:
        return _empty_payload(lookback_days, target_days, horizon, "prices_daily is empty")
    start_date = latest_price_date - timedelta(days=int(lookback_days))
    predictions = _load_predictions(conn, start_date, latest_price_date, horizon, symbols)
    if predictions.empty:
        return {
            **_empty_payload(lookback_days, target_days, horizon, "No predictions in lookback window"),
            "as_of_date": latest_price_date.isoformat(),
            "start_date": start_date.isoformat(),
        }
    predictions["rank_no"] = predictions.groupby(
        ["prediction_date", "horizon", "model_name", "model_version"], dropna=False
    )["recommendation_score"].rank(ascending=False, method="first")
    predictions["rank_no"] = predictions["rank_no"].astype(int)
    prices = _load_prices(conn, predictions["symbol"].dropna().unique().tolist(), start_date, latest_price_date)
    items = _build_items(predictions, prices, latest_price_date, int(target_days))
    if not include_pending:
        items = [item for item in items if item["status"] == "completed"]
    summary = summarize_price_gap(items)
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
        "lookback_days": int(lookback_days),
        "target_days": int(target_days),
        "horizon": horizon,
        "as_of_date": latest_price_date.isoformat(),
        "start_date": start_date.isoformat(),
        "summary": summary,
        "items": display_items,
        "disclaimer": "최근 가격 괴리 backtest는 연구·정보제공용이며, 목표일 미도래 예측은 pending으로 표시합니다.",
    }


def summarize_price_gap(items: list[dict[str, Any]]) -> dict[str, Any]:
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
        }
    completed = frame[frame["status"].eq("completed")]
    latest_valid = frame[pd.to_numeric(frame["return_gap_latest"], errors="coerce").notna()]
    completed_valid = completed[pd.to_numeric(completed["return_gap_30d"], errors="coerce").notna()]
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
    }


def _load_predictions(
    conn,
    start_date: date,
    end_date: date,
    horizon: str,
    symbols: list[str] | None,
) -> pd.DataFrame:
    horizon_filter = ""
    params: list[Any] = [start_date, end_date]
    if horizon.lower() != "all":
        horizon_filter = "AND horizon = ?"
        params.append(horizon)
    symbol_filter = ""
    if symbols:
        normalized = [str(symbol).zfill(6) for symbol in symbols]
        symbol_filter = f"AND symbol IN ({', '.join(['?'] * len(normalized))})"
        params.extend(normalized)
    production = conn.execute(
        f"""
        SELECT
          asof_date AS prediction_date,
          symbol,
          horizon,
          'lightgbm' AS model_name,
          COALESCE(model_version, 'unknown') AS model_version,
          pred_return AS predicted_return,
          pred_prob_top20 AS predicted_probability,
          pred_risk AS risk_score,
          pred_prob_top20 AS recommendation_score,
          'production' AS source
        FROM predictions
        WHERE asof_date BETWEEN ? AND ?
          {horizon_filter}
          {symbol_filter}
        """,
        params,
    ).fetchdf()
    shadow = conn.execute(
        f"""
        SELECT
          date AS prediction_date,
          symbol,
          horizon,
          model_name,
          COALESCE(model_version, model_name) AS model_version,
          pred_score AS predicted_return,
          pred_prob AS predicted_probability,
          risk_score,
          COALESCE(recommendation_score, pred_prob) AS recommendation_score,
          'shadow' AS source
        FROM model_predictions
        WHERE date BETWEEN ? AND ?
          {horizon_filter}
          {symbol_filter}
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
    target_days: int,
) -> list[dict[str, Any]]:
    prices_by_symbol = {symbol: group.sort_values("date") for symbol, group in prices.groupby("symbol")}
    rows = []
    for raw in predictions.sort_values(["prediction_date", "horizon", "rank_no"]).to_dict(orient="records"):
        symbol = str(raw["symbol"]).zfill(6)
        symbol_prices = prices_by_symbol.get(symbol, pd.DataFrame())
        prediction_date = pd.to_datetime(raw["prediction_date"]).date()
        target_calendar_date = prediction_date + timedelta(days=target_days)
        entry = _first_on_or_after(symbol_prices, prediction_date)
        latest = _last_on_or_before(symbol_prices, latest_price_date)
        target = _first_on_or_after(symbol_prices, target_calendar_date)
        status = "pending"
        if entry is None or latest is None:
            status = "missing_price"
        elif target_calendar_date <= latest_price_date and target is None:
            status = "missing_target_price"
        elif target_calendar_date <= latest_price_date:
            status = "completed"
        actual_latest = _return(entry, latest)
        actual_30d = _return(entry, target) if status == "completed" else None
        predicted = _safe_float(raw.get("predicted_return"))
        rows.append(
            {
                "prediction_date": prediction_date.isoformat(),
                "target_calendar_date": target_calendar_date.isoformat(),
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
                "target_price_date": _date_value(target) if status == "completed" else None,
                "target_price": _price_value(target) if status == "completed" else None,
                "elapsed_days": None if entry is None else (latest_price_date - entry["date"]).days,
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


def _first_on_or_after(frame: pd.DataFrame, target: date) -> dict[str, Any] | None:
    if frame.empty:
        return None
    selected = frame[frame["date"] >= target]
    if selected.empty:
        return None
    return selected.iloc[0].to_dict()


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
    return bool((predicted >= 0 and actual >= 0) or (predicted < 0 and actual < 0))


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


def _empty_payload(lookback_days: int, target_days: int, horizon: str, message: str) -> dict[str, Any]:
    return {
        "status": "empty",
        "lookback_days": int(lookback_days),
        "target_days": int(target_days),
        "horizon": horizon,
        "as_of_date": None,
        "start_date": None,
        "summary": summarize_price_gap([]),
        "items": [],
        "message": message,
        "disclaimer": "최근 가격 괴리 backtest는 연구·정보제공용입니다.",
    }
