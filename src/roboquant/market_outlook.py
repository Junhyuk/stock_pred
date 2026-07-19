from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from roboquant.data.freshness import (
    expected_latest_trading_day,
    local_today,
    previous_trading_day,
    resolve_next_krx_session,
)
from roboquant.db import append_dedup_table, table_exists
from roboquant.signals.news_signals import (
    DEFAULT_NEGATIVE_BUSINESS_KEYWORDS,
    X_MARKET_NEWS_SOURCE,
)

MARKETS = ("KOSPI", "KOSDAQ")
OUTLOOK_HORIZONS = ("TODAY", "WEEK")
SHOCK_THRESHOLD = -0.02
MODEL_VERSION_PREFIX = "market-outlook-v2"
NEWS_WINDOW_HOURS = 24
MIN_FEATURE_SUPPORT_ROWS = 60
MIN_FEATURE_SUPPORT_FRACTION = 0.05
MAX_STANDARDIZED_FEATURE_VALUE = 5.0

INDEX_FEATURE_COLUMNS = [
    "index_return_1d",
    "index_return_5d",
    "index_return_20d",
    "index_volatility_5d",
    "index_volatility_20d",
    "index_ma_gap_5d",
    "index_ma_gap_20d",
    "global_risk_score",
    "recommended_cash_ratio",
    "semiconductor_score",
    "futures_score",
    "koru_return_1d",
    "ewy_return_1d",
    "koru_ewy_spread_1d",
    "koru_impact_score",
    "usdkrw_change_pct",
    "telegram_sentiment_score",
    "telegram_risk_score",
    "telegram_semiconductor_score",
    "news_sentiment_score",
    "news_count_24h",
    "x_news_count_24h",
    "x_news_count_3d",
    "x_news_negative_count_3d",
    "x_news_negative_attention_score",
    "x_news_bias_adjusted_sentiment_score",
]
BREADTH_FEATURE_COLUMNS = [
    "top50_up_share_21d",
    "top50_avg_momentum_score",
    "top50_avg_risk_score",
    "top50_avg_koru_impact_score",
    "prediction_up_probability_avg",
    "prediction_down_probability_avg",
    "prediction_return_avg",
]
FEATURE_COLUMNS = [*INDEX_FEATURE_COLUMNS, *BREADTH_FEATURE_COLUMNS]
LOCAL_LLM_NEWS_FEATURE_COLUMNS = [
    "llm_news_attention",
    "llm_news_risk",
    "llm_news_direction",
    "llm_news_semiconductor",
    "llm_news_macro",
    "llm_news_confidence",
    "llm_news_coverage",
]


def refresh_market_outlook_forecasts(
    conn,
    config: dict[str, Any] | None = None,
    *,
    asof_date: str | date = "latest",
    model_path: str | Path | None = None,
    now: datetime | None = None,
    forecast_context: dict[str, Any] | None = None,
) -> pd.DataFrame:
    forecasts = build_market_outlook_forecasts(
        conn,
        config=config,
        asof_date=asof_date,
        model_path=model_path,
        now=now,
        forecast_context=forecast_context,
    )
    if forecasts.empty:
        return forecasts
    target_asof = pd.to_datetime(forecasts["asof_date"].iloc[0]).date()
    conn.execute("DELETE FROM market_outlook_forecasts WHERE asof_date = ?", [target_asof])
    append_dedup_table(
        conn,
        "market_outlook_forecasts",
        forecasts,
        ["asof_date", "target_date", "horizon", "market", "model_version"],
    )
    return forecasts


def build_market_outlook_forecasts(
    conn,
    config: dict[str, Any] | None = None,
    *,
    asof_date: str | date = "latest",
    model_path: str | Path | None = None,
    now: datetime | None = None,
    forecast_context: dict[str, Any] | None = None,
) -> pd.DataFrame:
    dataset = build_market_outlook_dataset(conn, config=config, asof_date=asof_date, now=now)
    if dataset.empty:
        return _empty_forecast_frame()
    target_asof = resolve_market_outlook_asof(conn, asof_date)
    latest_rows = dataset[pd.to_datetime(dataset["asof_date"]).dt.date.eq(target_asof)].copy()
    if latest_rows.empty:
        return _empty_forecast_frame()

    model = _load_model(model_path) if model_path else None
    if model is None:
        model = fit_market_outlook_model(dataset)

    rows: list[dict[str, Any]] = []
    created_at = _utcnow()
    model_version = str(model.get("model_version") or _model_version())
    for _, row in latest_rows.iterrows():
        row_dict = row.to_dict()
        forecast = _predict_row(row_dict, model)
        drivers = _drivers(row_dict, forecast)
        quality = _row_data_quality(row_dict, model)
        context = _forecast_context_for_row(row_dict, forecast_context)
        if context:
            quality = {**quality, **_forecast_context_quality(context)}
        rows.append(
            {
                "asof_date": row_dict.get("asof_date"),
                "target_date": row_dict.get("target_date"),
                "horizon": row_dict.get("horizon"),
                "market": row_dict.get("market"),
                "expected_return": forecast["expected_return"],
                "range_low": forecast["range_low"],
                "range_high": forecast["range_high"],
                "up_probability": forecast["up_probability"],
                "down_probability": forecast["down_probability"],
                "shock_probability": forecast["shock_probability"],
                "direction": forecast["direction"],
                "confidence": _confidence(forecast, quality),
                "drivers_json": _json(drivers),
                "data_quality_json": _json(quality),
                "model_version": model_version,
                "created_at": created_at,
                "prediction_session_date": context.get("target_date") if context else None,
                "domestic_asof_date": context.get("domestic_asof_date") if context else None,
                "global_cutoff": context.get("global_cutoff") if context else None,
                "forecast_status": context.get("forecast_status") if context else "legacy",
                "calendar_source": context.get("calendar_source") if context else None,
            }
        )
    return pd.DataFrame(rows, columns=_empty_forecast_frame().columns)


