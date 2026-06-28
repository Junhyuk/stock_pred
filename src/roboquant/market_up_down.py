from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from roboquant.long_short import (
    _ensure_scores,
    _filter_liquidity,
    _market_leg_counts,
    _merge_features,
    _merge_symbols,
    _prepare_prediction_frame,
    _reasons,
    _risk_flags,
)
from roboquant.reports.prompt_templates import validate_report_text

MARKET_UP_DOWN_DISCLAIMER = (
    "본 추천은 Top50 유니버스 내 시장별 상승·하락 랭킹 참고용이며 투자 권유가 아닙니다. "
    "하락 추천은 모델 기반 하방 확률 신호이며 실제 공매도 가능성을 보장하지 않습니다."
)


def build_market_up_down_recommendations(
    predictions: pd.DataFrame,
    features: pd.DataFrame | None = None,
    symbols: pd.DataFrame | None = None,
    universe: pd.DataFrame | None = None,
    horizon: str = "2M",
    config: dict[str, Any] | None = None,
    asof_date: str | None = None,
) -> pd.DataFrame:
    cfg = _market_up_down_config(config)
    frame = _prepare_prediction_frame(predictions, horizon)
    if frame.empty:
        return _empty_recommendations()

    if asof_date and asof_date != "latest":
        selected_date = pd.to_datetime(asof_date).date()
    else:
        selected_date = pd.to_datetime(frame["asof_date"]).max().date()
    frame = frame[pd.to_datetime(frame["asof_date"]).dt.date == selected_date].copy()
    if frame.empty:
        return _empty_recommendations()

    frame = _merge_features(frame, features, horizon)
    frame = _merge_symbols(frame, symbols, universe=universe)
    frame = _ensure_scores(frame)
    eligible = _filter_liquidity(frame, cfg)
    if eligible.empty:
        return _empty_recommendations()

    created_at = datetime.now(UTC).replace(tzinfo=None)
    rows = _build_market_split_rows(eligible, cfg, created_at=created_at)
    if not rows:
        return _empty_recommendations()
    return pd.DataFrame(rows).sort_values(["market", "side", "rank"]).reset_index(drop=True)


def render_market_up_down_report(recommendations: pd.DataFrame, horizon: str) -> str:
    if recommendations.empty:
        return validate_report_text(
            f"# Market Up-Down {horizon}\n\n생성된 상승·하락 추천이 없습니다.\n\n> {MARKET_UP_DOWN_DISCLAIMER}\n"
        )

    asof_date = recommendations["asof_date"].iloc[0]
    lines = [
        f"# Market Up-Down {horizon}",
        "",
        f"- As-of: {asof_date}",
        "",
    ]
    for market in ("KOSPI", "KOSDAQ"):
        market_frame = recommendations[recommendations["market"] == market]
        if market_frame.empty:
            continue
        lines.append(f"## {market}")
        for side, label, score_col in (
            ("UP", "Upside", "long_score"),
            ("DOWN", "Downside", "short_score"),
        ):
            leg = market_frame[market_frame["side"] == side].sort_values("rank")
            if leg.empty:
                continue
            lines.extend(
                [
                    "",
                    f"### {label}",
                    "",
                    "| Rank | Symbol | Name | Score | Pred Return | Bottom Prob | Risk Flags |",
                    "|---:|---|---|---:|---:|---:|---|",
                ]
            )
            for _, row in leg.iterrows():
                flags = ", ".join(json.loads(row.get("risk_flags_json") or "[]")) or "-"
                name = row.get("name") if pd.notna(row.get("name")) else ""
                lines.append(
                    f"| {int(row['rank'])} | `{row['symbol']}` | {name} | "
                    f"{float(row[score_col]):.4f} | {float(row['pred_return']):.4f} | "
                    f"{float(row.get('pred_prob_bottom20', 0.5)):.2f} | {flags} |"
                )
    lines += ["", f"> {MARKET_UP_DOWN_DISCLAIMER}", ""]
    return validate_report_text("\n".join(lines))


def _build_market_split_rows(
    eligible: pd.DataFrame,
    cfg: dict[str, Any],
    *,
    created_at: datetime,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for market, counts in _market_leg_counts(cfg).items():
        market_frame = eligible[eligible["market"].astype(str).eq(market)].copy()
        if market_frame.empty:
            continue
        upside = (
            market_frame.sort_values(["long_score", "confidence", "pred_return"], ascending=[False, False, False])
            .head(counts["long"])
            .copy()
        )
        downside_candidates = market_frame[~market_frame["symbol"].isin(upside["symbol"])].copy()
        downside = (
            downside_candidates.sort_values(
                ["short_score", "confidence", "pred_prob_bottom20"],
                ascending=[False, False, False],
            )
            .head(counts["short"])
            .copy()
        )
        rows.extend(_rank_rows(upside, side="UP", market=market, created_at=created_at))
        rows.extend(_rank_rows(downside, side="DOWN", market=market, created_at=created_at))
    return rows


def _rank_rows(
    leg: pd.DataFrame,
    *,
    side: str,
    market: str,
    created_at: datetime,
) -> list[dict[str, object]]:
    if leg.empty:
        return []
    score_column = "long_score" if side == "UP" else "short_score"
    risk_side = "LONG" if side == "UP" else "SHORT"
    rows: list[dict[str, object]] = []
    for rank, (_, row) in enumerate(leg.iterrows(), start=1):
        rows.append(
            {
                "asof_date": row["asof_date"],
                "horizon": row["horizon"],
                "market": market,
                "symbol": row["symbol"],
                "side": side,
                "rank": rank,
                "long_score": float(row.get("long_score", 0.0)),
                "short_score": float(row.get("short_score", 0.0)),
                "pred_return": float(row.get("pred_return", 0.0)),
                "pred_prob_top20": float(row.get("pred_prob_top20", 0.5)),
                "pred_prob_bottom20": float(row.get("pred_prob_bottom20", 0.5)),
                "risk_score": float(row.get("risk_score", row.get("pred_risk", 0.5))),
                "confidence": float(row.get("confidence", 0.5)),
                "reason_json": json.dumps(_reasons(row, risk_side, score_column), ensure_ascii=False),
                "risk_flags_json": json.dumps(_risk_flags(row, risk_side), ensure_ascii=False),
                "model_version": row.get("model_version"),
                "created_at": created_at,
                "name": row.get("name"),
                "sector": row.get("sector"),
            }
        )
    return rows


def _market_up_down_config(config: dict[str, Any] | None) -> dict[str, Any]:
    base = dict((config or {}).get("market_up_down", {}))
    split = dict(base.get("market_split", {}))
    split.setdefault("enabled", True)
    upside = int(base.get("upside_count", 10))
    downside = int(base.get("downside_count", 10))
    return {
        "long_count": upside,
        "short_count": downside,
        "market_split": split,
        "min_trading_value_20d": float(
            base.get("min_trading_value_20d", (config or {}).get("market", {}).get("min_trading_value_20d", 0) or 0)
        ),
    }


def _empty_recommendations() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "asof_date",
            "horizon",
            "market",
            "symbol",
            "side",
            "rank",
            "long_score",
            "short_score",
            "pred_return",
            "pred_prob_top20",
            "pred_prob_bottom20",
            "risk_score",
            "confidence",
            "reason_json",
            "risk_flags_json",
            "model_version",
            "created_at",
        ]
    )
