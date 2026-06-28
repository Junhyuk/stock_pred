from __future__ import annotations

import pandas as pd

PROFILE_LIMITS = {
    "stable": {"max_stock_weight": 0.12, "max_sector_weight": 0.25, "cash_ratio": 0.30},
    "neutral": {"max_stock_weight": 0.15, "max_sector_weight": 0.30, "cash_ratio": 0.15},
    "aggressive": {"max_stock_weight": 0.20, "max_sector_weight": 0.40, "cash_ratio": 0.05},
}


def normalize_portfolio_weights(
    items: list[dict],
    max_stock_weight: float = 0.15,
    max_sector_weight: float = 0.30,
    cash_ratio: float = 0.0,
) -> list[dict]:
    if not items:
        return []
    output = [dict(item) for item in items]
    investable_ratio = max(0.0, min(1.0, 1.0 - float(cash_ratio)))
    total_score = sum(max(_number(item.get("final_score")), 0.0) for item in output)
    if total_score <= 0:
        return []
    for item in output:
        item["raw_weight"] = max(_number(item.get("final_score")), 0.0) / total_score * investable_ratio
        item["weight"] = min(item["raw_weight"], float(max_stock_weight))

    sector_sum: dict[str, float] = {}
    for item in output:
        sector = str(item.get("sector") or "기타")
        sector_sum[sector] = sector_sum.get(sector, 0.0) + item["weight"]
    for sector, weight in sector_sum.items():
        if weight > max_sector_weight:
            ratio = float(max_sector_weight) / weight
            for item in output:
                if str(item.get("sector") or "기타") == sector:
                    item["weight"] *= ratio

    if sum(item["weight"] for item in output) <= 0:
        return []
    for item in output:
        item["weight"] = round(item["weight"], 4)
    return output


def build_portfolio_from_recommendations(
    recommendations: pd.DataFrame,
    profile: str = "neutral",
    limit: int = 20,
) -> dict:
    limits = PROFILE_LIMITS.get(profile, PROFILE_LIMITS["neutral"])
    if recommendations.empty:
        return {"profile": profile, "cash_ratio": limits["cash_ratio"], "items": []}
    frame = recommendations.sort_values("final_score", ascending=False).head(limit).copy()
    frame["sector"] = frame.get("sector", frame.get("market", "기타")).fillna("기타")
    items = [
        {
            "symbol": row.get("symbol"),
            "ticker": row.get("symbol"),
            "name": row.get("name") or row.get("symbol"),
            "sector": row.get("sector") or "기타",
            "final_score": _number(row.get("final_score")),
            "upside": _number(row.get("target_upside_score"), 0.5),
            "risk_score": _number(row.get("risk_score"), 0.5),
        }
        for _, row in frame.iterrows()
    ]
    weighted_items = normalize_portfolio_weights(
        items,
        max_stock_weight=limits["max_stock_weight"],
        max_sector_weight=limits["max_sector_weight"],
        cash_ratio=limits["cash_ratio"],
    )
    effective_cash_ratio = round(max(limits["cash_ratio"], 1.0 - sum(item["weight"] for item in weighted_items)), 4)
    return {
        "profile": profile,
        "cash_ratio": effective_cash_ratio,
        "items": weighted_items,
    }


def _number(value, default: float = 0.0) -> float:
    if value is None or pd.isna(value):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