def build_market_outlook_dataset(
    conn,
    config: dict[str, Any] | None = None,
    *,
    asof_date: str | date = "latest",
    now: datetime | None = None,
    include_feature_provenance: bool = False,
    include_local_llm_features: bool = False,
) -> pd.DataFrame:
    benchmark = _benchmark_frame(conn)
    if benchmark.empty:
        return _empty_dataset_frame()
    target_asof = resolve_market_outlook_asof(conn, asof_date)
    benchmark = benchmark[benchmark["date"] <= target_asof].copy()
    if benchmark.empty:
        return _empty_dataset_frame()

    components, messages = _component_status(conn)
    top50_breadth = _top50_breadth(conn)
    prediction_breadth = _prediction_breadth(conn, config)
    koru = _koru_features(conn)
    regime = _regime_features(conn)
    telegram = _telegram_features(conn)
    local_llm_news = _local_llm_news_features(conn) if include_local_llm_features else pd.DataFrame()
    holidays = market_outlook_holidays(config)
    use_realtime_target = str(asof_date).strip().lower() == "latest" or now is not None
    use_realtime_news_cutoff = use_realtime_target
    news = _news_features(
        conn,
        sorted(set(benchmark["date"])),
        latest_asof=target_asof,
        now=now,
        use_realtime_cutoff=use_realtime_news_cutoff,
        window_hours=int((config or {}).get("market_outlook", {}).get("news_window_hours", NEWS_WINDOW_HOURS)),
    )

    rows: list[dict[str, Any]] = []
    for market in MARKETS:
        market_frame = benchmark[benchmark["market"] == market].sort_values("date").copy()
        if market_frame.empty:
            continue
        feature_frame = _market_index_features(market_frame)
        available_dates = list(feature_frame["date"])
        close_by_date = dict(zip(feature_frame["date"], feature_frame["close"], strict=False))
        for _, item in feature_frame.iterrows():
            current_asof = item["date"]
            if current_asof > target_asof:
                continue
            for horizon in OUTLOOK_HORIZONS:
                if current_asof == target_asof:
                    target_today = (
                        resolve_market_outlook_target(current_asof, config, now=now)["target_date"]
                        if use_realtime_target
                        else _next_trading_day(current_asof, holidays=holidays)
                    )
                    target = target_dates_for_run(
                        current_asof,
                        today=target_today,
                        now=now,
                        holidays=holidays,
                    )[horizon]
                elif horizon == "TODAY":
                    target = _next_available_date(current_asof, available_dates)
                else:
                    target = _available_week_target(current_asof, available_dates)
                if target is None:
                    continue
                target_close = close_by_date.get(target)
                label = None
                if target_close is not None and target > current_asof and item["close"]:
                    label = float(target_close / item["close"] - 1.0)
                row = {
                    "asof_date": current_asof,
                    "target_date": target,
                    "horizon": horizon,
                    "market": market,
                    "label_return": label,
                    "feature_cutoff_date": current_asof,
                    "index_close": item["close"],
                    "index_return_1d": item["return_1d"],
                    "index_return_5d": item["return_5d"],
                    "index_return_20d": item["return_20d"],
                    "index_volatility_5d": item["volatility_5d"],
                    "index_volatility_20d": item["volatility_20d"],
                    "index_ma_gap_5d": item["ma_gap_5d"],
                    "index_ma_gap_20d": item["ma_gap_20d"],
                    "components_json": _json(components),
                    "messages_json": _json(messages),
                }
                rows.append(row)

    if not rows:
        return _empty_dataset_frame()
    dataset = pd.DataFrame(rows)
    dataset["asof_date"] = pd.to_datetime(dataset["asof_date"]).dt.date
    dataset["target_date"] = pd.to_datetime(dataset["target_date"]).dt.date
    for extra in (top50_breadth, prediction_breadth):
        dataset = _merge_market_features(dataset, extra)
    for extra in (koru, regime, telegram, news):
        dataset = _merge_date_features(dataset, extra)
    if include_local_llm_features:
        dataset = _merge_prediction_date_features(dataset, local_llm_news)
    dataset = _fill_feature_defaults(dataset, include_feature_provenance=include_feature_provenance)
    if include_local_llm_features:
        for column in LOCAL_LLM_NEWS_FEATURE_COLUMNS:
            if column not in dataset.columns:
                dataset[column] = np.nan
    return dataset.sort_values(["asof_date", "horizon", "market"]).reset_index(drop=True)


def train_market_outlook_model(
    conn,
    config: dict[str, Any] | None = None,
    *,
    model_path: str | Path | None = None,
    asof_date: str | date = "latest",
) -> dict[str, Any]:
    dataset = build_market_outlook_dataset(conn, config=config, asof_date=asof_date)
    model = fit_market_outlook_model(dataset)
    if model_path:
        path = Path(model_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json(model), encoding="utf-8")
    return model


def fit_market_outlook_model(dataset: pd.DataFrame) -> dict[str, Any]:
    model: dict[str, Any] = {
        "model_version": _model_version(),
        "created_at": _utcnow().isoformat(),
        "index_weight": 0.65,
        "breadth_weight": 0.35,
        "shock_threshold": SHOCK_THRESHOLD,
        "feature_columns": FEATURE_COLUMNS,
        "models": {},
    }
    if dataset.empty:
        return model
    data = dataset.copy()
    data["label_return"] = pd.to_numeric(data["label_return"], errors="coerce")
    data = data.dropna(subset=["label_return"])
    for horizon in OUTLOOK_HORIZONS:
        for market in MARKETS:
            key = _model_key(market, horizon)
            subset = data[(data["horizon"] == horizon) & (data["market"] == market)].copy()
            model["models"][key] = _fit_market_horizon(subset)
    return model


def get_market_outlook(
    conn,
    *,
    date: str = "latest",
    horizon: str = "all",
    market: str = "all",
    limit: int = 20,
    config: dict[str, Any] | None = None,
    require_confirmed_current: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] | None = None
    if require_confirmed_current and (not date or date == "latest"):
        asof = resolve_market_outlook_asof(conn, "latest")
        target = resolve_market_outlook_target(asof, config, now=now)
        context = market_outlook_input_status(
            conn,
            asof_date=asof,
            target_date=target["target_date"],
            config=config,
            calendar_source=target["calendar_source"],
            calendar_messages=target["calendar_messages"],
        )
        if context["forecast_status"] != "confirmed":
            return _blocked_current_outlook(context)
        frame = _stored_market_outlook(
            conn,
            date="latest",
            horizon=horizon,
            market=market,
            limit=limit,
            target_date=context["target_date"],
            forecast_status="confirmed",
        )
    else:
        frame = _stored_market_outlook(conn, date=date, horizon=horizon, market=market, limit=limit)
    source = "market_outlook_forecasts"
    if frame.empty:
        if context is not None:
            return {
                "asof_date": _date_string(context.get("domestic_asof_date")),
                "target_date": _date_string(context.get("target_date")),
                "forecast_status": "not_collected",
                "calendar_source": context.get("calendar_source"),
                "status": "not_collected",
                "source": source,
                "summary": {"count": 0, "markets": [], "horizons": []},
                "items": [],
                "data_quality": {
                    "status": "not_collected",
                    "forecast_status": "not_collected",
                    "messages": ["확정 입력은 준비됐지만 현재 목표일의 시장 전망 행이 없습니다."],
                    **_forecast_context_quality(context),
                },
            }
        frame = build_market_outlook_forecasts(conn, config=config, asof_date=date)
        frame = _filter_forecasts(frame, horizon=horizon, market=market, limit=limit)
        source = "computed_live"
    if frame.empty:
        return {
            "asof_date": None,
            "status": "not_collected",
            "source": source,
            "summary": {"count": 0, "markets": [], "horizons": []},
            "items": [],
            "data_quality": {"status": "not_collected", "messages": ["시장 전망 데이터가 없습니다."]},
        }
    items = _forecast_records(frame)
    quality = items[0].get("data_quality") or {}
    return {
        "asof_date": _date_string(frame["asof_date"].max()),
        "target_date": _date_string(frame["target_date"].min()),
        "forecast_status": items[0].get("forecast_status") or quality.get("forecast_status") or "legacy",
        "calendar_source": items[0].get("calendar_source") or quality.get("calendar_source"),
        "status": quality.get("status") or "partial_ready",
        "source": source,
        "summary": {
            "count": len(items),
            "markets": sorted({str(item.get("market")) for item in items if item.get("market")}),
            "horizons": sorted({str(item.get("horizon")) for item in items if item.get("horizon")}),
        },
        "items": items,
        "data_quality": quality,
    }


