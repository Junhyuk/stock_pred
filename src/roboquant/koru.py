from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from roboquant.db import append_dedup_table, table_exists

KORU_FEATURE_COLUMNS = [
    "koru_return_1d",
    "koru_volume_ratio_20d",
    "ewy_return_1d",
    "koru_ewy_spread_1d",
    "koru_leverage_drift_1d",
    "koru_impact_score",
    "koru_market_shock_flag",
    "kospi_return_1d",
    "kosdaq_return_1d",
]
KORU_TRAINING_FEATURE_COLUMNS = [
    "koru_return_1d",
    "koru_volume_ratio_20d",
    "ewy_return_1d",
    "koru_ewy_spread_1d",
    "koru_leverage_drift_1d",
    "koru_impact_score",
    "koru_market_shock_flag",
    "kospi_return_1d",
    "kosdaq_return_1d",
]
KORU_LEVERAGE_WARNING = (
    "KORU는 일간 3배 레버리지 ETF입니다. 장기 보유 시 기초지수 수익률의 "
    "단순 3배와 다르게 움직일 수 있으며, 변동성, 일일 리밸런싱, 복리 효과로 "
    "손실이 확대될 수 있습니다. 본 화면은 투자 참고용 분석이며 매수/매도 추천이 아닙니다."
)
KOREA_MARKET_SHOCK_THRESHOLD = -0.02
KST = ZoneInfo("Asia/Seoul")


def refresh_koru_korea_linkage(
    conn,
    config: dict[str, Any] | None = None,
    *,
    asof_date: str = "latest",
) -> pd.DataFrame:
    frame = build_koru_korea_linkage(conn, config=config, asof_date=asof_date)
    append_dedup_table(conn, "koru_korea_linkage", frame, ["trade_date"])
    return frame


def build_koru_korea_linkage(
    conn,
    config: dict[str, Any] | None = None,
    *,
    asof_date: str = "latest",
) -> pd.DataFrame:
    dates = _analysis_dates(conn, asof_date)
    if not dates:
        return _empty_linkage_frame()
    global_frame = _global_frame(conn)
    snapshot_frame = _intraday_snapshot_frame(conn)
    benchmark_returns = _benchmark_returns(conn)
    stock_returns = _stock_returns(conn)
    flow_by_date = _flow_by_date(conn)
    rows: list[dict[str, Any]] = []
    created_at = _utcnow()

    for target_date in dates:
        signals = _aligned_global_signals(global_frame, snapshot_frame, target_date)
        benchmarks = benchmark_returns.get(target_date, {})
        stocks = stock_returns.get(target_date, {})
        flows = flow_by_date.get(target_date, {})
        features = {
            "trade_date": target_date,
            "us_signal_date": signals.get("us_signal_date"),
            "koru_return_1d": signals.get("KORU.return_1d"),
            "koru_volume_ratio_20d": signals.get("KORU.volume_ratio_20d"),
            "ewy_return_1d": signals.get("EWY.return_1d"),
            "spy_return_1d": signals.get("SPY.return_1d"),
            "qqq_return_1d": signals.get("QQQ.return_1d"),
            "koru_ewy_spread_1d": _spread(signals.get("KORU.return_1d"), signals.get("EWY.return_1d"), multiplier=1.0),
            "koru_leverage_drift_1d": _spread(signals.get("KORU.return_1d"), signals.get("EWY.return_1d"), multiplier=3.0),
            "kospi_return_1d": benchmarks.get("KOSPI"),
            "kosdaq_return_1d": benchmarks.get("KOSDAQ"),
            "samsung_return_1d": stocks.get("005930"),
            "hynix_return_1d": stocks.get("000660"),
            "usdkrw_change_pct": signals.get("USDKRW=X.return_1d"),
            "foreign_net_buy_krw": flows.get("foreign_net_value"),
            "institution_net_buy_krw": flows.get("institution_net_value"),
            "retail_net_buy_krw": flows.get("retail_net_value"),
            "koru_signal_source": signals.get("KORU.signal_source"),
            "ewy_signal_source": signals.get("EWY.signal_source"),
            "us_signal_timestamp": signals.get("KORU.signal_timestamp"),
        }
        trigger = market_index_trigger(features)
        features["koru_market_shock_flag"] = bool(trigger["triggered"])
        features["koru_impact_score"] = calculate_koru_impact_score(features)
        causes = classify_koru_move_causes(features)
        rows.append(
            {
                **features,
                "market_index_trigger_json": _json(trigger),
                "causes_json": _json(causes),
                "data_quality_json": _json(_data_quality(features)),
                "created_at": created_at,
            }
        )

    return pd.DataFrame(rows, columns=_empty_linkage_frame().columns)


