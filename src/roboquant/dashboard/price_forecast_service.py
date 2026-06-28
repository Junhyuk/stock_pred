from __future__ import annotations

from datetime import date
from math import sqrt
from typing import Any

import numpy as np
import pandas as pd

DEFAULT_FORECAST_HORIZONS = ("3M", "6M", "9M", "1Y")
DEFAULT_HORIZON_DAYS = {
    "2M": 42,
    "3M": 63,
    "6M": 126,
    "9M": 189,
    "1Y": 252,
}

DISCLAIMER = (
    "예상 가격은 모델 기반 정보제공용 전망 범위이며 매수·매도 목표가나 수익 보장이 아닙니다."
)


def get_top20_price_forecast(
    conn,
    *,
    horizons: str | list[str] | tuple[str, ...] | None = None,
    limit: int = 20,
    base_horizon: str = "3M",
    asof_date: str | date | None = None,
) -> dict[str, Any]:
    selected_horizons = _normalize_horizons(horizons)
    limit = int(max(1, min(int(limit), 100)))
    base_horizon = str(base_horizon or "3M")
    recommendation_date = _resolve_recommendation_date(conn, base_horizon, asof_date)
    price_date = _resolve_price_date(conn, asof_date)
    if recommendation_date is None:
        return _empty_payload(
            selected_horizons,
            base_horizon,
            price_date,
            "missing_recommendations",
            f"No recommendations found for base_horizon={base_horizon}",
        )

    base = _load_base_top20(conn, base_horizon, recommendation_date, limit)
    if base.empty:
        return _empty_payload(
            selected_horizons,
            base_horizon,
            price_date,
            "missing_recommendations",
            f"No Top20 rows found for base_horizon={base_horizon} asof={recommendation_date}",
        )

    symbols = base["symbol"].astype(str).str.zfill(6).tolist()
    prices = _load_latest_prices(conn, symbols, price_date)
    predictions = _load_prediction_rows(conn, symbols, selected_horizons, price_date or recommendation_date)
    error_bands = _load_backtest_error_bands(conn, selected_horizons)

    price_by_symbol = {str(row["symbol"]).zfill(6): row for row in prices.to_dict(orient="records")}
    prediction_by_key = {
        (str(row["symbol"]).zfill(6), str(row["horizon"])): row
        for row in predictions.to_dict(orient="records")
    }

    items = []
    for raw in base.to_dict(orient="records"):
        symbol = str(raw["symbol"]).zfill(6)
        price_row = price_by_symbol.get(symbol)
        current_price = _safe_float(price_row.get("close")) if price_row else None
        item = {
            "rank": _safe_int(raw.get("rank")),
            "symbol": symbol,
            "name": raw.get("name"),
            "market": raw.get("market"),
            "sector": raw.get("sector"),
            "base_horizon": base_horizon,
            "base_final_score": _safe_float(raw.get("final_score")),
            "base_model_version": raw.get("model_version"),
            "latest_price_date": _date_string(price_row.get("date")) if price_row else None,
            "current_price": current_price,
            "forecasts": [],
        }
        for horizon in selected_horizons:
            prediction = prediction_by_key.get((symbol, horizon))
            item["forecasts"].append(
                _build_forecast_row(
                    horizon=horizon,
                    current_price=current_price,
                    prediction=prediction,
                    error_band=error_bands.get(horizon),
                )
            )
        items.append(item)

    return {
        "status": "ready",
        "asof_date": recommendation_date.isoformat(),
        "price_date": None if price_date is None else price_date.isoformat(),
        "base_horizon": base_horizon,
        "horizons": selected_horizons,
        "summary": _summary(items, selected_horizons),
        "items": items,
        "disclaimer": DISCLAIMER,
    }


