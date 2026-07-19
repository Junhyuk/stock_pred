from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge

from roboquant.config import load_config
from roboquant.db import append_dedup_table, table_exists
from roboquant.premarket_shadow import FINAL_STAGE, build_premarket_feature_history

MODEL_NAME = "market-anchor-quantile-shadow-v1"
FEATURE_VERSION = "market_anchor_v1"
KOSPI = "KOSPI"
KOSDAQ = "KOSDAQ"
MARKETS = (KOSPI, KOSDAQ)
KOSPI_SELECTION_RULE = "fixed_user_anchor_v1"
KOSDAQ_SELECTION_RULE = "point_in_time_market_cap_top3_v1"
KST = ZoneInfo("Asia/Seoul")
FINAL_FLOW_CUTOFF_KST = time(18, 20)

ANCHOR_FEATURE_COLUMNS = [
    "anchor_weight_concentration",
    "anchor_weighted_return_1d",
    "anchor_weighted_return_5d",
    "anchor_weighted_return_20d",
    "anchor_weighted_volatility_5d",
    "anchor_weighted_volatility_20d",
    "anchor_weighted_turnover_ratio_20d",
    "anchor_relative_return_1d",
    "anchor_relative_return_5d",
    "anchor_return_dispersion_1d",
    "anchor_return_dispersion_20d",
    "semiconductor_anchor_return_1d",
    "auto_anchor_return_1d",
    "anchor_foreign_net_to_mcap",
    "anchor_institution_net_to_mcap",
    "anchor_retail_net_to_mcap",
]
GLOBAL_FEATURE_COLUMNS = [
    "us_sp500_return_1d",
    "us_nasdaq_return_1d",
    "us_sox_return_1d",
    "us_ewy_return_1d",
    "us_koru_return_1d",
    "us_usdkrw_return_1d",
    "us_tsm_return_1d",
    "us_dow_return_1d",
    "us_nikkei_return_1d",
    "semiconductor_global_link_1d",
    "auto_global_link_1d",
]
MODEL_FEATURE_COLUMNS = [*ANCHOR_FEATURE_COLUMNS, *GLOBAL_FEATURE_COLUMNS]
FEATURE_STORE_COLUMNS = [
    "asof_date",
    "prediction_date",
    "market",
    "forecast_stage",
    "prediction_cutoff",
    "is_final_close",
    "anchor_count",
    *MODEL_FEATURE_COLUMNS,
    "label_return",
    "components_json",
    "data_quality_json",
    "feature_version",
    "created_at",
]


def load_market_anchor_config(path: str | Path) -> dict:
    """Load the small anchor config over the normal Top50 project config."""

    overlay = load_config(path)
    base_ref = str(overlay.get("base_config") or "").strip()
    if not base_ref:
        return overlay
    root = Path(overlay["_root"])
    candidate = Path(base_ref)
    if not candidate.is_absolute():
        candidate = root / candidate if candidate.parts[:1] == ("configs",) else root / "configs" / candidate
    base = load_config(candidate)
    patch = {key: value for key, value in overlay.items() if key not in {"base_config", "_root"}}
    return _deep_merge(base, patch)


def build_market_anchor_feature_history(
    conn,
    config: Mapping[str, object],
    *,
    asof_date: str | date = "latest",
    stage: str | None = None,
) -> pd.DataFrame:
    """Build point-in-time anchor features for the next KRX-session return.

    ``build_premarket_feature_history`` supplies the cutoff-safe global fields
    and the index target.  The anchor components are joined strictly by the
    same KRX as-of date, so neither future market-cap membership nor future
    prices can enter an observation.
    """

    settings = market_anchor_settings(config)
    forecast_stage = str(stage or settings.get("forecast_stage") or FINAL_STAGE)
    base = build_premarket_feature_history(
        conn,
        dict(config),
        stage=forecast_stage,
        asof_date=asof_date,
    )
    if base.empty or not table_exists(conn, "market_anchor_constituents_daily"):
        return _empty_feature_frame()
    base = base[base["market"].astype(str).isin(MARKETS)].copy()
    if base.empty:
        return _empty_feature_frame()
    base["asof_date"] = pd.to_datetime(base["asof_date"], errors="coerce").dt.date
    base["prediction_date"] = pd.to_datetime(base["prediction_date"], errors="coerce").dt.date

    constituents = _load_constituents(conn, base["asof_date"].min(), base["asof_date"].max())
    if constituents.empty:
        return _empty_feature_frame()
    prices = _load_anchor_price_signals(conn, constituents["symbol"].unique().tolist())
    flows = _load_anchor_investor_flows(conn, constituents["symbol"].unique().tolist())
    global_signals = _load_anchor_global_signals(conn)
    indexed = {
        (item.asof_date, item.market): group.copy()
        for item, group in constituents.groupby(["asof_date", "market"], sort=False)
    }
    rows: list[dict[str, object]] = []
    for _, base_row in base.sort_values(["asof_date", "market"]).iterrows():
        asof = _to_date(base_row.get("asof_date"))
        market = str(base_row.get("market") or "")
        components = indexed.get((asof, market), pd.DataFrame())
        rows.append(
            _build_anchor_row(
                base_row,
                components=components,
                prices=prices,
                flows=flows,
                global_signals=global_signals,
                settings=settings,
                forecast_stage=forecast_stage,
            )
        )
    output = pd.DataFrame(rows)
    for column in FEATURE_STORE_COLUMNS:
        if column not in output.columns:
            output[column] = None
    return output[FEATURE_STORE_COLUMNS].sort_values(["asof_date", "market"]).reset_index(drop=True)


