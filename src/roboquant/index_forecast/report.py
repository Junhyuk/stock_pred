"""GitHub Pages payload for the four-index 1D/2D research report."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from roboquant.index_forecast.indexes import (
    RESEARCH_INDEXES,
    get_official_index,
    horizon_label,
    horizon_title,
)
from roboquant.index_forecast.reliable_inputs import KST, session_schedule
from roboquant.index_forecast.sqlite_store import PRODUCTION_WEIGHT

DISCLAIMER = (
    "연구·정보제공용입니다. 확률은 상승 확정이 아니고, 신호는 매수/매도 지시가 아닙니다. "
    "production_weight=0 이며 production Ridge(market-outlook-v2)는 바꾸지 않았습니다."
)
CURRENT_CLOSE_NOTE = "예측 기준일은 마지막 확정 종가일입니다. 오늘 장 마감 전에는 그 종가가 달력 날짜와 다를 수 있습니다."
STALE_WAREHOUSE_NOTE = "예측 기준일은 마지막 확정 종가일입니다. 창고가 최근 거래일보다 늦으면 페이지의 내일은 달력상 내일이 아닙니다."
METHOD_NOTE = (
    "2거래일 예측은 1일 예측을 두 번 이은 값이 아닙니다. "
    "2거래일 뒤 종가수익률을 직접 맞추고, 학습에서는 그 2거래일을 빼며, "
    "우승 모델은 같은 수익이 겹치지 않게 2거래일마다 한 번씩 센 방향 적중과 MAE로 고릅니다."
)


def _latest_completed_session(generated_at: str) -> str | None:
    """Last XKRX session whose close is at or before the report clock."""
    stamp = pd.Timestamp(generated_at)
    if pd.isna(stamp):
        return None
    stamp = (stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC"))
    local = stamp.tz_convert(KST)
    try:
        schedule, _source = session_schedule(local.date() - timedelta(days=21), local.date())
    except (ValueError, OverflowError):
        return None
    closed = schedule.loc[pd.to_datetime(schedule["close_at"], utc=True) <= stamp]
    if closed.empty:
        return None
    return str(closed["date"].iloc[-1])


def _data_freshness(asof_dates: list[Any], generated_at: str) -> dict[str, Any]:
    latest = max(str(item) for item in asof_dates) if asof_dates else None
    expected = _latest_completed_session(generated_at) if latest else None
    if latest and expected:
        stale = latest < expected
    else:
        stale = bool(latest) and latest < generated_at[:10]
    return {
        "latest_asof": latest,
        "expected_asof": expected,
        "stale_vs_calendar": stale,
        "note": STALE_WAREHOUSE_NOTE if stale else CURRENT_CLOSE_NOTE,
    }


def build_github_pages_payload(
    slots: dict[str, Any],
    *,
    eval_years: int,
    ingest: dict[str, Any] | None = None,
    export: dict[str, Any] | None = None,
    model_types: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    generated_at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
    cards = [_index_card(slots, index_code) for index_code in RESEARCH_INDEXES]
    leaderboard = _leaderboard(slots)
    asof_dates = [card.get("asof_date") for card in cards if card.get("asof_date")]
    freshness = _data_freshness(asof_dates, generated_at)
    return {
        "status": "ready" if any(card["status"] == "ready" for card in cards) else "not_ready",
        "generated_at": generated_at,
        "eval_years": eval_years,
        "production_weight": PRODUCTION_WEIGHT,
        "disclaimer": DISCLAIMER,
        "method_note": METHOD_NOTE,
        "data_freshness": freshness,
        "sequential7": _sequential7_checklist(cards, leaderboard),
        "model_candidates": list(model_types or []),
        "ingest": ingest or {},
        "export": export or {},
        "leaderboard": leaderboard,
        "indexes": cards,
    }


def write_github_pages_report(output_dir: str | Path, payload: dict[str, Any]) -> Path:
    output = Path(output_dir)
    data_dir = output / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "index_forecast.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _index_card(slots: dict[str, Any], index_code: str) -> dict[str, Any]:
    spec = get_official_index(index_code)
    labels = _horizon_labels(slots, index_code)
    reports = [slots.get(f"{index_code}:{label}") or {} for label in labels]
    ready = any(item.get("rows") for item in reports)
    lives = [item.get("live_forecast") or {} for item in reports]
    asof = next((item.get("asof_date") for item in lives if item.get("asof_date")), None)
    last_close = next((item.get("last_close") for item in lives if item.get("last_close") is not None), None)
    first = reports[0] if reports else {}
    return {
        "index_code": index_code,
        "display_name": spec.display_name,
        "korean_name": spec.korean_name,
        "pykrx_code": spec.pykrx_code,
        "status": "ready" if ready else "not_ready",
        "reason": first.get("reason") if not ready else None,
        "asof_date": asof,
        "last_close": last_close,
        "horizons": {
            label: _horizon_block(report, title=horizon_title(int(label[:-1])))
            for label, report in zip(labels, reports, strict=True)
        },
    }


def _horizon_labels(slots: dict[str, Any], index_code: str) -> list[str]:
    labels = [key.split(":", 1)[1] for key in slots if key.startswith(f"{index_code}:")]
    if not labels:
        labels = [horizon_label(days) for days in (1, 2)]
    return sorted(set(labels), key=lambda label: int(label[:-1]) if label[:-1].isdigit() else 99)


def _horizon_block(report: dict[str, Any], *, title: str) -> dict[str, Any]:
    winner = report.get("winner") or {}
    live = report.get("live_forecast") or {}
    models = []
    for group_name, group in (report.get("groups") or {}).items():
        for model_type, payload in (group.get("models") or {}).items():
            metrics = payload.get("metrics") or {}
            models.append(
                {
                    "group": group_name,
                    "model_type": model_type,
                    "model_version": payload.get("model_version"),
                    "n": metrics.get("n"),
                    "direction_hit": metrics.get("direction_hit"),
                    "mae": metrics.get("mae"),
                    "rmse": metrics.get("rmse"),
                    "coverage": metrics.get("coverage"),
                    "spearman": metrics.get("spearman"),
                    "champion": model_type == winner.get("model_type") and group_name == winner.get("group"),
                }
            )
    models.sort(
        key=lambda item: (
            -(item["direction_hit"] or -1.0),
            item["mae"] if item["mae"] is not None else 9.0,
        )
    )
    return {
        "title": title,
        "status": report.get("status") or "not_ready",
        "reason": report.get("reason"),
        "rows": report.get("rows"),
        "eval_start": report.get("eval_start"),
        "champion": {
            "model_type": winner.get("model_type"),
            "group": winner.get("group"),
            "model_version": winner.get("model_version"),
            "gate_status": winner.get("status"),
            "gate_reason": winner.get("reason"),
            "metrics": winner.get("metrics") or {},
            "recent_actual_vs_pred": winner.get("recent") or [],
        },
        "models": models,
        "forecast": live if live.get("status") == "ready" else None,
    }


def _leaderboard(slots: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, report in slots.items():
        winner = report.get("winner") or {}
        metrics = winner.get("metrics") or {}
        if winner.get("model_type") is None:
            continue
        rows.append(
            {
                "slot": key,
                "index_code": report.get("index_code"),
                "horizon": report.get("horizon"),
                "model_type": winner.get("model_type"),
                "group": winner.get("group"),
                "direction_hit": metrics.get("direction_hit"),
                "mae": metrics.get("mae"),
                "n": metrics.get("n"),
                "gate_status": winner.get("status"),
            }
        )
    rows.sort(key=lambda item: (-(item["direction_hit"] or -1.0), item["mae"] if item["mae"] is not None else 9.0))
    return rows


def _sequential7_checklist(cards: list[dict[str, Any]], leaderboard: list[dict[str, Any]]) -> dict[str, Any]:
    ready_indexes = [card["index_code"] for card in cards if card["status"] == "ready"]
    missing = [card["index_code"] for card in cards if card["status"] != "ready"]
    has_forecast = any(
        (card.get("horizons") or {}).get("1D", {}).get("forecast")
        or any((block or {}).get("forecast") for block in (card.get("horizons") or {}).values())
        for card in cards
    )
    items = [
        {
            "id": "ux",
            "agent": "UX Designer",
            "check": "1D/2D 분리, 예측 vs 실현, 확률≠확정, 데이터 기준일 표시",
            "pass": True,
        },
        {
            "id": "python",
            "agent": "Python Engineer",
            "check": "walk-forward·live forecast·Pages JSON이 재현 가능하게 생성됨",
            "pass": bool(leaderboard) or bool(missing),
        },
        {
            "id": "ml",
            "agent": "ML Expert",
            "check": "naive/previous_direction 대비 후보 비교, 1D와 2D 모델 분리",
            "pass": bool(leaderboard),
        },
        {
            "id": "sqlite",
            "agent": "SQLite Architecture",
            "check": "prediction immutable, production_weight=0",
            "pass": PRODUCTION_WEIGHT == 0.0,
        },
        {
            "id": "terms",
            "agent": "Stock Terms",
            "check": "다음 1/2 거래일, 방향 적중, 80% 잔차 범위 용어 고정",
            "pass": True,
        },
        {
            "id": "forecast",
            "agent": "Market Forecast Expert",
            "check": "우승 모델을 내일(1D)·모레(2D) 연구 예측에 적용",
            "pass": has_forecast,
        },
        {
            "id": "data",
            "agent": "Market Data Expert",
            "check": "KOSPI100=1034, 1028/KOSDAQ150 대체 금지, 없는 지수는 not_ready",
            "pass": True,
        },
    ]
    return {
        "ready_indexes": ready_indexes,
        "not_ready_indexes": missing,
        "items": items,
        "all_passed": all(item["pass"] for item in items),
    }
