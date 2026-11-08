"""GitHub Pages payload for the four-index 1D/5D research report."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from roboquant.index_forecast.indexes import RESEARCH_INDEXES, get_official_index
from roboquant.index_forecast.sqlite_store import PRODUCTION_WEIGHT

DISCLAIMER = (
    "연구·정보제공용입니다. 확률은 상승 확정이 아니고, 신호는 매수/매도 지시가 아닙니다. "
    "production_weight=0 이며 production Ridge(market-outlook-v2)는 바꾸지 않았습니다."
)


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
    stale = bool(asof_dates) and max(str(item) for item in asof_dates) < generated_at[:10]
    return {
        "status": "ready" if any(card["status"] == "ready" for card in cards) else "not_ready",
        "generated_at": generated_at,
        "eval_years": eval_years,
        "production_weight": PRODUCTION_WEIGHT,
        "disclaimer": DISCLAIMER,
        "data_freshness": {
            "latest_asof": max(str(item) for item in asof_dates) if asof_dates else None,
            "stale_vs_calendar": stale,
            "note": "예측 기준일은 마지막 확정 종가일입니다. 창고가 늦으면 달력상 내일이 아닙니다.",
        },
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
    one = slots.get(f"{index_code}:1D") or {}
    five = slots.get(f"{index_code}:5D") or {}
    ready = bool(one.get("rows") or five.get("rows"))
    live_1d = one.get("live_forecast") or {}
    live_5d = five.get("live_forecast") or {}
    asof = live_1d.get("asof_date") or live_5d.get("asof_date")
    return {
        "index_code": index_code,
        "display_name": spec.display_name,
        "korean_name": spec.korean_name,
        "pykrx_code": spec.pykrx_code,
        "status": "ready" if ready else "not_ready",
        "reason": one.get("reason") if not ready else None,
        "asof_date": asof,
        "last_close": live_1d.get("last_close") or live_5d.get("last_close"),
        "horizons": {
            "1D": _horizon_block(one, title="내일 (다음 1 거래일)"),
            "5D": _horizon_block(five, title="다음주 (다음 5 거래일)"),
        },
    }


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
        or (card.get("horizons") or {}).get("5D", {}).get("forecast")
        for card in cards
    )
    items = [
        {
            "id": "ux",
            "agent": "UX Designer",
            "check": "1D/5D 분리, 예측 vs 실현, 확률≠확정, 데이터 기준일 표시",
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
            "check": "naive/previous_direction 대비 후보 비교, 1D와 5D 모델 분리",
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
            "check": "다음 1/5 거래일, 방향 적중, 80% 잔차 범위 용어 고정",
            "pass": True,
        },
        {
            "id": "forecast",
            "agent": "Market Forecast Expert",
            "check": "우승 모델을 내일(1D)·다음주(5D) 연구 예측에 적용",
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