def refresh_market_anchor_feature_store(conn, frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    output = frame.copy()
    output["created_at"] = datetime.now(UTC).replace(tzinfo=None)
    for column in FEATURE_STORE_COLUMNS:
        if column not in output.columns:
            output[column] = None
    output = output[FEATURE_STORE_COLUMNS]
    append_dedup_table(
        conn,
        "market_anchor_features_daily",
        output,
        ["asof_date", "market", "forecast_stage", "feature_version"],
    )
    return output


def load_market_anchor_features(
    conn,
    *,
    feature_version: str | None = None,
    stage: str | None = None,
) -> pd.DataFrame:
    if not table_exists(conn, "market_anchor_features_daily"):
        return _empty_feature_frame()
    conditions = []
    params: list[object] = []
    if feature_version:
        conditions.append("feature_version = ?")
        params.append(feature_version)
    if stage:
        conditions.append("forecast_stage = ?")
        params.append(stage)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    frame = conn.execute(
        f"""
        SELECT *
        FROM market_anchor_features_daily
        {where}
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY asof_date, market, forecast_stage, feature_version
          ORDER BY created_at DESC
        ) = 1
        ORDER BY asof_date, market
        """,
        params,
    ).fetchdf()
    return _coerce_feature_frame(frame)


def fit_market_anchor_shadow_model(
    frame: pd.DataFrame,
    config: Mapping[str, object],
) -> dict:
    settings = market_anchor_settings(config)
    stage = str(settings.get("forecast_stage") or FINAL_STAGE)
    data = _eligible_rows(frame, settings=settings, stage=stage)
    if data.empty:
        raise ValueError("No eligible market-anchor rows. Collect point-in-time market caps first.")
    model_version = f"{settings['model_name']}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    bundle: dict[str, object] = {
        "model_name": settings["model_name"],
        "model_version": model_version,
        "feature_version": settings["feature_version"],
        "forecast_stage": stage,
        "production_weight": 0.0,
        "settings": _json_safe(settings),
        "models": {},
        "metrics": {},
    }
    for market in MARKETS:
        subset = data[data["market"].eq(market)].sort_values("asof_date").reset_index(drop=True)
        if len(subset) < int(settings["initial_train_days"]):
            bundle["metrics"][market] = {
                "status": "not_ready",
                "rows": int(len(subset)),
                "required_rows": int(settings["initial_train_days"]),
            }
            continue
        split = max(int(settings["initial_train_days"]), int(len(subset) * 0.8))
        split = min(split, len(subset) - 1)
        train = subset.iloc[:split].copy()
        validation = subset.iloc[split:].copy()
        model_set = _fit_model_set(train, settings)
        candidate = _predict_model_set(model_set, validation)
        actual = validation["label_return"].to_numpy(dtype=float)
        baseline = candidate["baseline_expected_return"]
        median = candidate["q50_return"]
        bundle["models"][market] = _fit_model_set(subset, settings)
        bundle["metrics"][market] = {
            "status": "ready",
            "rows": int(len(subset)),
            "train_rows": int(len(train)),
            "validation_rows": int(len(validation)),
            "feature_columns": model_set["feature_columns"],
            "candidate_q50_mae": _mae(actual, median),
            "baseline_q50_mae": _mae(actual, baseline),
            "candidate_direction_accuracy": _direction_accuracy(actual, median),
            "baseline_direction_accuracy": _direction_accuracy(actual, baseline),
        }
    if not bundle["models"]:
        raise ValueError("Insufficient eligible rows for both KOSPI and KOSDAQ anchor models")
    return bundle