def attach_koru_features(
    features: pd.DataFrame,
    linkage: pd.DataFrame | None,
    *,
    missing_factor_default: float = 0.5,
) -> pd.DataFrame:
    if features.empty:
        return features
    output = features.copy()
    if linkage is not None and not linkage.empty:
        items = linkage.copy()
        items["date"] = pd.to_datetime(items["trade_date"]).dt.date
        keep = ["date", *KORU_FEATURE_COLUMNS]
        items = items[[column for column in keep if column in items.columns]]
        output["date"] = pd.to_datetime(output["date"]).dt.date
        output = output.merge(items, on="date", how="left")
    for column in KORU_FEATURE_COLUMNS:
        if column not in output.columns:
            output[column] = None
    neutral = {
        "koru_impact_score": missing_factor_default,
        "koru_market_shock_flag": 0.0,
    }
    for column in KORU_FEATURE_COLUMNS:
        default = neutral.get(column, 0.0)
        output[column] = pd.to_numeric(output[column], errors="coerce").fillna(default)
    return output


def calculate_koru_impact_score(features: dict[str, Any]) -> float:
    scores = {
        "ewy_sync_score": _positive_return_score(features.get("ewy_return_1d"), scale=0.025),
        "korea_index_score": _average(
            [
                _positive_return_score(features.get("kospi_return_1d"), scale=0.025),
                _positive_return_score(features.get("kosdaq_return_1d"), scale=0.03),
            ],
            default=0.5,
        ),
        "semiconductor_score": _average(
            [
                _positive_return_score(features.get("samsung_return_1d"), scale=0.03),
                _positive_return_score(features.get("hynix_return_1d"), scale=0.03),
            ],
            default=0.5,
        ),
        "foreign_flow_score": _flow_score(features.get("foreign_net_buy_krw")),
        "usdkrw_score": _fx_score(features.get("usdkrw_change_pct")),
        "us_risk_on_score": _average(
            [
                _positive_return_score(features.get("spy_return_1d"), scale=0.02),
                _positive_return_score(features.get("qqq_return_1d"), scale=0.025),
            ],
            default=0.5,
        ),
        "news_sentiment_score": _safe_float(features.get("news_sentiment_score"), default=0.5),
    }
    score = (
        scores["ewy_sync_score"] * 0.20
        + scores["korea_index_score"] * 0.15
        + scores["semiconductor_score"] * 0.20
        + scores["foreign_flow_score"] * 0.15
        + scores["usdkrw_score"] * 0.10
        + scores["us_risk_on_score"] * 0.10
        + scores["news_sentiment_score"] * 0.10
    )
    return float(max(0.0, min(1.0, score)))


def classify_koru_move_causes(features: dict[str, Any]) -> list[dict[str, Any]]:
    causes: list[dict[str, Any]] = []
    ewy = _safe_float(features.get("ewy_return_1d"))
    samsung = _safe_float(features.get("samsung_return_1d"))
    hynix = _safe_float(features.get("hynix_return_1d"))
    foreign = _safe_float(features.get("foreign_net_buy_krw"))
    usdkrw = _safe_float(features.get("usdkrw_change_pct"))
    spy = _safe_float(features.get("spy_return_1d"))
    qqq = _safe_float(features.get("qqq_return_1d"))

    if samsung is not None and samsung > 0.02 or hynix is not None and hynix > 0.02:
        causes.append(_cause("SEMICONDUCTOR", "삼성전자/SK하이닉스 등 반도체 대형주 강세", 0.20))
    if ewy is not None and ewy > 0.01:
        causes.append(_cause("ETF_SYNC", "EWY와 한국시장 ETF 동반 상승", 0.20))
    if foreign is not None and foreign > 100_000_000_000:
        causes.append(_cause("FOREIGN_FLOW", "외국인 한국시장 순매수 확대", 0.15))
    if usdkrw is not None and usdkrw < -0.003:
        causes.append(_cause("FX", "원화 강세에 따른 한국자산 선호 개선", 0.10))
    if spy is not None and spy > 0.005 and qqq is not None and qqq > 0.005:
        causes.append(_cause("US_RISK_ON", "미국 시장 위험자산 선호 회복", 0.10))

    trigger = market_index_trigger(features)
    if trigger["triggered"]:
        causes.append(_cause("MARKET_SHOCK", "KOSPI/KOSDAQ -2% 시장충격", 0.20))
    if ewy is not None and ewy < -0.01:
        causes.append(_cause("ETF_SYNC_DOWN", "EWY와 한국시장 ETF 동반 약세", 0.18))
    if usdkrw is not None and usdkrw > 0.003:
        causes.append(_cause("FX_DOWN", "원화 약세에 따른 한국자산 부담", 0.10))
    return sorted(causes, key=lambda item: float(item["impact"]), reverse=True)[:5]