def target_dates_for_run(
    asof_date: date | str,
    *,
    today: date | str | None = None,
    now: datetime | None = None,
    holidays: set[date] | None = None,
) -> dict[str, date]:
    asof = _to_date(asof_date) or expected_latest_trading_day(now=now)
    current = _to_date(today) or local_today(now)
    reference = current if current > asof else asof + timedelta(days=1)
    today_target = _next_trading_day_on_or_after(reference, holidays=holidays)
    week_target = _week_last_trading_day(today_target, holidays=holidays)
    if week_target <= asof:
        week_target = _week_last_trading_day(today_target + timedelta(days=7), holidays=holidays)
    return {"TODAY": today_target, "WEEK": week_target}


def market_outlook_holidays(config: dict[str, Any] | None = None) -> set[date]:
    raw = ((config or {}).get("market_outlook") or {}).get("krx_holidays") or []
    holidays: set[date] = set()
    for value in raw:
        parsed = _to_date(value)
        if parsed is not None:
            holidays.add(parsed)
    return holidays


def resolve_market_outlook_target(
    asof_date: date | str,
    config: dict[str, Any] | None = None,
    *,
    target_date: date | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve the upcoming KRX target for the current market-outlook run."""
    asof = _to_date(asof_date)
    if asof is None:
        raise ValueError(f"Invalid asof_date: {asof_date}")
    explicit = _to_date(target_date)
    holidays = market_outlook_holidays(config)
    if explicit is not None:
        return {
            "target_date": explicit,
            "calendar_source": "explicit",
            "calendar_messages": [],
        }
    market_cfg = (config or {}).get("market_outlook") or {}
    resolution = resolve_next_krx_session(
        asof,
        now=now,
        holidays=holidays,
        use_pykrx=bool(market_cfg.get("use_pykrx_calendar", True)),
    )
    return {
        "target_date": resolution.target_date,
        "calendar_source": resolution.source,
        "calendar_messages": resolution.messages,
    }


def market_outlook_input_status(
    conn,
    *,
    asof_date: date | str,
    target_date: date | str,
    config: dict[str, Any] | None = None,
    calendar_source: str | None = None,
    calendar_messages: list[str] | None = None,
) -> dict[str, Any]:
    """Validate the domestic inputs required for a confirmed next-session row."""
    asof = _to_date(asof_date)
    target = _to_date(target_date)
    if asof is None or target is None:
        raise ValueError("asof_date and target_date are required for input validation")
    holidays = market_outlook_holidays(config)
    required_asof = previous_trading_day(target, holidays=holidays)
    benchmark_rows = _benchmark_rows_for_date(conn, required_asof)
    universe_total, universe_price_count = _top50_price_completeness(conn, required_asof)
    benchmark_ready = benchmark_rows == set(MARKETS)
    top50_ready = universe_total > 0 and universe_price_count == universe_total
    asof_ready = asof == required_asof
    messages = list(calendar_messages or [])
    if not asof_ready:
        messages.append(
            f"국내 기준일 {asof.isoformat()}이 확정에 필요한 {required_asof.isoformat()}보다 오래되었습니다."
        )
    if not benchmark_ready:
        missing = ", ".join(sorted(set(MARKETS).difference(benchmark_rows))) or "KOSPI/KOSDAQ"
        messages.append(f"{required_asof.isoformat()} 지수 종가 누락: {missing}")
    if not top50_ready:
        messages.append(
            f"{required_asof.isoformat()} Top50 가격 완결성 부족: {universe_price_count}/{universe_total}"
        )
    global_cutoff = _global_cutoff_for_target(conn, target)
    status = "confirmed" if asof_ready and benchmark_ready and top50_ready else "blocked_stale_input"
    return {
        "forecast_status": status,
        "target_date": target,
        "domestic_asof_date": asof,
        "required_domestic_asof_date": required_asof,
        "global_cutoff": global_cutoff,
        "calendar_source": calendar_source or "weekday_fallback",
        "calendar_messages": list(calendar_messages or []),
        "benchmark_ready": benchmark_ready,
        "benchmark_markets": sorted(benchmark_rows),
        "top50_total": universe_total,
        "top50_price_count": universe_price_count,
        "messages": messages,
    }


def _benchmark_rows_for_date(conn, target_date: date) -> set[str]:
    if not table_exists(conn, "benchmark_daily"):
        return set()
    frame = conn.execute(
        "SELECT DISTINCT benchmark FROM benchmark_daily WHERE date = ?",
        [target_date],
    ).fetchdf()
    return {str(value).upper() for value in frame.get("benchmark", pd.Series(dtype=str)).dropna()}


def _top50_price_completeness(conn, target_date: date) -> tuple[int, int]:
    if not table_exists(conn, "prices_daily") or not table_exists(conn, "current_prediction_universe"):
        return 0, 0
    try:
        row = conn.execute(
            """
            SELECT
              COUNT(DISTINCT u.symbol) AS universe_total,
              COUNT(DISTINCT p.symbol) AS price_count
            FROM current_prediction_universe AS u
            LEFT JOIN prices_daily AS p
              ON p.symbol = u.symbol AND p.date = ?
            """,
            [target_date],
        ).fetchone()
    except Exception:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


def _global_cutoff_for_target(conn, target_date: date) -> datetime | None:
    if not table_exists(conn, "market_regime_daily"):
        return None
    row = conn.execute(
        """
        SELECT prediction_cutoff
        FROM market_regime_daily
        WHERE prediction_date = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        [target_date],
    ).fetchone()
    return None if not row else row[0]


def _forecast_context_for_row(
    row: dict[str, Any],
    context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not context or str(row.get("horizon") or "").upper() != "TODAY":
        return None
    row_target = _to_date(row.get("target_date"))
    context_target = _to_date(context.get("target_date"))
    if row_target is None or row_target != context_target:
        return None
    return context


def _forecast_context_quality(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "forecast_status": context.get("forecast_status"),
        "domestic_asof_date": _date_string(context.get("domestic_asof_date")),
        "required_domestic_asof_date": _date_string(context.get("required_domestic_asof_date")),
        "global_cutoff": _json_default(context.get("global_cutoff")),
        "calendar_source": context.get("calendar_source"),
        "calendar_messages": list(context.get("calendar_messages") or []),
        "input_messages": list(context.get("messages") or []),
        "top50_price_count": context.get("top50_price_count"),
        "top50_total": context.get("top50_total"),
    }


def resolve_market_outlook_asof(conn, asof_date: str | date = "latest") -> date:
    if asof_date and asof_date != "latest":
        resolved = _to_date(asof_date)
        if resolved is None:
            raise ValueError(f"Invalid asof_date: {asof_date}")
        return resolved
    benchmark = _benchmark_frame(conn)
    if not benchmark.empty:
        return pd.to_datetime(benchmark["date"].max()).date()
    if table_exists(conn, "prices_daily"):
        value = conn.execute("SELECT MAX(date) FROM prices_daily").fetchone()[0]
        resolved = _to_date(value)
        if resolved is not None:
            return resolved
    return expected_latest_trading_day()


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(float(value) / math.sqrt(2.0)))