def save_market_anchor_shadow_model(bundle: Mapping[str, object], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(dict(bundle), target)


def load_market_anchor_shadow_model(path: str | Path) -> dict:
    return joblib.load(path)


def default_market_anchor_model_path(config: Mapping[str, object]) -> Path:
    return Path(dict(config)["paths"]["model_dir"]) / "market_anchor_shadow" / "model.joblib"


def run_market_anchor_shadow_backtest(
    frame: pd.DataFrame,
    config: Mapping[str, object],
    *,
    model_version: str | None = None,
) -> tuple[pd.DataFrame, list[dict]]:
    """Run expanding, purged, sequentially calibrated quantile forecasts."""

    settings = market_anchor_settings(config)
    stage = str(settings.get("forecast_stage") or FINAL_STAGE)
    data = _eligible_rows(frame, settings=settings, stage=stage)
    version = model_version or f"{settings['model_name']}-backtest"
    rows: list[dict[str, object]] = []
    summaries: list[dict] = []
    for market in MARKETS:
        subset = data[data["market"].eq(market)].sort_values("asof_date").reset_index(drop=True)
        market_rows = _walk_forward_market(subset, market=market, settings=settings, model_version=version)
        rows.extend(market_rows)
        summaries.append(_summarize_backtest(market_rows, market=market, settings=settings, model_version=version))
    return pd.DataFrame(rows, columns=_forecast_columns()), summaries


def refresh_market_anchor_shadow_forecasts(conn, frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    output = frame.copy()
    for column in _forecast_columns():
        if column not in output.columns:
            output[column] = None
    append_dedup_table(
        conn,
        "market_anchor_shadow_forecasts",
        output[_forecast_columns()],
        ["prediction_date", "market", "forecast_stage", "model_version"],
    )
    return output


def refresh_market_anchor_backtest_runs(conn, summaries: list[Mapping[str, object]]) -> pd.DataFrame:
    rows = []
    now = datetime.now(UTC).replace(tzinfo=None)
    for summary in summaries:
        metrics = dict(summary.get("metrics") or {})
        rows.append(
            {
                "run_id": uuid4().hex,
                "model_name": summary["model_name"],
                "model_version": summary["model_version"],
                "market": summary["market"],
                "forecast_stage": summary["forecast_stage"],
                "start_date": summary.get("start_date"),
                "end_date": summary.get("end_date"),
                "sample_count": int(summary.get("sample_count") or 0),
                "target_coverage": float(summary.get("target_coverage") or 0.8),
                "candidate_coverage": metrics.get("candidate_coverage"),
                "control_coverage": metrics.get("control_coverage"),
                "candidate_average_width": metrics.get("candidate_average_width"),
                "control_average_width": metrics.get("control_average_width"),
                "candidate_interval_score": metrics.get("candidate_interval_score"),
                "control_interval_score": metrics.get("control_interval_score"),
                "candidate_q50_mae": metrics.get("candidate_q50_mae"),
                "baseline_q50_mae": metrics.get("baseline_q50_mae"),
                "candidate_direction_accuracy": metrics.get("candidate_direction_accuracy"),
                "baseline_direction_accuracy": metrics.get("baseline_direction_accuracy"),
                "candidate_low_breach_rate": metrics.get("candidate_low_breach_rate"),
                "control_low_breach_rate": metrics.get("control_low_breach_rate"),
                "gate_status": summary["gate_status"],
                "fail_reason": summary.get("fail_reason"),
                "metrics_json": json.dumps(_json_safe(metrics), ensure_ascii=False),
                "created_at": now,
            }
        )
    output = pd.DataFrame(rows)
    if output.empty:
        return output
    append_dedup_table(conn, "market_anchor_shadow_backtest_runs", output, ["run_id"])
    return output


def build_market_anchor_shadow_forecasts(
    frame: pd.DataFrame,
    bundle: Mapping[str, object],
    calibration_history: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=_forecast_columns())
    stage = str(bundle["forecast_stage"])
    settings = dict(bundle.get("settings") or {})
    latest_asof = pd.to_datetime(frame["asof_date"], errors="coerce").max()
    if pd.isna(latest_asof):
        return pd.DataFrame(columns=_forecast_columns())
    latest = frame[pd.to_datetime(frame["asof_date"], errors="coerce").dt.date.eq(latest_asof.date())]
    rows: list[dict[str, object]] = []
    created_at = datetime.now(UTC).replace(tzinfo=None)
    for _, item in latest.iterrows():
        market = str(item.get("market") or "")
        model_set = dict(bundle.get("models", {})).get(market)
        if not model_set or not bool(item.get("is_final_close")):
            continue
        prediction = _predict_model_set(model_set, item.to_frame().T)
        q10, q50, q90 = _ordered_quantiles(prediction)
        history = _market_calibration_history(calibration_history, market=market, model_version=str(bundle["model_version"]))
        calibration = _calibration_values(
            history,
            q10=q10,
            q90=q90,
            baseline=float(prediction["baseline_expected_return"][0]),
            settings=settings,
        )
        status = "experimental" if calibration["ready"] else "calibrating"
        rows.append(
            _forecast_row(
                item,
                market=market,
                model_name=str(bundle["model_name"]),
                model_version=str(bundle["model_version"]),
                stage=stage,
                q10=q10,
                q50=q50,
                q90=q90,
                crossing=bool(prediction["quantile_crossing_corrected"][0]),
                calibration=calibration,
                status=status,
                is_backtest=False,
                actual_return=None,
                created_at=created_at,
                baseline_expected=float(prediction["baseline_expected_return"][0]),
                baseline_sigma=float(prediction["baseline_sigma"][0]),
            )
        )
    return pd.DataFrame(rows, columns=_forecast_columns())


def get_market_anchor_shadow(
    conn,
    *,
    market: str = "all",
    limit: int = 20,
) -> dict:
    market_filter = str(market or "all").upper()
    params: list[object] = []
    where = ""
    if market_filter in MARKETS:
        where = "WHERE market = ?"
        params.append(market_filter)
    if not table_exists(conn, "market_anchor_shadow_forecasts"):
        return _empty_payload("앵커 shadow 예측 테이블이 없습니다.")
    forecasts = conn.execute(
        f"""
        SELECT *
        FROM market_anchor_shadow_forecasts
        {where}
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY market
          ORDER BY prediction_date DESC, created_at DESC
        ) = 1
        ORDER BY CASE market WHEN 'KOSPI' THEN 1 WHEN 'KOSDAQ' THEN 2 ELSE 3 END
        LIMIT ?
        """,
        [*params, max(1, int(limit))],
    ).fetchdf()
    if forecasts.empty:
        return _empty_payload("앵커 shadow 예측이 아직 생성되지 않았습니다.")
    items = forecasts.to_dict(orient="records")
    constituents = _latest_payload_constituents(conn, items)
    backtests = _latest_payload_backtests(conn, market_filter)
    return {
        "status": "ready" if any(item.get("status") == "experimental" for item in items) else "calibrating",
        "production_weight": 0.0,
        "items": [_json_safe(item) for item in items],
        "constituents": constituents,
        "backtests": backtests,
        "message": "앵커 기반 범위는 검증 중인 shadow 모델이며 production 전망에는 반영되지 않습니다.",
    }


def market_anchor_settings(config: Mapping[str, object]) -> dict:
    raw = dict(config.get("market_anchor_shadow") or {})
    return {
        "enabled": bool(raw.get("enabled", True)),
        "model_name": str(raw.get("model_name") or MODEL_NAME),
        "feature_version": str(raw.get("feature_version") or FEATURE_VERSION),
        "forecast_stage": str(raw.get("forecast_stage") or FINAL_STAGE),
        "source": str(raw.get("source") or "pykrx"),
        "start_date": str(raw.get("start_date") or "2019-01-02"),
        "sleep_seconds": float(raw.get("sleep_seconds", 0.2)),
        "kospi_anchors": [str(symbol).zfill(6) for symbol in raw.get("kospi_anchors", ["005930", "000660", "005380"])],
        "kosdaq_top_n": max(1, int(raw.get("kosdaq_top_n", 3))),
        "initial_train_days": max(30, int(raw.get("initial_train_days", 756))),
        "purge_days": max(0, int(raw.get("purge_days", 1))),
        "retrain_step_days": max(1, int(raw.get("retrain_step_days", 21))),
        "calibration_window": max(1, int(raw.get("calibration_window", 252))),
        "min_calibration_rows": max(1, int(raw.get("min_calibration_rows", 60))),
        "min_feature_support_rows": max(1, int(raw.get("min_feature_support_rows", 60))),
        "min_feature_support_fraction": float(raw.get("min_feature_support_fraction", 0.05)),
        "target_coverage": float(raw.get("target_coverage", 0.80)),
        "coverage_lower_bound": float(raw.get("coverage_lower_bound", 0.75)),
        "coverage_upper_bound": float(raw.get("coverage_upper_bound", 0.85)),
        "max_average_width_ratio": float(raw.get("max_average_width_ratio", 1.10)),
        "max_low_breach_worsening": float(raw.get("max_low_breach_worsening", 0.02)),
        "minimum_live_final_days": max(1, int(raw.get("minimum_live_final_days", 60))),
        "quantiles": [float(value) for value in raw.get("quantiles", [0.10, 0.50, 0.90])],
        "lightgbm": dict(raw.get("lightgbm") or {}),
    }


def _load_constituents(conn, start_date: date, end_date: date) -> pd.DataFrame:
    frame = conn.execute(
        """
        SELECT *
        FROM market_anchor_constituents_daily
        WHERE asof_date BETWEEN ? AND ?
          AND selection_rule IN (?, ?)
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY asof_date, market, symbol, selection_rule
          ORDER BY created_at DESC
        ) = 1
        ORDER BY asof_date, market, market_cap_rank, symbol
        """,
        [start_date, end_date, KOSPI_SELECTION_RULE, KOSDAQ_SELECTION_RULE],
    ).fetchdf()
    if not frame.empty:
        frame["asof_date"] = pd.to_datetime(frame["asof_date"], errors="coerce").dt.date
        frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    return frame


def _load_anchor_price_signals(conn, symbols: list[str]) -> pd.DataFrame:
    signal_columns = [
        "date",
        "symbol",
        "close",
        "trading_value",
        "anchor_return_1d",
        "anchor_return_5d",
        "anchor_return_20d",
        "anchor_volatility_5d",
        "anchor_volatility_20d",
        "anchor_turnover_ratio_20d",
    ]
    if not symbols or not table_exists(conn, "prices_daily"):
        return pd.DataFrame(columns=signal_columns)
    placeholders = ", ".join(["?"] * len(symbols))
    raw = conn.execute(
        f"""
        SELECT date, symbol, close, volume, trading_value, collected_at
        FROM prices_daily
        WHERE symbol IN ({placeholders})
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY date, symbol
          ORDER BY collected_at DESC NULLS LAST
        ) = 1
        ORDER BY symbol, date
        """,
        [str(symbol).zfill(6) for symbol in symbols],
    ).fetchdf()
    if raw.empty:
        return pd.DataFrame(columns=signal_columns)
    frames: list[pd.DataFrame] = []
    for symbol, group in raw.groupby("symbol", sort=False):
        item = group.sort_values("date").copy()
        item["close"] = pd.to_numeric(item["close"], errors="coerce")
        item["trading_value"] = pd.to_numeric(item["trading_value"], errors="coerce")
        fallback_value = item["close"] * pd.to_numeric(item["volume"], errors="coerce")
        item["trading_value"] = item["trading_value"].fillna(fallback_value)
        item["anchor_return_1d"] = item["close"].pct_change(1)
        item["anchor_return_5d"] = item["close"].pct_change(5)
        item["anchor_return_20d"] = item["close"].pct_change(20)
        item["anchor_volatility_5d"] = item["anchor_return_1d"].rolling(5, min_periods=5).std(ddof=0)
        item["anchor_volatility_20d"] = item["anchor_return_1d"].rolling(20, min_periods=20).std(ddof=0)
        mean_value = item["trading_value"].rolling(20, min_periods=20).mean().replace(0.0, np.nan)
        item["anchor_turnover_ratio_20d"] = item["trading_value"] / mean_value
        item["symbol"] = symbol
        frames.append(item)
    output = pd.concat(frames, ignore_index=True)
    output["date"] = pd.to_datetime(output["date"], errors="coerce").dt.date
    return output.reindex(columns=[*signal_columns, "volume", "collected_at"], fill_value=None)


def _load_anchor_investor_flows(conn, symbols: list[str]) -> pd.DataFrame:
    columns = [
        "date",
        "symbol",
        "foreign_net_value",
        "institution_net_value",
        "retail_net_value",
    ]
    if not symbols or not table_exists(conn, "investor_flows_daily"):
        return pd.DataFrame(columns=columns)
    placeholders = ", ".join(["?"] * len(symbols))
    frame = conn.execute(
        f"""
        SELECT date, symbol, foreign_net_value, institution_net_value, retail_net_value, collected_at
        FROM investor_flows_daily
        WHERE symbol IN ({placeholders})
        """,
        [str(symbol).zfill(6) for symbol in symbols],
    ).fetchdf()
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    # The historical ticker-flow table has no finality flag.  Treat rows as
    # model-eligible only when they were collected after 18:20 KST on the
    # trade date, or on a later KST date.  No zero/imputed flow is substituted.
    collected = pd.to_datetime(frame["collected_at"], errors="coerce", utc=True)
    collected_kst = collected.dt.tz_convert(KST)
    final_mask = (collected_kst.dt.date > frame["date"]) | (
        (collected_kst.dt.date == frame["date"])
        & (collected_kst.dt.time >= FINAL_FLOW_CUTOFF_KST)
    )
    frame = frame.loc[final_mask.fillna(False)].copy()
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame = frame.sort_values("collected_at").drop_duplicates(["date", "symbol"], keep="last")
    return frame[columns]


def _load_anchor_global_signals(conn) -> pd.DataFrame:
    columns = ["trade_date", "symbol", "return_1d", "source_timestamp", "ingested_at"]
    if not table_exists(conn, "global_market_daily"):
        return pd.DataFrame(columns=columns)
    frame = conn.execute(
        """
        SELECT trade_date, symbol, return_1d, source_timestamp, ingested_at
        FROM global_market_daily
        WHERE symbol IN ('TSM', '^DJI', '^N225')
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY trade_date, symbol
          ORDER BY source_timestamp DESC NULLS LAST, ingested_at DESC NULLS LAST
        ) = 1
        ORDER BY trade_date, symbol
        """
    ).fetchdf()
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.date
    frame["source_timestamp"] = pd.to_datetime(frame["source_timestamp"], errors="coerce", utc=True)
    return frame[columns]


def _build_anchor_row(
    base_row: pd.Series,
    *,
    components: pd.DataFrame,
    prices: pd.DataFrame,
    flows: pd.DataFrame,
    global_signals: pd.DataFrame,
    settings: Mapping[str, object],
    forecast_stage: str,
) -> dict[str, object]:
    asof = _to_date(base_row.get("asof_date"))
    market = str(base_row.get("market") or "")
    expected = _expected_anchor_count(market, settings)
    component = components.copy()
    if not component.empty:
        component["symbol"] = component["symbol"].astype(str).str.zfill(6)
        component = component.merge(
            prices,
            left_on=["asof_date", "symbol"],
            right_on=["date", "symbol"],
            how="left",
        ).drop(columns=["date"], errors="ignore")
        component = component.merge(
            flows,
            left_on=["asof_date", "symbol"],
            right_on=["date", "symbol"],
            how="left",
        ).drop(columns=["date"], errors="ignore")
        component["index_return_1d"] = _number(base_row.get("index_return_1d"))
        component["index_return_5d"] = _number(base_row.get("index_return_5d"))
    values = _aggregate_components(component, expected=expected)
    missing = list(values.pop("missing", []))
    is_final = _as_bool(base_row.get("is_final_close"))
    if not is_final:
        missing.append("final_global_close_unavailable")
    payload = {
        "status": "ready" if not missing else "partial_ready",
        "missing": sorted(set(missing)),
        "anchor_count": int(len(component)),
        "expected_anchor_count": expected,
        "selection_basis": "same_asof_market_cap_snapshot",
        "flow_policy": "final_t_plus_1_only_when_present",
    }
    row: dict[str, object] = {
        "asof_date": asof,
        "prediction_date": _to_date(base_row.get("prediction_date")),
        "market": market,
        "forecast_stage": forecast_stage,
        "prediction_cutoff": base_row.get("prediction_cutoff"),
        "is_final_close": is_final,
        "anchor_count": int(len(component)),
        "label_return": _number(base_row.get("label_return")),
        "components_json": json.dumps(_component_payload(component), ensure_ascii=False),
        "data_quality_json": json.dumps(payload, ensure_ascii=False),
        "feature_version": str(settings["feature_version"]),
        "created_at": datetime.now(UTC).replace(tzinfo=None),
    }
    row.update(values)
    for column in GLOBAL_FEATURE_COLUMNS:
        row[column] = _number(base_row.get(column))
    linked_globals = _linked_global_values(base_row, global_signals, market=market)
    row.update(linked_globals)
    for column, value in linked_globals.items():
        if value is None:
            missing.append(column)
    row["data_quality_json"] = json.dumps(
        {**payload, "status": "ready" if not missing else "partial_ready", "missing": sorted(set(missing))},
        ensure_ascii=False,
    )
    return row


def _linked_global_values(
    base_row: pd.Series,
    global_signals: pd.DataFrame,
    *,
    market: str,
) -> dict[str, float | None]:
    output: dict[str, float | None] = {
        "us_tsm_return_1d": None,
        "us_dow_return_1d": None,
        "us_nikkei_return_1d": None,
        "semiconductor_global_link_1d": None,
        "auto_global_link_1d": None,
    }
    global_trade_date = _optional_date(base_row.get("global_trade_date"))
    cutoff = _cutoff_utc(base_row.get("prediction_cutoff"))
    if global_trade_date is None or cutoff is None or global_signals.empty:
        return output
    eligible = global_signals[
        global_signals["trade_date"].le(global_trade_date)
        & global_signals["source_timestamp"].notna()
        & global_signals["source_timestamp"].le(cutoff)
    ].copy()
    if eligible.empty:
        return output
    latest = eligible.sort_values(["trade_date", "source_timestamp"]).groupby("symbol", as_index=False).tail(1)
    values = {
        str(item.symbol): _number(item.return_1d)
        for item in latest.itertuples(index=False)
    }
    output["us_tsm_return_1d"] = values.get("TSM")
    output["us_dow_return_1d"] = values.get("^DJI")
    output["us_nikkei_return_1d"] = values.get("^N225")
    if market != KOSPI:
        return output
    output["semiconductor_global_link_1d"] = _mean_if_complete(
        [
            _number(base_row.get("us_sox_return_1d")),
            _number(base_row.get("us_nasdaq_return_1d")),
            output["us_tsm_return_1d"],
            _number(base_row.get("us_usdkrw_return_1d")),
        ]
    )
    output["auto_global_link_1d"] = _mean_if_complete(
        [
            _number(base_row.get("us_sp500_return_1d")),
            output["us_dow_return_1d"],
            output["us_nikkei_return_1d"],
            _number(base_row.get("us_usdkrw_return_1d")),
        ]
    )
    return output


def _aggregate_components(component: pd.DataFrame, *, expected: int) -> dict[str, object]:
    output: dict[str, object] = {column: None for column in ANCHOR_FEATURE_COLUMNS}
    missing: list[str] = []
    if len(component) != expected:
        missing.append("anchor_constituent_count")
        output["missing"] = missing
        return output
    weights = pd.to_numeric(component.get("weight"), errors="coerce")
    if weights.isna().any() or weights.sum() <= 0.0:
        missing.append("anchor_weights")
        output["missing"] = missing
        return output
    weights = weights / weights.sum()
    output["anchor_weight_concentration"] = float(np.square(weights).sum())
    value_map = {
        "anchor_weighted_return_1d": "anchor_return_1d",
        "anchor_weighted_return_5d": "anchor_return_5d",
        "anchor_weighted_return_20d": "anchor_return_20d",
        "anchor_weighted_volatility_5d": "anchor_volatility_5d",
        "anchor_weighted_volatility_20d": "anchor_volatility_20d",
        "anchor_weighted_turnover_ratio_20d": "anchor_turnover_ratio_20d",
    }
    for target, source in value_map.items():
        value = _weighted_value(component.get(source), weights)
        output[target] = value
        if value is None:
            missing.append(source)
    weighted_ret_1d = output["anchor_weighted_return_1d"]
    weighted_ret_5d = output["anchor_weighted_return_5d"]
    index_return_1d = _number(component.get("index_return_1d").iloc[0]) if "index_return_1d" in component else None
    index_return_5d = _number(component.get("index_return_5d").iloc[0]) if "index_return_5d" in component else None
    output["anchor_relative_return_1d"] = (
        None if weighted_ret_1d is None or index_return_1d is None else weighted_ret_1d - index_return_1d
    )
    output["anchor_relative_return_5d"] = (
        None if weighted_ret_5d is None or index_return_5d is None else weighted_ret_5d - index_return_5d
    )
    output["anchor_return_dispersion_1d"] = _weighted_std(component.get("anchor_return_1d"), weights)
    output["anchor_return_dispersion_20d"] = _weighted_std(component.get("anchor_return_20d"), weights)
    output["semiconductor_anchor_return_1d"] = _subset_weighted_value(
        component, weights, {"005930", "000660"}, "anchor_return_1d"
    )
    output["auto_anchor_return_1d"] = _subset_weighted_value(
        component, weights, {"005380"}, "anchor_return_1d"
    )
    total_cap = pd.to_numeric(component.get("market_cap"), errors="coerce").sum()
    for target, source in (
        ("anchor_foreign_net_to_mcap", "foreign_net_value"),
        ("anchor_institution_net_to_mcap", "institution_net_value"),
        ("anchor_retail_net_to_mcap", "retail_net_value"),
    ):
        values = pd.to_numeric(component.get(source), errors="coerce")
        if values.isna().any() or total_cap <= 0.0:
            output[target] = None
            missing.append(source)
        else:
            output[target] = float(values.sum() / total_cap)
    output["missing"] = missing
    return output


def _component_payload(component: pd.DataFrame) -> list[dict]:
    if component.empty:
        return []
    columns = [
        "symbol",
        "name",
        "market_cap_rank",
        "market_cap",
        "weight",
        "anchor_return_1d",
        "anchor_return_5d",
        "anchor_return_20d",
    ]
    existing = [column for column in columns if column in component.columns]
    return [_json_safe(item) for item in component[existing].to_dict(orient="records")]


def _eligible_rows(frame: pd.DataFrame, *, settings: Mapping[str, object], stage: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    data = _coerce_feature_frame(frame)
    data = data[data["forecast_stage"].astype(str).eq(stage)].copy()
    data = data[data["market"].astype(str).isin(MARKETS)].copy()
    data = data.dropna(subset=["asof_date", "prediction_date", "label_return"])
    expected = data["market"].map(lambda value: _expected_anchor_count(str(value), settings))
    data = data[pd.to_numeric(data["anchor_count"], errors="coerce").eq(expected)].copy()
    required = [
        "anchor_weighted_return_1d",
        "anchor_weighted_return_5d",
        "anchor_weighted_return_20d",
        "anchor_weighted_volatility_20d",
    ]
    return data.dropna(subset=required).sort_values(["market", "asof_date"]).reset_index(drop=True)


def _fit_model_set(frame: pd.DataFrame, settings: Mapping[str, object]) -> dict:
    feature_columns = _supported_feature_columns(frame, settings)
    if len(feature_columns) < 4:
        raise ValueError("Anchor model has fewer than four supported features")
    features = frame.reindex(columns=feature_columns).apply(pd.to_numeric, errors="coerce")
    imputer = SimpleImputer(strategy="median", keep_empty_features=True)
    matrix = pd.DataFrame(
        imputer.fit_transform(features),
        columns=feature_columns,
        index=frame.index,
    )
    lgbm = dict(settings.get("lightgbm") or {})
    quantiles = sorted({float(value) for value in settings.get("quantiles", [0.1, 0.5, 0.9])})
    if quantiles != [0.1, 0.5, 0.9]:
        raise ValueError("market_anchor_shadow.quantiles must contain exactly 0.10, 0.50, 0.90")
    models: dict[str, LGBMRegressor] = {}
    target = frame["label_return"].to_numpy(dtype=float)
    for quantile in quantiles:
        model = LGBMRegressor(
            objective="quantile",
            alpha=quantile,
            num_leaves=int(lgbm.get("num_leaves", 7)),
            min_child_samples=int(lgbm.get("min_child_samples", 80)),
            learning_rate=float(lgbm.get("learning_rate", 0.03)),
            n_estimators=int(lgbm.get("n_estimators", 250)),
            reg_lambda=float(lgbm.get("reg_lambda", 10.0)),
            feature_fraction=float(lgbm.get("feature_fraction", 0.7)),
            random_state=int(lgbm.get("random_state", 42)),
            n_jobs=int(lgbm.get("n_jobs", 4)),
            verbosity=-1,
        )
        model.fit(matrix, target)
        models[f"{quantile:.2f}"] = model
    baseline = Ridge(alpha=10.0)
    baseline.fit(matrix, target)
    residuals = target - baseline.predict(matrix)
    return {
        "feature_columns": feature_columns,
        "imputer": imputer,
        "quantile_models": models,
        "baseline_model": baseline,
        "baseline_sigma": float(max(0.001, np.std(residuals, ddof=0))),
    }


def _predict_model_set(model_set: Mapping[str, object], frame: pd.DataFrame) -> dict[str, np.ndarray]:
    columns = list(model_set["feature_columns"])
    features = frame.reindex(columns=columns).apply(pd.to_numeric, errors="coerce")
    matrix = pd.DataFrame(
        model_set["imputer"].transform(features),
        columns=columns,
        index=frame.index,
    )
    models = dict(model_set["quantile_models"])
    output: dict[str, np.ndarray] = {
        "q10_return": np.asarray(models["0.10"].predict(matrix), dtype=float),
        "q50_return": np.asarray(models["0.50"].predict(matrix), dtype=float),
        "q90_return": np.asarray(models["0.90"].predict(matrix), dtype=float),
        "baseline_expected_return": np.asarray(model_set["baseline_model"].predict(matrix), dtype=float),
        "baseline_sigma": np.full(len(frame), float(model_set["baseline_sigma"])),
    }
    quantiles = np.column_stack([output["q10_return"], output["q50_return"], output["q90_return"]])
    output["quantile_crossing_corrected"] = np.any(np.diff(quantiles, axis=1) < 0.0, axis=1)
    ordered = np.sort(quantiles, axis=1)
    output["q10_return"], output["q50_return"], output["q90_return"] = (
        ordered[:, 0],
        ordered[:, 1],
        ordered[:, 2],
    )
    return output


def _walk_forward_market(
    subset: pd.DataFrame,
    *,
    market: str,
    settings: Mapping[str, object],
    model_version: str,
) -> list[dict[str, object]]:
    initial = int(settings["initial_train_days"])
    purge = int(settings["purge_days"])
    step = int(settings["retrain_step_days"])
    if len(subset) <= initial + purge:
        return []
    history: list[dict[str, float]] = []
    rows: list[dict[str, object]] = []
    created_at = datetime.now(UTC).replace(tzinfo=None)
    for test_start in range(initial + purge, len(subset), step):
        train_end = test_start - purge
        train = subset.iloc[:train_end]
        test = subset.iloc[test_start : min(test_start + step, len(subset))]
        if train.empty or test.empty:
            continue
        model_set = _fit_model_set(train, settings)
        prediction = _predict_model_set(model_set, test)
        for offset, (_, item) in enumerate(test.iterrows()):
            q10, q50, q90 = _ordered_quantiles(prediction, offset=offset)
            baseline = float(prediction["baseline_expected_return"][offset])
            calibration = _calibration_values(
                pd.DataFrame(history),
                q10=q10,
                q90=q90,
                baseline=baseline,
                settings=settings,
            )
            row = _forecast_row(
                item,
                market=market,
                model_name=str(settings["model_name"]),
                model_version=model_version,
                stage=str(settings["forecast_stage"]),
                q10=q10,
                q50=q50,
                q90=q90,
                crossing=bool(prediction["quantile_crossing_corrected"][offset]),
                calibration=calibration,
                status="ready" if calibration["ready"] else "calibrating",
                is_backtest=True,
                actual_return=float(item["label_return"]),
                created_at=created_at,
                baseline_expected=baseline,
                baseline_sigma=float(prediction["baseline_sigma"][offset]),
            )
            rows.append(row)
            history.append(
                {
                    "actual_return": float(item["label_return"]),
                    "q10_return": q10,
                    "q90_return": q90,
                    "baseline_expected_return": baseline,
                }
            )
    return rows


def _calibration_values(
    history: pd.DataFrame,
    *,
    q10: float,
    q90: float,
    baseline: float,
    settings: Mapping[str, object],
) -> dict[str, object]:
    if history is None or history.empty:
        return {
            "ready": False,
            "count": 0,
            "adjustment": None,
            "calibrated_low": None,
            "calibrated_high": None,
            "control_low": None,
            "control_high": None,
            "baseline_low": None,
            "baseline_high": None,
        }
    window = int(settings["calibration_window"])
    calibration = history.tail(window).copy()
    actual = pd.to_numeric(calibration.get("actual_return"), errors="coerce")
    hist_q10 = pd.to_numeric(calibration.get("q10_return"), errors="coerce")
    hist_q90 = pd.to_numeric(calibration.get("q90_return"), errors="coerce")
    hist_baseline = pd.to_numeric(calibration.get("baseline_expected_return"), errors="coerce")
    valid = pd.DataFrame(
        {"actual": actual, "q10": hist_q10, "q90": hist_q90, "baseline": hist_baseline}
    ).dropna()
    count = len(valid)
    if count < int(settings["min_calibration_rows"]):
        return {
            "ready": False,
            "count": count,
            "adjustment": None,
            "calibrated_low": None,
            "calibrated_high": None,
            "control_low": None,
            "control_high": None,
            "baseline_low": None,
            "baseline_high": None,
        }
    scores = np.maximum.reduce(
        [
            valid["q10"].to_numpy(dtype=float) - valid["actual"].to_numpy(dtype=float),
            valid["actual"].to_numpy(dtype=float) - valid["q90"].to_numpy(dtype=float),
            np.zeros(count),
        ]
    )
    adjustment = _quantile(scores, float(settings["target_coverage"]))
    residuals = valid["actual"].to_numpy(dtype=float) - valid["baseline"].to_numpy(dtype=float)
    lower_quantile = (1.0 - float(settings["target_coverage"])) / 2.0
    upper_quantile = 1.0 - lower_quantile
    control_low = baseline + _quantile(residuals, lower_quantile)
    control_high = baseline + _quantile(residuals, upper_quantile)
    sigma = max(0.001, float(np.std(residuals, ddof=0)))
    return {
        "ready": True,
        "count": count,
        "adjustment": float(adjustment),
        "calibrated_low": float(q10 - adjustment),
        "calibrated_high": float(q90 + adjustment),
        "control_low": float(control_low),
        "control_high": float(control_high),
        "baseline_low": float(baseline - sigma),
        "baseline_high": float(baseline + sigma),
    }


def _forecast_row(
    item: pd.Series,
    *,
    market: str,
    model_name: str,
    model_version: str,
    stage: str,
    q10: float,
    q50: float,
    q90: float,
    crossing: bool,
    calibration: Mapping[str, object],
    status: str,
    is_backtest: bool,
    actual_return: float | None,
    created_at: datetime,
    baseline_expected: float | None = None,
    baseline_sigma: float | None = None,
) -> dict[str, object]:
    baseline = baseline_expected if baseline_expected is not None else _number(item.get("baseline_expected_return"))
    if baseline is None:
        baseline = q50
    if baseline_sigma is not None and not calibration.get("ready"):
        baseline_low = baseline - baseline_sigma
        baseline_high = baseline + baseline_sigma
    else:
        baseline_low = calibration.get("baseline_low")
        baseline_high = calibration.get("baseline_high")
    metrics = {
        "calibration_ready": bool(calibration.get("ready")),
        "calibration_count": int(calibration.get("count") or 0),
        "anchor_count": int(item.get("anchor_count") or 0),
    }
    return {
        "prediction_date": _to_date(item.get("prediction_date")),
        "asof_date": _to_date(item.get("asof_date")),
        "market": market,
        "forecast_stage": stage,
        "prediction_cutoff": item.get("prediction_cutoff"),
        "model_name": model_name,
        "model_version": model_version,
        "q10_return": q10,
        "q50_return": q50,
        "q90_return": q90,
        "calibration_adjustment": calibration.get("adjustment"),
        "calibrated_range_low": calibration.get("calibrated_low"),
        "calibrated_range_high": calibration.get("calibrated_high"),
        "baseline_expected_return": baseline,
        "baseline_range_low": baseline_low,
        "baseline_range_high": baseline_high,
        "control_range_low": calibration.get("control_low"),
        "control_range_high": calibration.get("control_high"),
        "actual_return": actual_return,
        "calibration_count": int(calibration.get("count") or 0),
        "quantile_crossing_corrected": crossing,
        "status": status,
        "production_weight": 0.0,
        "is_backtest": bool(is_backtest),
        "metrics_json": json.dumps(metrics, ensure_ascii=False),
        "data_quality_json": item.get("data_quality_json"),
        "created_at": created_at,
    }


def _summarize_backtest(
    rows: list[dict[str, object]],
    *,
    market: str,
    settings: Mapping[str, object],
    model_version: str,
) -> dict:
    frame = pd.DataFrame(rows)
    ready = frame[frame.get("status", pd.Series(dtype=str)).eq("ready")].copy() if not frame.empty else frame
    metrics = _backtest_metrics(ready)
    sample_count = len(ready)
    reasons: list[str] = []
    has_minimum_oos = sample_count >= 252
    if not has_minimum_oos:
        reasons.append(f"insufficient calibrated OOS rows: {sample_count}/252")
    if has_minimum_oos:
        coverage = metrics.get("candidate_coverage")
        if coverage is None or not (
            float(settings["coverage_lower_bound"]) <= coverage <= float(settings["coverage_upper_bound"])
        ):
            reasons.append("candidate coverage outside target bounds")
        width = metrics.get("candidate_average_width")
        control_width = metrics.get("control_average_width")
        if width is None or control_width is None or control_width <= 0.0 or width > control_width * float(settings["max_average_width_ratio"]):
            reasons.append("candidate interval is too wide versus control")
        low_breach = metrics.get("candidate_low_breach_rate")
        control_low_breach = metrics.get("control_low_breach_rate")
        if (
            low_breach is None
            or control_low_breach is None
            or low_breach > control_low_breach + float(settings["max_low_breach_worsening"])
        ):
            reasons.append("candidate lower-tail breach worsened")
        if metrics.get("candidate_interval_score") is None or metrics.get("control_interval_score") is None or metrics["candidate_interval_score"] > metrics["control_interval_score"]:
            reasons.append("candidate interval score did not improve")
        if metrics.get("candidate_q50_mae") is None or metrics.get("baseline_q50_mae") is None or metrics["candidate_q50_mae"] > metrics["baseline_q50_mae"]:
            reasons.append("candidate q50 MAE did not improve")
        if metrics.get("candidate_direction_accuracy") is None or metrics.get("baseline_direction_accuracy") is None or metrics["candidate_direction_accuracy"] < metrics["baseline_direction_accuracy"]:
            reasons.append("candidate direction accuracy did not improve")
    gate_status = "not_ready" if not has_minimum_oos else "accepted_shadow" if not reasons else "rejected"
    return {
        "model_name": str(settings["model_name"]),
        "model_version": model_version,
        "market": market,
        "forecast_stage": str(settings["forecast_stage"]),
        "start_date": None if ready.empty else ready["asof_date"].min(),
        "end_date": None if ready.empty else ready["asof_date"].max(),
        "sample_count": sample_count,
        "target_coverage": float(settings["target_coverage"]),
        "gate_status": gate_status,
        "fail_reason": "; ".join(reasons) if reasons else None,
        "metrics": metrics,
    }


def _backtest_metrics(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {}
    actual = pd.to_numeric(frame["actual_return"], errors="coerce")
    candidate_low = pd.to_numeric(frame["calibrated_range_low"], errors="coerce")
    candidate_high = pd.to_numeric(frame["calibrated_range_high"], errors="coerce")
    control_low = pd.to_numeric(frame["control_range_low"], errors="coerce")
    control_high = pd.to_numeric(frame["control_range_high"], errors="coerce")
    q50 = pd.to_numeric(frame["q50_return"], errors="coerce")
    baseline = pd.to_numeric(frame["baseline_expected_return"], errors="coerce")
    valid = pd.DataFrame(
        {
            "actual": actual,
            "candidate_low": candidate_low,
            "candidate_high": candidate_high,
            "control_low": control_low,
            "control_high": control_high,
            "q50": q50,
            "baseline": baseline,
        }
    ).dropna()
    if valid.empty:
        return {}
    alpha = 0.2
    candidate_covered = valid["actual"].between(valid["candidate_low"], valid["candidate_high"])
    control_covered = valid["actual"].between(valid["control_low"], valid["control_high"])
    return {
        "candidate_coverage": float(candidate_covered.mean()),
        "control_coverage": float(control_covered.mean()),
        "candidate_average_width": float((valid["candidate_high"] - valid["candidate_low"]).mean()),
        "control_average_width": float((valid["control_high"] - valid["control_low"]).mean()),
        "candidate_interval_score": _interval_score(valid["actual"], valid["candidate_low"], valid["candidate_high"], alpha),
        "control_interval_score": _interval_score(valid["actual"], valid["control_low"], valid["control_high"], alpha),
        "candidate_q50_mae": _mae(valid["actual"], valid["q50"]),
        "baseline_q50_mae": _mae(valid["actual"], valid["baseline"]),
        "candidate_direction_accuracy": _direction_accuracy(valid["actual"], valid["q50"]),
        "baseline_direction_accuracy": _direction_accuracy(valid["actual"], valid["baseline"]),
        "candidate_low_breach_rate": float((valid["actual"] < valid["candidate_low"]).mean()),
        "control_low_breach_rate": float((valid["actual"] < valid["control_low"]).mean()),
    }


def _market_calibration_history(
    frame: pd.DataFrame | None,
    *,
    market: str,
    model_version: str,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    if "market" in data.columns:
        data = data[data["market"].astype(str).eq(market)]
    if "model_version" in data.columns:
        data = data[data["model_version"].astype(str).eq(model_version)]
    if "is_backtest" in data.columns:
        data = data[data["is_backtest"].fillna(False).astype(bool)]
    return data.sort_values("prediction_date") if "prediction_date" in data.columns else data


def _latest_payload_constituents(conn, forecasts: list[dict]) -> dict[str, list[dict]]:
    if not forecasts or not table_exists(conn, "market_anchor_constituents_daily"):
        return {}
    output: dict[str, list[dict]] = {}
    for forecast in forecasts:
        market = str(forecast.get("market") or "")
        asof = forecast.get("asof_date")
        if not market or asof is None:
            continue
        frame = conn.execute(
            """
            SELECT asof_date, market, symbol, name, selection_rule, market_cap_rank, market_cap, weight
            FROM market_anchor_constituents_daily
            WHERE asof_date = ? AND market = ?
            ORDER BY market_cap_rank, symbol
            """,
            [asof, market],
        ).fetchdf()
        output[market] = [_json_safe(item) for item in frame.to_dict(orient="records")]
    return output


def _latest_payload_backtests(conn, market: str) -> list[dict]:
    if not table_exists(conn, "market_anchor_shadow_backtest_runs"):
        return []
    where = ""
    params: list[object] = []
    if market in MARKETS:
        where = "WHERE market = ?"
        params.append(market)
    frame = conn.execute(
        f"""
        SELECT *
        FROM market_anchor_shadow_backtest_runs
        {where}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY market ORDER BY created_at DESC) = 1
        ORDER BY CASE market WHEN 'KOSPI' THEN 1 WHEN 'KOSDAQ' THEN 2 ELSE 3 END
        """,
        params,
    ).fetchdf()
    return [_json_safe(item) for item in frame.to_dict(orient="records")]


def _empty_payload(message: str) -> dict:
    return {
        "status": "not_ready",
        "production_weight": 0.0,
        "items": [],
        "constituents": {},
        "backtests": [],
        "message": message,
    }


def _expected_anchor_count(market: str, settings: Mapping[str, object]) -> int:
    return len(settings["kospi_anchors"]) if market == KOSPI else int(settings["kosdaq_top_n"])


def _weighted_value(values: object, weights: pd.Series) -> float | None:
    if values is None:
        return None
    series = pd.to_numeric(pd.Series(values), errors="coerce")
    if len(series) != len(weights) or series.isna().any():
        return None
    return float(np.dot(series.to_numpy(dtype=float), weights.to_numpy(dtype=float)))


def _weighted_std(values: object, weights: pd.Series) -> float | None:
    if values is None:
        return None
    series = pd.to_numeric(pd.Series(values), errors="coerce")
    if len(series) != len(weights) or series.isna().any():
        return None
    mean = float(np.dot(series.to_numpy(dtype=float), weights.to_numpy(dtype=float)))
    return float(np.sqrt(np.dot(weights.to_numpy(dtype=float), np.square(series.to_numpy(dtype=float) - mean))))


def _subset_weighted_value(
    component: pd.DataFrame,
    weights: pd.Series,
    symbols: set[str],
    column: str,
) -> float | None:
    if column not in component.columns:
        return None
    mask = component["symbol"].astype(str).isin(symbols)
    if not mask.any():
        return None
    subset_weights = weights.loc[mask]
    return _weighted_value(component.loc[mask, column], subset_weights / subset_weights.sum())


def _supported_feature_columns(frame: pd.DataFrame, settings: Mapping[str, object]) -> list[str]:
    minimum = max(
        int(settings["min_feature_support_rows"]),
        math.ceil(len(frame) * float(settings["min_feature_support_fraction"])),
    )
    columns = []
    for column in MODEL_FEATURE_COLUMNS:
        values = pd.to_numeric(frame.get(column), errors="coerce")
        if values.notna().sum() < minimum or values.dropna().nunique() < 2:
            continue
        columns.append(column)
    return columns


def _interval_score(actual: pd.Series, low: pd.Series, high: pd.Series, alpha: float) -> float:
    width = high - low
    penalty_low = (low - actual).clip(lower=0.0) * (2.0 / alpha)
    penalty_high = (actual - high).clip(lower=0.0) * (2.0 / alpha)
    return float((width + penalty_low + penalty_high).mean())


def _ordered_quantiles(prediction: Mapping[str, np.ndarray], offset: int = 0) -> tuple[float, float, float]:
    values = sorted(
        [
            float(prediction["q10_return"][offset]),
            float(prediction["q50_return"][offset]),
            float(prediction["q90_return"][offset]),
        ]
    )
    return values[0], values[1], values[2]


def _quantile(values: np.ndarray, quantile: float) -> float:
    try:
        return float(np.quantile(values, quantile, method="higher"))
    except TypeError:  # pragma: no cover - NumPy < 1.22 compatibility
        return float(np.quantile(values, quantile, interpolation="higher"))


def _mae(actual: np.ndarray | pd.Series, predicted: np.ndarray | pd.Series) -> float:
    return float(np.mean(np.abs(np.asarray(actual, dtype=float) - np.asarray(predicted, dtype=float))))


def _direction_accuracy(actual: np.ndarray | pd.Series, predicted: np.ndarray | pd.Series) -> float:
    return float(np.mean((np.asarray(actual, dtype=float) > 0.0) == (np.asarray(predicted, dtype=float) > 0.0)))


def _coerce_feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    for column in ("asof_date", "prediction_date"):
        if column in output.columns:
            output[column] = pd.to_datetime(output[column], errors="coerce").dt.date
    for column in [*MODEL_FEATURE_COLUMNS, "label_return", "anchor_count"]:
        if column in output.columns:
            output[column] = pd.to_numeric(output[column], errors="coerce")
    return output


def _empty_feature_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=FEATURE_STORE_COLUMNS)


def _forecast_columns() -> list[str]:
    return [
        "prediction_date",
        "asof_date",
        "market",
        "forecast_stage",
        "prediction_cutoff",
        "model_name",
        "model_version",
        "q10_return",
        "q50_return",
        "q90_return",
        "calibration_adjustment",
        "calibrated_range_low",
        "calibrated_range_high",
        "baseline_expected_return",
        "baseline_range_low",
        "baseline_range_high",
        "control_range_low",
        "control_range_high",
        "actual_return",
        "calibration_count",
        "quantile_crossing_corrected",
        "status",
        "production_weight",
        "is_backtest",
        "metrics_json",
        "data_quality_json",
        "created_at",
    ]


def _to_date(value: object) -> date:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"Expected a date value, got {value!r}")
    return parsed.date()


def _optional_date(value: object) -> date | None:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def _cutoff_utc(value: object) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    timestamp = pd.Timestamp(parsed)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(KST)
    return timestamp.tz_convert(UTC)


def _mean_if_complete(values: list[float | None]) -> float | None:
    if any(value is None for value in values):
        return None
    return float(np.mean(np.asarray(values, dtype=float)))


def _number(value: object) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(parsed) else float(parsed)


def _as_bool(value: object) -> bool:
    if value is None or value is pd.NA:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    return bool(value)


def _json_safe(value: object):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    return value


def _deep_merge(base: dict, overlay: Mapping[str, object]) -> dict:
    output = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(output.get(key), Mapping):
            output[key] = _deep_merge(dict(output[key]), value)
        else:
            output[key] = value
    return output