def market_index_trigger(features: dict[str, Any], threshold: float = KOREA_MARKET_SHOCK_THRESHOLD) -> dict[str, Any]:
    kospi = _safe_float(features.get("kospi_return_1d"))
    kosdaq = _safe_float(features.get("kosdaq_return_1d"))
    markets = {
        "KOSPI": {"return_1d": kospi, "triggered": kospi is not None and kospi <= threshold},
        "KOSDAQ": {"return_1d": kosdaq, "triggered": kosdaq is not None and kosdaq <= threshold},
    }
    return {
        "threshold": threshold,
        "triggered": any(item["triggered"] for item in markets.values()),
        "markets": markets,
    }


def decide_koru_overlay_weights(
    baseline_metrics: dict[str, dict[str, float | int | None]],
    enhanced_metrics: dict[str, dict[str, float | int | None]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for horizon in ("2M", "3M", "6M"):
        baseline = baseline_metrics.get(horizon, {})
        enhanced = enhanced_metrics.get(horizon, {})
        passed = _gate_checks(baseline, enhanced)
        pass_count = sum(bool(value) for value in passed.values())
        if horizon == "6M":
            weight = 0.0
            decision = "excluded_long_horizon"
        elif pass_count == 3:
            weight = 0.07 if horizon == "2M" else 0.04
            decision = "passed"
        elif pass_count >= 2:
            weight = 0.03 if horizon == "2M" else 0.02
            decision = "partial"
        else:
            weight = 0.0
            decision = "failed"
        result[horizon] = {
            "horizon": horizon,
            "decision": decision,
            "overlay_weight": weight,
            "checks": passed,
        }
    return result


def latest_koru_overlay_weights(conn) -> dict[str, float]:
    if not table_exists(conn, "koru_weight_decisions"):
        return {}
    frame = conn.execute(
        """
        SELECT horizon, overlay_weight
        FROM koru_weight_decisions
        QUALIFY ROW_NUMBER() OVER (PARTITION BY horizon ORDER BY decision_date DESC, created_at DESC) = 1
        """
    ).fetchdf()
    if frame.empty:
        return {}
    return {str(row["horizon"]): float(row["overlay_weight"] or 0.0) for _, row in frame.iterrows()}


def build_koru_weight_decision_rows(
    baseline_metrics: dict[str, dict[str, Any]],
    enhanced_metrics: dict[str, dict[str, Any]],
    *,
    decision_date: date | None = None,
) -> pd.DataFrame:
    decisions = decide_koru_overlay_weights(baseline_metrics, enhanced_metrics)
    rows = []
    stamp = _utcnow()
    day = decision_date or stamp.date()
    for horizon, decision in decisions.items():
        rows.append(
            {
                "decision_date": day,
                "horizon": horizon,
                "decision": decision["decision"],
                "overlay_weight": decision["overlay_weight"],
                "baseline_metrics_json": _json(baseline_metrics.get(horizon, {})),
                "enhanced_metrics_json": _json(enhanced_metrics.get(horizon, {})),
                "reason_json": _json(decision.get("checks", {})),
                "created_at": stamp,
            }
        )
    return pd.DataFrame(rows)


def get_latest_koru_linkage(conn, *, asof_date: str = "latest") -> dict[str, Any]:
    if not table_exists(conn, "koru_korea_linkage"):
        return _empty_payload()
    params: list[Any] = []
    date_filter = ""
    if asof_date and asof_date != "latest":
        date_filter = "WHERE trade_date <= ?"
        params.append(pd.to_datetime(asof_date).date())
    frame = conn.execute(
        f"""
        SELECT *
        FROM koru_korea_linkage
        {date_filter}
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        params,
    ).fetchdf()
    if frame.empty:
        return _empty_payload()
    row = _sanitize_record(frame.iloc[0].to_dict())
    row["causes"] = _loads(row.get("causes_json"), [])
    row["market_index_trigger"] = _loads(row.get("market_index_trigger_json"), {})
    row["data_quality"] = _loads(row.get("data_quality_json"), {})
    weights = latest_koru_overlay_weights(conn)
    return {
        "status": row["data_quality"].get("status") or "partial_ready",
        "asof_date": row.get("trade_date"),
        "item": row,
        "weight_decisions": weights,
        "leverage_warning": KORU_LEVERAGE_WARNING,
    }


def _analysis_dates(conn, asof_date: str) -> list[date]:
    dates: set[date] = set()
    target = None if not asof_date or asof_date == "latest" else pd.to_datetime(asof_date).date()
    for table, column in (("benchmark_daily", "date"), ("prices_daily", "date")):
        if not table_exists(conn, table):
            continue
        query = f"SELECT DISTINCT {column} FROM {table}"
        params: list[Any] = []
        if target is not None:
            query += f" WHERE {column} <= ?"
            params.append(target)
        for (value,) in conn.execute(query, params).fetchall():
            parsed = _to_date(value)
            if parsed is not None:
                dates.add(parsed)
    return sorted(dates)


def _global_frame(conn) -> pd.DataFrame:
    if not table_exists(conn, "global_market_daily"):
        return pd.DataFrame()
    frame = conn.execute(
        """
        SELECT *
        FROM global_market_daily
        WHERE symbol IN ('KORU', 'EWY', 'SPY', 'QQQ', 'USDKRW=X')
        ORDER BY symbol, trade_date
        """
    ).fetchdf()
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    frame["volume_ratio_20d"] = (
        frame.groupby("symbol")["volume"]
        .transform(lambda series: pd.to_numeric(series, errors="coerce") / pd.to_numeric(series, errors="coerce").rolling(20, min_periods=5).mean())
    )
    return frame


def _intraday_snapshot_frame(conn) -> pd.DataFrame:
    if not table_exists(conn, "global_market_intraday_snapshot"):
        return pd.DataFrame()
    frame = conn.execute(
        """
        SELECT *
        FROM global_market_intraday_snapshot
        WHERE symbol IN ('KORU', 'EWY', 'SPY', 'QQQ', 'USDKRW=X')
        ORDER BY symbol, snapshot_at
        """
    ).fetchdf()
    if frame.empty:
        return frame
    frame["snapshot_at_utc"] = _as_utc_timestamp(frame["snapshot_at"])
    frame["source_timestamp_utc"] = _as_utc_timestamp(frame["source_timestamp"])
    frame["snapshot_date_kst"] = frame["snapshot_at_utc"].dt.tz_convert(KST).dt.date
    return frame


def _aligned_global_signals(
    global_frame: pd.DataFrame,
    snapshot_frame: pd.DataFrame,
    target_date: date,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if global_frame.empty and snapshot_frame.empty:
        return result
    cutoff = target_date - timedelta(days=1)
    for symbol in ("KORU", "EWY", "SPY", "QQQ", "USDKRW=X"):
        daily_row = _latest_daily_signal(global_frame, symbol, cutoff)
        snapshot_row = _latest_current_snapshot(snapshot_frame, symbol, target_date)
        if symbol == "KORU" and daily_row is not None:
            result["KORU.volume_ratio_20d"] = _safe_float(daily_row.get("volume_ratio_20d"))
        if snapshot_row is not None and _safe_float(snapshot_row.get("change_rate")) is not None:
            source_ts = _first_notna(snapshot_row.get("source_timestamp_utc"), snapshot_row.get("snapshot_at_utc"))
            result[f"{symbol}.return_1d"] = _safe_float(snapshot_row.get("change_rate"))
            result[f"{symbol}.signal_source"] = "intraday_snapshot_current_price"
            result[f"{symbol}.signal_timestamp"] = source_ts
            if symbol == "KORU":
                result["us_signal_date"] = _timestamp_date(source_ts) or snapshot_row.get("snapshot_date_kst")
            continue
        if daily_row is None:
            continue
        if symbol == "KORU":
            result["us_signal_date"] = daily_row.get("trade_date")
        result[f"{symbol}.return_1d"] = _safe_float(daily_row.get("return_1d"))
        result[f"{symbol}.signal_source"] = "daily_close_lagged"
        result[f"{symbol}.signal_timestamp"] = daily_row.get("trade_date")
    return result


def _latest_daily_signal(global_frame: pd.DataFrame, symbol: str, cutoff: date):
    if global_frame.empty:
        return None
    rows = global_frame[(global_frame["symbol"].eq(symbol)) & (global_frame["trade_date"] <= cutoff)]
    if rows.empty:
        return None
    return rows.sort_values("trade_date").iloc[-1]


def _latest_current_snapshot(snapshot_frame: pd.DataFrame, symbol: str, target_date: date):
    if snapshot_frame.empty:
        return None
    rows = snapshot_frame[
        snapshot_frame["symbol"].astype(str).eq(symbol)
        & (snapshot_frame["snapshot_date_kst"] <= target_date)
    ]
    if rows.empty:
        return None
    return rows.sort_values(["snapshot_at_utc", "source_timestamp_utc"], na_position="first").iloc[-1]


def _benchmark_returns(conn) -> dict[date, dict[str, float | None]]:
    if not table_exists(conn, "benchmark_daily"):
        return {}
    frame = conn.execute(
        """
        SELECT date, benchmark, close
        FROM benchmark_daily
        WHERE benchmark IN ('KOSPI', 'KOSDAQ')
        ORDER BY benchmark, date
        """
    ).fetchdf()
    if frame.empty:
        return {}
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["return_1d"] = frame.groupby("benchmark")["close"].pct_change(1)
    result: dict[date, dict[str, float | None]] = {}
    for _, row in frame.iterrows():
        result.setdefault(row["date"], {})[str(row["benchmark"])] = _safe_float(row.get("return_1d"))
    return result


def _stock_returns(conn) -> dict[date, dict[str, float | None]]:
    if not table_exists(conn, "prices_daily"):
        return {}
    frame = conn.execute(
        """
        SELECT date, symbol, close
        FROM prices_daily
        WHERE symbol IN ('005930', '000660')
        ORDER BY symbol, date
        """
    ).fetchdf()
    if frame.empty:
        return {}
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    frame["return_1d"] = frame.groupby("symbol")["close"].pct_change(1)
    result: dict[date, dict[str, float | None]] = {}
    for _, row in frame.iterrows():
        result.setdefault(row["date"], {})[str(row["symbol"])] = _safe_float(row.get("return_1d"))
    return result


def _flow_by_date(conn) -> dict[date, dict[str, float | None]]:
    if not table_exists(conn, "investor_flows_daily"):
        return {}
    frame = conn.execute(
        """
        SELECT date, foreign_net_value, institution_net_value, retail_net_value
        FROM investor_flows_daily
        ORDER BY date
        """
    ).fetchdf()
    if frame.empty:
        return {}
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    result: dict[date, dict[str, float | None]] = {}
    for day, group in frame.groupby("date", sort=True):
        result[day] = {
            "foreign_net_value": _sum(group.get("foreign_net_value")),
            "institution_net_value": _sum(group.get("institution_net_value")),
            "retail_net_value": _sum(group.get("retail_net_value")),
        }
    return result


def _gate_checks(baseline: dict[str, Any], enhanced: dict[str, Any]) -> dict[str, bool]:
    precision_delta = _metric_value(enhanced, "precision_at_k", "precision_top20") - _metric_value(
        baseline, "precision_at_k", "precision_top20"
    )
    ic_delta = _metric_value(enhanced, "rank_ic", "ic") - _metric_value(baseline, "rank_ic", "ic")
    baseline_rmse = _safe_float(baseline.get("rmse"))
    enhanced_rmse = _safe_float(enhanced.get("rmse"))
    return {
        "precision_at_20_plus_1_5pp": precision_delta >= 0.015,
        "rank_ic_plus_0_01": ic_delta >= 0.01,
        "rmse_not_worse_than_5pct": (
            baseline_rmse is not None
            and enhanced_rmse is not None
            and enhanced_rmse <= baseline_rmse * 1.05
        ),
    }


def _data_quality(features: dict[str, Any]) -> dict[str, Any]:
    components = {
        "koru": "ready" if features.get("koru_return_1d") is not None else "missing",
        "ewy": "ready" if features.get("ewy_return_1d") is not None else "missing",
        "kospi": "ready" if features.get("kospi_return_1d") is not None else "missing",
        "kosdaq": "ready" if features.get("kosdaq_return_1d") is not None else "missing",
        "usdkrw": "ready" if features.get("usdkrw_change_pct") is not None else "missing",
        "flows": "ready" if features.get("foreign_net_buy_krw") is not None else "missing",
    }
    messages = [f"{name} 데이터 부족" for name, status in components.items() if status != "ready"]
    signal_sources = {
        "koru": features.get("koru_signal_source"),
        "ewy": features.get("ewy_signal_source"),
        "koru_timestamp": _sanitize(features.get("us_signal_timestamp")),
    }
    if components["koru"] == "ready" and signal_sources["koru"] != "intraday_snapshot_current_price":
        messages.append("KORU 현재가 snapshot 없음: 전일 미국장 종가 기반 fallback 사용")
    ready_count = sum(status == "ready" for status in components.values())
    return {
        "status": "ready" if ready_count == len(components) else ("partial_ready" if ready_count else "not_collected"),
        "components": components,
        "messages": messages,
        "signal_sources": signal_sources,
        "timestamp_basis": "한국 거래일 기준 KST 날짜가 같거나 이전인 최신 미국 ETF 현재가 snapshot을 우선 사용하고, 없으면 D-1 미국장 종가를 사용합니다.",
    }


def _empty_linkage_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "trade_date",
            "us_signal_date",
            "koru_return_1d",
            "koru_volume_ratio_20d",
            "ewy_return_1d",
            "spy_return_1d",
            "qqq_return_1d",
            "koru_ewy_spread_1d",
            "koru_leverage_drift_1d",
            "kospi_return_1d",
            "kosdaq_return_1d",
            "samsung_return_1d",
            "hynix_return_1d",
            "usdkrw_change_pct",
            "foreign_net_buy_krw",
            "institution_net_buy_krw",
            "retail_net_buy_krw",
            "koru_impact_score",
            "koru_market_shock_flag",
            "market_index_trigger_json",
            "causes_json",
            "data_quality_json",
            "created_at",
        ]
    )


def _empty_payload() -> dict[str, Any]:
    return {
        "status": "not_collected",
        "asof_date": None,
        "item": {},
        "weight_decisions": {},
        "leverage_warning": KORU_LEVERAGE_WARNING,
    }


def _cause(kind: str, title: str, impact: float) -> dict[str, Any]:
    return {"type": kind, "title": title, "impact": impact}


def _spread(koru_return, ewy_return, *, multiplier: float) -> float | None:
    koru = _safe_float(koru_return)
    ewy = _safe_float(ewy_return)
    if koru is None or ewy is None:
        return None
    return koru - multiplier * ewy


def _positive_return_score(value, *, scale: float) -> float:
    number = _safe_float(value)
    if number is None:
        return 0.5
    return float(max(0.0, min(1.0, 0.5 + number / (2.0 * scale))))


def _flow_score(value) -> float:
    number = _safe_float(value)
    if number is None:
        return 0.5
    return float(max(0.0, min(1.0, 0.5 + number / 400_000_000_000)))


def _fx_score(value) -> float:
    number = _safe_float(value)
    if number is None:
        return 0.5
    return float(max(0.0, min(1.0, 0.5 - number / 0.02)))


def _average(values: list[float | None], *, default: float) -> float:
    valid = [value for value in values if value is not None]
    if not valid:
        return default
    return float(sum(valid) / len(valid))


def _metric_value(metrics: dict[str, Any], key: str, fallback_key: str) -> float:
    return float(_safe_float(metrics.get(key), default=_safe_float(metrics.get(fallback_key), default=0.0)) or 0.0)


def _sum(series) -> float | None:
    if series is None:
        return None
    values = pd.to_numeric(series, errors="coerce")
    value = values.sum(min_count=1)
    return _safe_float(value)


def _safe_float(value, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _to_date(value) -> date | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return pd.to_datetime(value).date()


def _timestamp_date(value) -> date | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return pd.to_datetime(value).date()


def _as_utc_timestamp(series) -> pd.Series:
    values = pd.to_datetime(series, errors="coerce")
    if values.dt.tz is None:
        return values.dt.tz_localize(UTC)
    return values.dt.tz_convert(UTC)


def _first_notna(*values):
    for value in values:
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        if value is not None:
            return value
    return None


def _json(value: Any) -> str:
    return json.dumps(_sanitize(value), ensure_ascii=False, allow_nan=False)


def _loads(value, default):
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return _sanitize(json.loads(value))
    except (TypeError, json.JSONDecodeError):
        return default


def _sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: _sanitize(value) for key, value in record.items()}


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_sanitize(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        return _sanitize(value.item())
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