def default_market_outlook_model_path(config: dict[str, Any]) -> Path:
    return Path(config["paths"]["model_dir"]) / "market_outlook" / "model.json"


def _fit_market_horizon(subset: pd.DataFrame) -> dict[str, Any]:
    train, validation = _chronological_holdout(subset)
    validation_spec = _fit_market_horizon_spec(train)
    validation_fitted = _predict_subset(validation, validation_spec)
    validation_label = pd.to_numeric(validation.get("label_return"), errors="coerce")

    spec = _fit_market_horizon_spec(subset)
    spec["validation_sample_count"] = int(len(validation))
    if not validation_fitted.empty and validation_label.notna().any():
        valid = validation_label.notna()
        actual = validation_label.loc[valid].to_numpy(dtype=float)
        predicted = validation_fitted.loc[valid].to_numpy(dtype=float)
        spec["validation_direction_accuracy"] = float(
            np.mean(np.sign(predicted) == np.sign(actual))
        )
        spec["validation_rmse"] = float(np.sqrt(np.mean((predicted - actual) ** 2)))
    else:
        spec["validation_direction_accuracy"] = None
        spec["validation_rmse"] = None

    fitted = _predict_subset(subset, spec)
    if not fitted.empty and len(fitted) > 1:
        label = pd.to_numeric(subset["label_return"], errors="coerce")
        direction = np.sign(fitted.to_numpy(dtype=float)) == np.sign(label.to_numpy(dtype=float))
        spec["direction_accuracy"] = float(np.nanmean(direction)) if len(direction) else None
    else:
        spec["direction_accuracy"] = None
    return spec


def _fit_market_horizon_spec(subset: pd.DataFrame) -> dict[str, Any]:
    sample_count = int(len(subset))
    label = pd.to_numeric(subset.get("label_return"), errors="coerce").dropna()
    fallback_mean = float(label.mean()) if not label.empty else 0.0
    fallback_sigma = float(label.std(ddof=0)) if len(label) > 1 else 0.015
    fallback_sigma = max(0.006, min(0.08, fallback_sigma if math.isfinite(fallback_sigma) else 0.015))
    spec = {
        "sample_count": sample_count,
        "label_mean": fallback_mean,
        "residual_std": fallback_sigma,
        "index": _fit_linear_spec(subset, INDEX_FEATURE_COLUMNS, fallback_mean),
        "breadth": _fit_linear_spec(subset, BREADTH_FEATURE_COLUMNS, 0.0),
    }
    fitted = _predict_subset(subset, spec)
    if not fitted.empty and len(fitted) > 1:
        residual = pd.to_numeric(subset["label_return"], errors="coerce") - fitted
        sigma = float(np.nanstd(residual.to_numpy(dtype=float)))
        if math.isfinite(sigma) and sigma > 0:
            spec["residual_std"] = float(max(0.006, min(0.08, sigma)))
    return spec


