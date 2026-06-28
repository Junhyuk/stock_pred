from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd


def decide_model_gate(candidate: dict, baseline: dict, min_sample_count: int = 200) -> dict:
    reasons: list[str] = []
    if _number(candidate.get("sample_count")) < min_sample_count:
        reasons.append("sample_count < 200")
    if _number(candidate.get("precision_top20")) < _number(baseline.get("precision_top20")):
        reasons.append("precision_top20 lower than baseline")
    if _number(candidate.get("avg_excess_return")) < _number(baseline.get("avg_excess_return")):
        reasons.append("avg_excess_return lower than baseline")
    rank_ic = candidate.get("rank_ic")
    if rank_ic is not None and pd.notna(rank_ic) and float(rank_ic) < 0:
        reasons.append("rank_ic < 0")
    candidate_mdd = _number(candidate.get("mdd"))
    baseline_mdd = _number(baseline.get("mdd"))
    if candidate_mdd < baseline_mdd * 1.2:
        reasons.append("mdd worse than baseline")
    if reasons:
        return {"gate_status": "rejected", "production_weight": 0.0, "reasons": reasons}
    return {
        "gate_status": "accepted",
        "production_weight": min(_number(candidate.get("suggested_weight"), 0.1), 0.3),
        "reasons": ["candidate model passed backtest gate"],
    }


def run_model_gatekeeper(conn, baseline_model: str = "lightgbm", min_sample_count: int = 200) -> pd.DataFrame:
    performance = conn.execute(
        "SELECT * FROM model_performance_daily ORDER BY eval_date DESC, created_at DESC"
    ).fetchdf()
    if performance.empty:
        return pd.DataFrame(columns=["model_name", "model_version", "horizon", "gate_status", "production_weight", "reasons"])
    rows = []
    for horizon, group in performance.groupby("horizon"):
        baseline_candidates = group[group["model_name"] == baseline_model]
        if baseline_candidates.empty:
            continue
        baseline = baseline_candidates.iloc[0].to_dict()
        for _, candidate_row in group.iterrows():
            candidate = candidate_row.to_dict()
            if candidate["model_name"] == baseline_model:
                decision = {
                    "gate_status": "production",
                    "production_weight": 1.0,
                    "reasons": ["baseline production model"],
                }
            else:
                decision = decide_model_gate(candidate, baseline, min_sample_count=min_sample_count)
            conn.execute(
                """
                UPDATE model_performance_daily
                SET gate_status = ?,
                    production_weight = ?
                WHERE eval_date = ?
                  AND model_name = ?
                  AND model_version = ?
                  AND horizon = ?
                """,
                [
                    decision["gate_status"],
                    float(decision["production_weight"]),
                    candidate["eval_date"],
                    candidate["model_name"],
                    candidate["model_version"],
                    candidate["horizon"],
                ],
            )
            if candidate["model_name"] != baseline_model:
                conn.execute(
                    """
                    UPDATE model_registry
                    SET status = ?,
                        production_weight = ?,
                        fail_reason = ?,
                        updated_at = ?
                    WHERE model_name = ?
                    """,
                    [
                        decision["gate_status"],
                        float(decision["production_weight"]),
                        "; ".join(decision["reasons"]),
                        _utcnow(),
                        candidate["model_name"],
                    ],
                )
            rows.append(
                {
                    "model_name": candidate["model_name"],
                    "model_version": candidate["model_version"],
                    "horizon": horizon,
                    "gate_status": decision["gate_status"],
                    "production_weight": float(decision["production_weight"]),
                    "reasons": decision["reasons"],
                }
            )
    return pd.DataFrame(rows)


def _number(value, default: float = 0.0) -> float:
    if value is None or pd.isna(value):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