def _build_forecast_row(
    *,
    horizon: str,
    current_price: float | None,
    prediction: dict[str, Any] | None,
    error_band: float | None,
) -> dict[str, Any]:
    if prediction is None:
        return {
            "horizon": horizon,
            "horizon_days": DEFAULT_HORIZON_DAYS.get(horizon),
            "status": "missing_prediction",
            "error_band": None,
            "error_band_source": None,
            "expected_return": None,
            "expected_price": None,
            "upside_price": None,
            "downside_price": None,
            "up_probability": None,
            "down_probability": None,
            "confidence": None,
            "risk_score": None,
            "model_version": None,
        }
    horizon_days = _safe_int(prediction.get("horizon_days")) or DEFAULT_HORIZON_DAYS.get(horizon)
    expected_return = _safe_float(prediction.get("pred_return"))
    volatility_band = _volatility_error_band(prediction, horizon_days)
    band_source = "backtest_rmse"
    band = _safe_float(error_band)
    if band is None:
        band = volatility_band
        band_source = "volatility_60d" if band is not None else None
    if band is not None:
        band = float(np.clip(band, 0.0, 1.0))

    row = {
        "horizon": horizon,
        "horizon_days": horizon_days,
        "status": "ready",
        "error_band": band,
        "error_band_source": band_source,
        "expected_return": expected_return,
        "expected_price": None,
        "upside_price": None,
        "downside_price": None,
        "up_probability": _safe_float(prediction.get("pred_prob_top20")),
        "down_probability": _safe_float(prediction.get("pred_prob_bottom20")),
        "confidence": _safe_float(prediction.get("confidence")),
        "risk_score": _safe_float(prediction.get("feature_risk_score") or prediction.get("pred_risk")),
        "model_version": prediction.get("model_version"),
        "prediction_asof_date": _date_string(prediction.get("asof_date")),
    }
    if current_price is None or current_price <= 0:
        row["status"] = "missing_price"
        return row
    if expected_return is None:
        row["status"] = "missing_prediction"
        return row

    band = band or 0.0
    row["expected_price"] = _price(current_price * (1.0 + expected_return))
    row["upside_price"] = _price(current_price * (1.0 + max(expected_return, 0.0) + band))
    row["downside_price"] = _price(current_price * (1.0 + min(expected_return, 0.0) - band))
    return row


def _normalize_horizons(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return list(DEFAULT_FORECAST_HORIZONS)
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",")]
    else:
        raw = [str(item).strip() for item in value]
    selected = [item for item in raw if item]
    return selected or list(DEFAULT_FORECAST_HORIZONS)


def _resolve_recommendation_date(conn, base_horizon: str, value: str | date | None) -> date | None:
    if value and str(value).lower() != "latest":
        return pd.to_datetime(value).date()
    row = conn.execute(
        "SELECT MAX(asof_date) FROM recommendations WHERE horizon = ?",
        [base_horizon],
    ).fetchone()
    return None if not row or row[0] is None else pd.to_datetime(row[0]).date()


def _resolve_price_date(conn, value: str | date | None) -> date | None:
    if value and str(value).lower() != "latest":
        target = pd.to_datetime(value).date()
        row = conn.execute("SELECT MAX(date) FROM prices_daily WHERE date <= ?", [target]).fetchone()
    else:
        row = conn.execute("SELECT MAX(date) FROM prices_daily").fetchone()
    return None if not row or row[0] is None else pd.to_datetime(row[0]).date()