def _chronological_holdout(
    subset: pd.DataFrame,
    validation_fraction: float = 0.2,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if subset.empty:
        return subset.copy(), subset.copy()
    dates = sorted(pd.to_datetime(subset["asof_date"], errors="coerce").dropna().unique())
    if len(dates) < 10:
        return subset.copy(), subset.iloc[0:0].copy()
    split_index = min(len(dates) - 1, max(1, int(len(dates) * (1.0 - validation_fraction))))
    validation_dates = set(dates[split_index:])
    date_values = pd.to_datetime(subset["asof_date"], errors="coerce")
    return (
        subset[~date_values.isin(validation_dates)].copy(),
        subset[date_values.isin(validation_dates)].copy(),
    )


def _fit_linear_spec(subset: pd.DataFrame, features: list[str], fallback: float) -> dict[str, Any]:
    if subset.empty or len(subset) < 8:
        return {"features": features, "intercept": float(fallback), "coef": [0.0] * len(features), "mean": {}, "std": {}}
    frame = subset.copy()
    y = pd.to_numeric(frame["label_return"], errors="coerce")
    matrix = frame.reindex(columns=features)
    matrix = matrix.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    fill_values = matrix.median(numeric_only=True).fillna(0.0)
    matrix = matrix.fillna(fill_values)
    valid = y.notna()
    if int(valid.sum()) < 8:
        return {"features": features, "intercept": float(fallback), "coef": [0.0] * len(features), "mean": {}, "std": {}}
    support_threshold = max(
        MIN_FEATURE_SUPPORT_ROWS,
        int(math.ceil(int(valid.sum()) * MIN_FEATURE_SUPPORT_FRACTION)),
    )
    support = matrix.loc[valid].sub(fill_values).abs().gt(1e-12).sum()
    active_features = [
        feature for feature in features if int(support.get(feature, 0)) >= support_threshold
    ]
    blocked_features = [feature for feature in features if feature not in active_features]
    if not active_features:
        return {
            "features": [],
            "intercept": float(y.loc[valid].mean()),
            "coef": [],
            "mean": {},
            "std": {},
            "fill": {},
            "support_threshold": support_threshold,
            "blocked_features": blocked_features,
        }
    matrix = matrix[active_features]
    fill_values = fill_values.reindex(active_features)
    x = matrix.loc[valid].to_numpy(dtype=float)
    yv = y.loc[valid].to_numpy(dtype=float)
    finite = np.where(np.isfinite(x), x, np.nan)
    counts = np.sum(np.isfinite(finite), axis=0)
    mean = np.divide(
        np.nansum(finite, axis=0),
        counts,
        out=np.zeros(finite.shape[1], dtype=float),
        where=counts > 0,
    )
    centered = np.where(np.isfinite(finite), finite - mean, 0.0)
    variance = np.divide(
        np.sum(centered * centered, axis=0),
        counts,
        out=np.zeros(finite.shape[1], dtype=float),
        where=counts > 0,
    )
    std = np.sqrt(variance)
    std = np.where(std < 1e-9, 1.0, std)
    x_scaled = (np.where(np.isfinite(x), x, mean) - mean) / std
    x_design = np.column_stack([np.ones(len(x_scaled)), x_scaled])
    alpha = 1.0
    penalty = np.eye(x_design.shape[1]) * alpha
    penalty[0, 0] = 0.0
    try:
        beta = np.linalg.solve(x_design.T @ x_design + penalty, x_design.T @ yv)
    except np.linalg.LinAlgError:
        beta = np.linalg.pinv(x_design.T @ x_design + penalty) @ x_design.T @ yv
    return {
        "features": active_features,
        "intercept": float(beta[0]),
        "coef": [float(item) for item in beta[1:]],
        "mean": {feature: float(value) for feature, value in zip(active_features, mean, strict=False)},
        "std": {feature: float(value) for feature, value in zip(active_features, std, strict=False)},
        "fill": {feature: float(fill_values.get(feature, 0.0)) for feature in active_features},
        "support_threshold": support_threshold,
        "blocked_features": blocked_features,
    }


def _predict_subset(subset: pd.DataFrame, spec: dict[str, Any]) -> pd.Series:
    if subset.empty:
        return pd.Series(dtype=float)
    predictions = []
    for _, row in subset.iterrows():
        predictions.append(_combined_expected_return(row.to_dict(), spec))
    return pd.Series(predictions, index=subset.index, dtype=float)


def _predict_row(row: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    spec = model.get("models", {}).get(_model_key(row.get("market"), row.get("horizon")), {})
    mu = _combined_expected_return(row, spec)
    sigma = _safe_float(spec.get("residual_std"), 0.015) or 0.015
    sigma = max(0.006, min(0.08, sigma))
    up_probability = 1.0 - normal_cdf((0.0 - mu) / sigma)
    shock_probability = normal_cdf((SHOCK_THRESHOLD - mu) / sigma)
    down_probability = 1.0 - up_probability
    direction = "NEUTRAL"
    if mu >= 0.005 or up_probability >= 0.56:
        direction = "BULLISH"
    elif mu <= -0.005 or down_probability >= 0.56:
        direction = "BEARISH"
    return {
        "expected_return": float(mu),
        "range_low": float(mu - sigma),
        "range_high": float(mu + sigma),
        "up_probability": float(max(0.0, min(1.0, up_probability))),
        "down_probability": float(max(0.0, min(1.0, down_probability))),
        "shock_probability": float(max(0.0, min(1.0, shock_probability))),
        "direction": direction,
        "index_expected_return": _linear_predict(row, spec.get("index", {})),
        "breadth_expected_return": _linear_predict(row, spec.get("breadth", {})),
        "sample_count": int(spec.get("sample_count") or 0),
        "residual_std": float(sigma),
    }


def _combined_expected_return(row: dict[str, Any], spec: dict[str, Any]) -> float:
    if not spec:
        return 0.0
    index_mu = _linear_predict(row, spec.get("index", {}))
    breadth_mu = _linear_predict(row, spec.get("breadth", {}))
    if not math.isfinite(index_mu):
        index_mu = _safe_float(spec.get("label_mean"), 0.0) or 0.0
    if not math.isfinite(breadth_mu):
        breadth_mu = 0.0
    return float(0.65 * index_mu + 0.35 * breadth_mu)


def _linear_predict(row: dict[str, Any], spec: dict[str, Any]) -> float:
    features = list(spec.get("features") or [])
    if not features:
        return float(spec.get("intercept") or 0.0)
    value = float(spec.get("intercept") or 0.0)
    coefs = list(spec.get("coef") or [0.0] * len(features))
    means = dict(spec.get("mean") or {})
    stds = dict(spec.get("std") or {})
    fills = dict(spec.get("fill") or {})
    for feature, coef in zip(features, coefs, strict=False):
        raw = _safe_float(row.get(feature), fills.get(feature, 0.0))
        mean = _safe_float(means.get(feature), 0.0) or 0.0
        std = _safe_float(stds.get(feature), 1.0) or 1.0
        if abs(std) < 1e-9:
            std = 1.0
        standardized = (float(raw or 0.0) - mean) / std
        standardized = max(
            -MAX_STANDARDIZED_FEATURE_VALUE,
            min(MAX_STANDARDIZED_FEATURE_VALUE, standardized),
        )
        value += float(coef) * standardized
    return float(value)


def _drivers(row: dict[str, Any], forecast: dict[str, Any]) -> list[dict[str, Any]]:
    drivers = [
        {
            "kind": "index_model",
            "label": "지수 직접 모델",
            "summary": f"{row.get('market')} {row.get('horizon')} 지수 momentum/volatility 기반",
            "value": {
                "index_expected_return": forecast.get("index_expected_return"),
                "index_return_1d": row.get("index_return_1d"),
                "index_volatility_20d": row.get("index_volatility_20d"),
            },
        },
        {
            "kind": "breadth",
            "label": "Top50 breadth 모델",
            "summary": "Top50 상승 breadth와 기존 2M 예측 분포를 단기 시장 방향 보조 신호로 사용",
            "value": {
                "breadth_expected_return": forecast.get("breadth_expected_return"),
                "top50_up_share_21d": row.get("top50_up_share_21d"),
                "prediction_up_probability_avg": row.get("prediction_up_probability_avg"),
                "prediction_down_probability_avg": row.get("prediction_down_probability_avg"),
            },
        },
        {
            "kind": "koru",
            "label": "KORU/EWY",
            "summary": "미국장 한국 ETF와 3배 레버리지 심리",
            "value": {
                "koru_return_1d": row.get("koru_return_1d"),
                "ewy_return_1d": row.get("ewy_return_1d"),
                "koru_ewy_spread_1d": row.get("koru_ewy_spread_1d"),
                "koru_impact_score": row.get("koru_impact_score"),
            },
        },
        {
            "kind": "regime",
            "label": "글로벌 레짐",
            "summary": "해외 지수·반도체·선물·환율 위험 레짐",
            "value": {
                "global_risk_score": row.get("global_risk_score"),
                "recommended_cash_ratio": row.get("recommended_cash_ratio"),
                "semiconductor_score": row.get("semiconductor_score"),
                "futures_score": row.get("futures_score"),
            },
        },
        {
            "kind": "news_telegram",
            "label": "뉴스/Telegram",
            "summary": "거시 뉴스와 Telegram 시장 attention을 원문 전문 없이 집계",
            "value": {
                "news_count_24h": row.get("news_count_24h"),
                "news_sentiment_score": row.get("news_sentiment_score"),
                "x_news_count_24h": row.get("x_news_count_24h"),
                "x_news_negative_attention_score": row.get("x_news_negative_attention_score"),
                "x_news_bias_adjusted_sentiment_score": row.get("x_news_bias_adjusted_sentiment_score"),
                "telegram_sentiment_score": row.get("telegram_sentiment_score"),
                "telegram_risk_score": row.get("telegram_risk_score"),
            },
        },
    ]
    return drivers[:5]


def _row_data_quality(row: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    components = _loads(row.get("components_json"), {})
    messages = list(_loads(row.get("messages_json"), []))
    key = _model_key(row.get("market"), row.get("horizon"))
    spec = model.get("models", {}).get(key, {})
    if int(spec.get("sample_count") or 0) < 30:
        messages.append("단기 전망 학습 표본이 적어 신뢰도를 낮췄습니다.")
    missing_components = [name for name, status in components.items() if status != "ready"]
    status = "ready" if not missing_components else "partial_ready"
    return {
        "status": status,
        "components": components,
        "messages": messages[:8],
        "model_sample_count": int(spec.get("sample_count") or 0),
        "feature_cutoff_date": _date_string(row.get("feature_cutoff_date") or row.get("asof_date")),
        "lookahead_guard": "features use asof_date-or-earlier only",
    }


def _confidence(forecast: dict[str, Any], quality: dict[str, Any]) -> float:
    sample_count = int(quality.get("model_sample_count") or 0)
    sample_score = min(0.35, sample_count / 500.0 * 0.35)
    probability_score = min(0.2, abs(float(forecast.get("up_probability") or 0.5) - 0.5) * 0.6)
    missing_count = sum(1 for status in (quality.get("components") or {}).values() if status != "ready")
    confidence = 0.35 + sample_score + probability_score - 0.04 * missing_count
    return float(max(0.2, min(0.95, confidence)))


def _stored_market_outlook(
    conn,
    *,
    date: str,
    horizon: str,
    market: str,
    limit: int,
    target_date: date | str | None = None,
    forecast_status: str | None = None,
) -> pd.DataFrame:
    if not table_exists(conn, "market_outlook_forecasts"):
        return pd.DataFrame()
    asof_value = date
    if not asof_value or asof_value == "latest":
        row = conn.execute("SELECT MAX(asof_date) FROM market_outlook_forecasts").fetchone()
        if not row or row[0] is None:
            return pd.DataFrame()
        asof_value = _date_string(row[0]) or "latest"
    where = ["asof_date = ?"]
    params: list[Any] = [pd.to_datetime(asof_value).date()]
    if horizon and horizon != "all":
        where.append("horizon = ?")
        params.append(str(horizon).upper())
    if market and market != "all":
        where.append("market = ?")
        params.append(str(market).upper())
    if target_date is not None:
        where.append("target_date = ?")
        params.append(pd.to_datetime(target_date).date())
    if forecast_status is not None:
        where.append("forecast_status = ?")
        params.append(str(forecast_status))
    params.append(int(max(1, limit or 20)))
    return conn.execute(
        f"""
        SELECT *
        FROM market_outlook_forecasts
        WHERE {" AND ".join(where)}
        ORDER BY
          CASE WHEN horizon = 'TODAY' THEN 0 WHEN horizon = 'WEEK' THEN 1 ELSE 2 END,
          CASE WHEN market = 'KOSPI' THEN 0 WHEN market = 'KOSDAQ' THEN 1 ELSE 2 END
        LIMIT ?
        """,
        params,
    ).fetchdf()


def _filter_forecasts(frame: pd.DataFrame, *, horizon: str, market: str, limit: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    output = frame.copy()
    if horizon and horizon != "all":
        output = output[output["horizon"].astype(str).str.upper().eq(str(horizon).upper())]
    if market and market != "all":
        output = output[output["market"].astype(str).str.upper().eq(str(market).upper())]
    return output.head(int(max(1, limit or 20)))


def _forecast_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records = []
    for record in _records(frame):
        record["drivers"] = _loads(record.get("drivers_json"), [])
        record["data_quality"] = _loads(record.get("data_quality_json"), {})
        records.append(record)
    return records


def _blocked_current_outlook(context: dict[str, Any]) -> dict[str, Any]:
    quality = {
        "status": "blocked_stale_input",
        "forecast_status": "blocked_stale_input",
        "components": {"domestic_inputs": "blocked_stale_input"},
        "messages": list(context.get("messages") or []),
        **_forecast_context_quality(context),
    }
    return {
        "asof_date": _date_string(context.get("domestic_asof_date")),
        "target_date": _date_string(context.get("target_date")),
        "forecast_status": "blocked_stale_input",
        "calendar_source": context.get("calendar_source"),
        "status": "blocked_stale_input",
        "source": "market_outlook_forecasts",
        "summary": {"count": 0, "markets": [], "horizons": []},
        "items": [],
        "data_quality": quality,
    }


def _market_index_features(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.sort_values("date").copy()
    output["close"] = pd.to_numeric(output["close"], errors="coerce")
    output["return_1d"] = output["close"].pct_change(1)
    output["return_5d"] = output["close"].pct_change(5)
    output["return_20d"] = output["close"].pct_change(20)
    output["volatility_5d"] = output["return_1d"].rolling(5, min_periods=2).std()
    output["volatility_20d"] = output["return_1d"].rolling(20, min_periods=5).std()
    output["ma_gap_5d"] = output["close"] / output["close"].rolling(5, min_periods=2).mean() - 1.0
    output["ma_gap_20d"] = output["close"] / output["close"].rolling(20, min_periods=5).mean() - 1.0
    return output


def _benchmark_frame(conn) -> pd.DataFrame:
    if not table_exists(conn, "benchmark_daily"):
        return pd.DataFrame(columns=["date", "market", "close"])
    frame = conn.execute(
        """
        SELECT date, benchmark, close
        FROM benchmark_daily
        WHERE benchmark IN ('KOSPI', 'KOSDAQ', '1001', '2001')
          AND close IS NOT NULL
        ORDER BY date, benchmark
        """
    ).fetchdf()
    if frame.empty:
        return pd.DataFrame(columns=["date", "market", "close"])
    frame["market"] = frame["benchmark"].map({"1001": "KOSPI", "2001": "KOSDAQ"}).fillna(frame["benchmark"])
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame.groupby(["date", "market"], as_index=False)["close"].mean()


def _top50_breadth(conn) -> pd.DataFrame:
    if not table_exists(conn, "features_daily"):
        return pd.DataFrame()
    frame = conn.execute(
        """
        SELECT
          f.date,
          COALESCE(u.market, s.market, 'KOSPI') AS market,
          AVG(CASE WHEN COALESCE(f.ret_21d, 0) > 0 THEN 1.0 ELSE 0.0 END) AS top50_up_share_21d,
          AVG(COALESCE(f.momentum_score, 0.5)) AS top50_avg_momentum_score,
          AVG(COALESCE(f.risk_score, 0.5)) AS top50_avg_risk_score,
          AVG(COALESCE(f.koru_impact_score, 0.5)) AS top50_avg_koru_impact_score
        FROM features_daily AS f
        LEFT JOIN current_prediction_universe AS u ON f.symbol = u.symbol
        LEFT JOIN symbols AS s ON f.symbol = s.symbol
        WHERE f.horizon = '2M'
        GROUP BY f.date, COALESCE(u.market, s.market, 'KOSPI')
        ORDER BY f.date
        """
    ).fetchdf()
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def _prediction_breadth(conn, config: dict[str, Any] | None) -> pd.DataFrame:
    if not table_exists(conn, "predictions"):
        return pd.DataFrame()
    horizon = str((config or {}).get("market_outlook", {}).get("prediction_horizon", "2M"))
    frame = conn.execute(
        """
        SELECT
          p.asof_date AS date,
          COALESCE(u.market, s.market, 'KOSPI') AS market,
          AVG(COALESCE(p.pred_prob_top20, 0.5)) AS prediction_up_probability_avg,
          AVG(COALESCE(p.pred_prob_bottom20, 0.5)) AS prediction_down_probability_avg,
          AVG(COALESCE(p.pred_return, 0.0)) AS prediction_return_avg
        FROM predictions AS p
        LEFT JOIN current_prediction_universe AS u ON p.symbol = u.symbol
        LEFT JOIN symbols AS s ON p.symbol = s.symbol
        WHERE p.horizon = ?
        GROUP BY p.asof_date, COALESCE(u.market, s.market, 'KOSPI')
        ORDER BY p.asof_date
        """,
        [horizon],
    ).fetchdf()
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def _koru_features(conn) -> pd.DataFrame:
    if not table_exists(conn, "koru_korea_linkage"):
        return pd.DataFrame()
    frame = conn.execute(
        """
        SELECT
          trade_date AS date,
          koru_return_1d,
          ewy_return_1d,
          koru_ewy_spread_1d,
          koru_impact_score,
          usdkrw_change_pct
        FROM koru_korea_linkage
        ORDER BY trade_date
        """
    ).fetchdf()
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def _regime_features(conn) -> pd.DataFrame:
    if not table_exists(conn, "market_regime_daily"):
        return pd.DataFrame()
    frame = conn.execute(
        """
        SELECT
          prediction_date AS date,
          global_risk_score,
          recommended_cash_ratio,
          semiconductor_score,
          futures_score
        FROM market_regime_daily
        ORDER BY prediction_date
        """
    ).fetchdf()
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def _telegram_features(conn) -> pd.DataFrame:
    if not table_exists(conn, "telegram_market_signal_daily"):
        return pd.DataFrame()
    frame = conn.execute(
        """
        SELECT
          signal_date AS date,
          telegram_attention_score,
          telegram_sentiment_score,
          telegram_urgency_score,
          telegram_risk_score,
          telegram_semiconductor_score,
          telegram_macro_score
        FROM telegram_market_signal_daily
        ORDER BY signal_date
        """
    ).fetchdf()
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def _local_llm_news_features(conn) -> pd.DataFrame:
    """Return only observed local-LLM market signals for shadow candidates."""
    if not table_exists(conn, "local_llm_news_signal_daily"):
        return pd.DataFrame()
    frame = conn.execute(
        """
        SELECT
          signal_date AS date,
          llm_news_attention,
          llm_news_risk,
          llm_news_direction,
          llm_news_semiconductor,
          llm_news_macro,
          llm_news_confidence,
          llm_news_coverage,
          assessment_count,
          source_coverage_json
        FROM local_llm_news_signal_daily
        WHERE scope = 'market' AND symbol = 'ALL'
        ORDER BY signal_date, created_at
        """
    ).fetchdf()
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    return frame.drop_duplicates("date", keep="last")


def _news_features(
    conn,
    asof_dates: list[date],
    *,
    latest_asof: date,
    now: datetime | None = None,
    use_realtime_cutoff: bool = False,
    window_hours: int = NEWS_WINDOW_HOURS,
) -> pd.DataFrame:
    if not table_exists(conn, "market_news_feed"):
        return pd.DataFrame()
    frame = conn.execute(
        """
        SELECT pub_date, source, title, summary, sentiment_score
        FROM market_news_feed
        WHERE pub_date IS NOT NULL
        ORDER BY pub_date
        """
    ).fetchdf()
    if frame.empty or not asof_dates:
        return pd.DataFrame()
    frame["pub_date"] = pd.to_datetime(frame["pub_date"], errors="coerce")
    frame["source"] = frame["source"].fillna("").astype(str)
    frame["text"] = (
        frame.get("title", pd.Series("", index=frame.index)).fillna("").astype(str)
        + " "
        + frame.get("summary", pd.Series("", index=frame.index)).fillna("").astype(str)
    )
    frame["sentiment_score"] = pd.to_numeric(frame["sentiment_score"], errors="coerce").fillna(0.5)
    frame = frame.dropna(subset=["pub_date"])
    rows: list[dict[str, Any]] = []
    window = timedelta(hours=max(1, int(window_hours)))
    for current_asof in asof_dates:
        cutoff = _news_cutoff_for_asof(
            current_asof,
            latest_asof=latest_asof,
            now=now,
            use_realtime_cutoff=use_realtime_cutoff,
        )
        start = cutoff - window
        subset = frame[(frame["pub_date"] > start) & (frame["pub_date"] <= cutoff)]
        x_subset_24h = subset[subset["source"].str.lower().eq(X_MARKET_NEWS_SOURCE)].copy()
        x_start_3d = cutoff - timedelta(days=3)
        x_subset_3d = frame[
            (frame["pub_date"] > x_start_3d)
            & (frame["pub_date"] <= cutoff)
            & frame["source"].str.lower().eq(X_MARKET_NEWS_SOURCE)
        ].copy()
        x_negative = _negative_x_news_mask(x_subset_3d)
        x_negative_count = int(x_negative.sum()) if not x_subset_3d.empty else 0
        rows.append(
            {
                "date": current_asof,
                "news_count_24h": int(len(subset)),
                "news_sentiment_score": float(subset["sentiment_score"].mean()) if not subset.empty else 0.5,
                "x_news_count_24h": int(len(x_subset_24h)),
                "x_news_count_3d": int(len(x_subset_3d)),
                "x_news_negative_count_3d": x_negative_count,
                "x_news_negative_attention_score": min(1.0, x_negative_count * 2.0 / 3.0),
                "x_news_bias_adjusted_sentiment_score": _weighted_x_sentiment(x_subset_3d, x_negative),
            }
        )
    return pd.DataFrame(rows)


def _news_cutoff_for_asof(
    asof: date,
    *,
    latest_asof: date,
    now: datetime | None,
    use_realtime_cutoff: bool,
) -> datetime:
    if use_realtime_cutoff and asof == latest_asof:
        return _to_utc_naive(now) if now is not None else _utcnow()
    return datetime.combine(asof + timedelta(days=1), time.min)


def _merge_market_features(dataset: pd.DataFrame, extra: pd.DataFrame) -> pd.DataFrame:
    if extra.empty:
        return dataset
    merge_cols = [column for column in extra.columns if column not in {"date", "market"}]
    frame = extra[["date", "market", *merge_cols]].copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return dataset.merge(frame, left_on=["asof_date", "market"], right_on=["date", "market"], how="left").drop(columns=["date"], errors="ignore")


def _merge_date_features(dataset: pd.DataFrame, extra: pd.DataFrame) -> pd.DataFrame:
    if extra.empty:
        return dataset
    merge_cols = [column for column in extra.columns if column != "date"]
    frame = extra[["date", *merge_cols]].copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return dataset.merge(frame, left_on="asof_date", right_on="date", how="left").drop(columns=["date"], errors="ignore")


def _merge_prediction_date_features(dataset: pd.DataFrame, extra: pd.DataFrame) -> pd.DataFrame:
    """Join cutoff-shifted news to the market forecast's target session."""
    if extra.empty:
        return dataset
    merge_cols = [column for column in extra.columns if column != "date"]
    frame = extra[["date", *merge_cols]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    return dataset.merge(frame, left_on="target_date", right_on="date", how="left").drop(columns=["date"], errors="ignore")


def _fill_feature_defaults(
    dataset: pd.DataFrame,
    *,
    include_feature_provenance: bool = False,
) -> pd.DataFrame:
    output = dataset.copy()
    provenance: dict[str, pd.Series] = {}
    defaults = {
        "global_risk_score": 50.0,
        "recommended_cash_ratio": 0.15,
        "semiconductor_score": 50.0,
        "futures_score": 50.0,
        "koru_impact_score": 0.5,
        "telegram_sentiment_score": 0.5,
        "news_sentiment_score": 0.5,
        "x_news_bias_adjusted_sentiment_score": 0.5,
        "prediction_up_probability_avg": 0.5,
        "prediction_down_probability_avg": 0.5,
    }
    for column in FEATURE_COLUMNS:
        if column not in output.columns:
            output[column] = defaults.get(column, 0.0)
        raw = pd.to_numeric(output[column], errors="coerce")
        if include_feature_provenance:
            provenance[f"__raw_{column}"] = raw
            provenance[f"__default_applied_{column}"] = raw.isna()
        output[column] = raw.fillna(defaults.get(column, 0.0))
    if provenance:
        output = pd.concat([output, pd.DataFrame(provenance, index=output.index)], axis=1)
    return output


def _negative_x_news_mask(frame: pd.DataFrame) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype=bool)
    sentiment_negative = pd.to_numeric(frame["sentiment_score"], errors="coerce").fillna(0.5) <= 0.45
    text_negative = frame["text"].fillna("").astype(str).map(_has_negative_business_keyword)
    return sentiment_negative | text_negative


def _weighted_x_sentiment(frame: pd.DataFrame, negative_mask: pd.Series) -> float:
    if frame.empty:
        return 0.5
    sentiments = pd.to_numeric(frame["sentiment_score"], errors="coerce").fillna(0.5)
    weights = pd.Series(1.0, index=frame.index, dtype=float)
    positive = sentiments >= 0.60
    weights.loc[positive] = 0.75
    if not negative_mask.empty:
        weights.loc[negative_mask.reindex(frame.index).fillna(False)] = 2.0
    denominator = float(weights.sum())
    if denominator <= 0:
        return 0.5
    return float((sentiments * weights).sum() / denominator)


def _has_negative_business_keyword(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(str(keyword).lower() in lowered for keyword in DEFAULT_NEGATIVE_BUSINESS_KEYWORDS)


def _component_status(conn) -> tuple[dict[str, str], list[str]]:
    checks = {
        "benchmark": ("benchmark_daily", "지수 가격"),
        "top50_breadth": ("features_daily", "Top50 breadth"),
        "prediction_breadth": ("predictions", "기존 예측 breadth"),
        "koru": ("koru_korea_linkage", "KORU linkage"),
        "regime": ("market_regime_daily", "글로벌 레짐"),
        "telegram": ("telegram_market_signal_daily", "Telegram 시장 신호"),
        "news": ("market_news_feed", "거시 뉴스"),
    }
    components: dict[str, str] = {}
    messages: list[str] = []
    for key, (table, label) in checks.items():
        ready = False
        if table_exists(conn, table):
            try:
                ready = bool(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except Exception:
                ready = False
        components[key] = "ready" if ready else "missing"
        if not ready:
            messages.append(f"{label} 데이터 부족: 중립값으로 전망을 생성했습니다.")
    return components, messages


def _next_available_date(asof: date, available_dates: list[date]) -> date | None:
    for item in available_dates:
        if item > asof:
            return item
    return None


def _available_week_target(asof: date, available_dates: list[date]) -> date | None:
    target = _week_last_trading_day(asof)
    if target <= asof:
        target = _week_last_trading_day(asof + timedelta(days=7))
    candidates = [item for item in available_dates if asof < item <= target]
    if candidates:
        return candidates[-1]
    return _next_available_date(asof, available_dates)


def _week_last_trading_day(day: date, *, holidays: set[date] | None = None) -> date:
    friday = day + timedelta(days=(4 - day.weekday()) % 7)
    while not _is_trading_day(friday, holidays=holidays):
        friday -= timedelta(days=1)
    return friday


def _next_trading_day(day: date, *, holidays: set[date] | None = None) -> date:
    return _next_trading_day_on_or_after(day + timedelta(days=1), holidays=holidays)


def _next_trading_day_on_or_after(day: date, *, holidays: set[date] | None = None) -> date:
    current = day
    while not _is_trading_day(current, holidays=holidays):
        current += timedelta(days=1)
    return current


def _is_trading_day(day: date, *, holidays: set[date] | None = None) -> bool:
    return day.weekday() < 5 and day not in (holidays or set())


def _model_key(market: Any, horizon: Any) -> str:
    return f"{str(market).upper()}|{str(horizon).upper()}"


def _model_version() -> str:
    return f"{MODEL_VERSION_PREFIX}-{date.today().isoformat()}"


def _load_model(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    model_path = Path(path)
    if not model_path.exists():
        return None
    try:
        return json.loads(model_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _empty_dataset_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "asof_date",
            "target_date",
            "horizon",
            "market",
            "label_return",
            "feature_cutoff_date",
            *FEATURE_COLUMNS,
            "components_json",
            "messages_json",
        ]
    )


def _empty_forecast_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "asof_date",
            "target_date",
            "horizon",
            "market",
            "expected_return",
            "range_low",
            "range_high",
            "up_probability",
            "down_probability",
            "shock_probability",
            "direction",
            "confidence",
            "drivers_json",
            "data_quality_json",
            "model_version",
            "created_at",
            "prediction_session_date",
            "domestic_asof_date",
            "global_cutoff",
            "forecast_status",
            "calendar_source",
        ]
    )


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    return [_json_safe(row) for row in frame.to_dict(orient="records")]


def _json(value: Any) -> str:
    return json.dumps(_sanitize(value), ensure_ascii=False, allow_nan=False)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


def _json_safe(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_default(value) for key, value in row.items()}


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
        return None if not math.isfinite(value) else value
    if isinstance(value, float):
        return None if not math.isfinite(value) else value
    if isinstance(value, np.ndarray):
        return [_json_default(item) for item in value.tolist()]
    if pd.isna(value) if not isinstance(value, (list, dict, tuple)) else False:
        return None
    return value


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return _json_default(value)


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return pd.to_datetime(value).date()


def _date_string(value: Any) -> str | None:
    resolved = _to_date(value)
    return None if resolved is None else resolved.isoformat()


def _to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