def _load_base_top20(conn, base_horizon: str, asof_date: date, limit: int) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT
          r.asof_date,
          r.horizon,
          r.symbol,
          r.final_score,
          r.rank,
          r.model_version,
          s.name,
          s.market,
          COALESCE(s.sector, s.market, '기타') AS sector
        FROM recommendations AS r
        LEFT JOIN symbols AS s ON r.symbol = s.symbol
        WHERE r.horizon = ?
          AND r.asof_date = ?
        ORDER BY r.final_score DESC, r.rank NULLS LAST, r.symbol
        LIMIT ?
        """,
        [base_horizon, asof_date, limit],
    ).fetchdf()


def _load_latest_prices(conn, symbols: list[str], price_date: date | None) -> pd.DataFrame:
    if not symbols or price_date is None:
        return pd.DataFrame()
    placeholders = ", ".join(["?"] * len(symbols))
    return conn.execute(
        f"""
        SELECT date, symbol, close
        FROM (
          SELECT
            p.date,
            p.symbol,
            p.close,
            ROW_NUMBER() OVER (PARTITION BY p.symbol ORDER BY p.date DESC) AS rn
          FROM prices_daily AS p
          WHERE p.symbol IN ({placeholders})
            AND p.date <= ?
        )
        WHERE rn = 1
        """,
        [*symbols, price_date],
    ).fetchdf()


def _load_prediction_rows(
    conn,
    symbols: list[str],
    horizons: list[str],
    asof_date: date,
) -> pd.DataFrame:
    if not symbols or not horizons:
        return pd.DataFrame()
    horizon_placeholders = ", ".join(["?"] * len(horizons))
    symbol_placeholders = ", ".join(["?"] * len(symbols))
    frame = conn.execute(
        f"""
        WITH latest AS (
          SELECT horizon, MAX(asof_date) AS asof_date
          FROM predictions
          WHERE horizon IN ({horizon_placeholders})
            AND asof_date <= ?
          GROUP BY horizon
        )
        SELECT
          p.*,
          f.horizon_days,
          f.volatility_60d,
          f.risk_score AS feature_risk_score
        FROM predictions AS p
        INNER JOIN latest AS l
          ON p.horizon = l.horizon
         AND p.asof_date = l.asof_date
        LEFT JOIN features_daily AS f
          ON p.asof_date = f.date
         AND p.symbol = f.symbol
         AND p.horizon = f.horizon
        WHERE p.symbol IN ({symbol_placeholders})
        """,
        [*horizons, asof_date, *symbols],
    ).fetchdf()
    if frame.empty:
        return frame
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    return frame


def _load_backtest_error_bands(conn, horizons: list[str]) -> dict[str, float]:
    if not horizons:
        return {}
    placeholders = ", ".join(["?"] * len(horizons))
    frame = conn.execute(
        f"""
        SELECT
          horizon,
          SQRT(AVG(POWER(predicted_return - actual_return, 2))) AS rmse
        FROM backtest_results
        WHERE horizon IN ({placeholders})
          AND predicted_return IS NOT NULL
          AND actual_return IS NOT NULL
        GROUP BY horizon
        """,
        horizons,
    ).fetchdf()
    if frame.empty:
        return {}
    return {
        str(row["horizon"]): float(row["rmse"])
        for row in frame.to_dict(orient="records")
        if _safe_float(row.get("rmse")) is not None
    }


def _volatility_error_band(prediction: dict[str, Any], horizon_days: int | None) -> float | None:
    volatility = _safe_float(prediction.get("volatility_60d"))
    if volatility is None or horizon_days is None:
        return None
    return float(max(0.0, volatility) * sqrt(max(1, int(horizon_days)) / 252.0))


def _summary(items: list[dict[str, Any]], horizons: list[str]) -> dict[str, Any]:
    ready = 0
    missing_prediction = 0
    missing_price = 0
    expected_returns = []
    for item in items:
        for forecast in item.get("forecasts", []):
            if forecast.get("status") == "ready":
                ready += 1
                value = _safe_float(forecast.get("expected_return"))
                if value is not None:
                    expected_returns.append(value)
            elif forecast.get("status") == "missing_price":
                missing_price += 1
            else:
                missing_prediction += 1
    return {
        "top20_count": int(len(items)),
        "horizon_count": int(len(horizons)),
        "forecast_count": int(sum(len(item.get("forecasts", [])) for item in items)),
        "ready_count": ready,
        "missing_prediction_count": missing_prediction,
        "missing_price_count": missing_price,
        "average_expected_return": _mean(expected_returns),
    }


def _empty_payload(
    horizons: list[str],
    base_horizon: str,
    price_date: date | None,
    status: str,
    message: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "asof_date": None,
        "price_date": None if price_date is None else price_date.isoformat(),
        "base_horizon": base_horizon,
        "horizons": horizons,
        "summary": {
            "top20_count": 0,
            "horizon_count": int(len(horizons)),
            "forecast_count": 0,
            "ready_count": 0,
            "missing_prediction_count": 0,
            "missing_price_count": 0,
            "average_expected_return": None,
        },
        "items": [],
        "disclaimer": DISCLAIMER,
    }


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def _safe_int(value: Any) -> int | None:
    number = _safe_float(value)
    return None if number is None else int(number)


def _price(value: float) -> float:
    if not np.isfinite(value):
        return 0.0
    return float(max(0.0, value))


def _mean(values: list[float]) -> float | None:
    return None if not values else float(np.mean(values))


def _date_string(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.to_datetime(value).date().isoformat()
