from __future__ import annotations

import json

import pandas as pd

from roboquant.recommend.filters import build_exclusion_flags, build_risk_flags

DEFAULT_WEIGHTS = {
    "pred_prob_top20": 0.35,
    "pred_return_score": 0.20,
    "value_score": 0.10,
    "quality_score": 0.05,
    "momentum_score": 0.10,
    "supply_demand_score": 0.10,
    "consensus_revision_score": 0.05,
    "target_upside_score": 0.03,
    "analyst_reliability_score": 0.04,
    "new_coverage_score": 0.03,
    "sentiment_score": 0.05,
    "liquidity_score": 0.10,
    "koru_impact_score": 0.00,
    "risk_penalty": -0.20,
}


def score_predictions(
    predictions: pd.DataFrame,
    feature_frame: pd.DataFrame | None = None,
    weights: dict[str, float] | None = None,
    missing_factor_default: float = 0.5,
    dnn_scores: pd.DataFrame | None = None,
    dnn_weight: float = 0.0,
) -> pd.DataFrame:
    if predictions.empty:
        return pd.DataFrame()

    frame = predictions.copy()
    frame["asof_date"] = pd.to_datetime(frame["asof_date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)

    if feature_frame is not None and not feature_frame.empty:
        features = feature_frame.copy()
        features["date"] = pd.to_datetime(features["date"]).dt.date
        features["symbol"] = features["symbol"].astype(str).str.zfill(6)
        excluded = {
            "date",
            "symbol",
            "horizon",
            "horizon_days",
            "future_return",
            "benchmark_return",
            "excess_return",
            "rank_quantile",
            "is_top20pct",
            "max_drawdown_forward",
            "pred_return",
            "pred_prob_top20",
            "pred_risk",
            "confidence",
            "model_version",
        }
        feature_columns = [
            column
            for column in features.columns
            if column not in excluded
        ]
        frame = frame.merge(
            features.rename(columns={"date": "asof_date"})[
                ["asof_date", "symbol", "horizon", *feature_columns]
            ],
            on=["asof_date", "symbol", "horizon"],
            how="left",
        )
    if dnn_scores is not None and not dnn_scores.empty and dnn_weight > 0:
        dnn = dnn_scores.copy()
        date_column = "date" if "date" in dnn.columns else "asof_date"
        dnn["asof_date"] = pd.to_datetime(dnn[date_column]).dt.date
        dnn["symbol"] = dnn["symbol"].astype(str).str.zfill(6)
        dnn["dnn_score"] = pd.to_numeric(
            dnn.get("pred_prob", dnn.get("pred_prob_top20", dnn.get("pred_score"))),
            errors="coerce",
        )
        frame = frame.merge(
            dnn[["asof_date", "symbol", "horizon", "dnn_score"]],
            on=["asof_date", "symbol", "horizon"],
            how="left",
        )

    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    frame["pred_return_score"] = frame.groupby(["asof_date", "horizon"])["pred_return"].transform(
        lambda series: series.rank(pct=True)
    )
    if "new_coverage_score" not in frame.columns and "new_coverage_count_30d" in frame.columns:
        frame["new_coverage_score"] = (
            pd.to_numeric(frame["new_coverage_count_30d"], errors="coerce").fillna(0.0) / 3.0
        ).clip(0.0, 1.0)
    factor_columns = [
        "pred_prob_top20",
        "pred_return_score",
        "value_score",
        "quality_score",
        "momentum_score",
        "supply_demand_score",
        "consensus_revision_score",
        "target_upside_score",
        "analyst_reliability_score",
        "new_coverage_score",
        "sentiment_score",
        "liquidity_score",
        "koru_impact_score",
    ]
    for column in factor_columns:
        if column not in frame.columns:
            frame[column] = missing_factor_default
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(missing_factor_default)
    frame["risk_score"] = pd.to_numeric(
        frame.get("risk_score", frame.get("pred_risk", missing_factor_default)), errors="coerce"
    ).fillna(missing_factor_default)
    frame["koru_overlay_weight"] = float(weights.get("koru_impact_score", 0.0) or 0.0)

    frame["final_score"] = (
        weights.get("pred_prob_top20", 0.0) * frame["pred_prob_top20"].clip(0, 1)
        + weights.get("pred_return_score", 0.0) * frame["pred_return_score"].clip(0, 1)
        + weights.get("value_score", 0.0) * frame["value_score"].clip(0, 1)
        + weights.get("quality_score", 0.0) * frame["quality_score"].clip(0, 1)
        + weights.get("momentum_score", 0.0) * frame["momentum_score"].clip(0, 1)
        + weights.get("supply_demand_score", 0.0) * frame["supply_demand_score"].clip(0, 1)
        + weights.get("consensus_revision_score", 0.0) * frame["consensus_revision_score"].clip(0, 1)
        + weights.get("target_upside_score", 0.0) * frame["target_upside_score"].clip(0, 1)
        + weights.get("analyst_reliability_score", 0.0) * frame["analyst_reliability_score"].clip(0, 1)
        + weights.get("new_coverage_score", 0.0) * frame["new_coverage_score"].clip(0, 1)
        + weights.get("sentiment_score", 0.0) * frame["sentiment_score"].clip(0, 1)
        + weights.get("liquidity_score", 0.0) * frame["liquidity_score"].clip(0, 1)
        + weights.get("koru_impact_score", 0.0) * frame["koru_impact_score"].clip(0, 1)
        + weights.get("risk_penalty", 0.0) * frame["risk_score"].clip(0, 1)
    )
    if dnn_weight > 0 and "dnn_score" in frame.columns:
        frame["final_score"] = (
            (1.0 - float(dnn_weight)) * frame["final_score"]
            + float(dnn_weight) * pd.to_numeric(frame["dnn_score"], errors="coerce").fillna(missing_factor_default).clip(0, 1)
        )
    return frame


def build_recommendations(
    scored: pd.DataFrame,
    horizon: str,
    top_k: int,
    min_trading_value_20d: float = 1_000_000_000,
    symbols: pd.DataFrame | None = None,
    exclusion_thresholds: dict[str, float] | None = None,
) -> pd.DataFrame:
    if scored.empty:
        return pd.DataFrame(
            columns=[
                "asof_date",
                "horizon",
                "symbol",
                "final_score",
                "rank",
                "reason_json",
                "risk_flags_json",
                "model_version",
            ]
        )

    frame = scored[scored["horizon"] == horizon].copy()
    frame["asof_date"] = pd.to_datetime(frame["asof_date"]).dt.date
    latest_date = frame["asof_date"].max()
    frame = frame[frame["asof_date"] == latest_date].copy()
    thresholds = {"min_trading_value_20d": min_trading_value_20d, **(exclusion_thresholds or {})}
    frame["exclusion_flags"] = frame.apply(
        lambda row: build_exclusion_flags(row, thresholds=thresholds), axis=1
    )
    frame = frame[frame["exclusion_flags"].map(len) == 0]
    frame = frame.dropna(subset=["final_score"])
    frame = frame.sort_values("final_score", ascending=False).head(int(top_k)).copy()
    frame["rank"] = range(1, len(frame) + 1)
    frame["reason_json"] = frame.apply(lambda row: json.dumps(_reasons(row), ensure_ascii=False), axis=1)
    frame["risk_flags_json"] = frame.apply(
        lambda row: json.dumps(build_risk_flags(row), ensure_ascii=False), axis=1
    )

    if symbols is not None and not symbols.empty:
        symbol_names = symbols[["symbol", "name", "market"]].copy()
        symbol_names["symbol"] = symbol_names["symbol"].astype(str).str.zfill(6)
        frame = frame.merge(symbol_names, on="symbol", how="left")

    columns = [
        "asof_date",
        "horizon",
        "symbol",
        "final_score",
        "rank",
        "reason_json",
        "risk_flags_json",
        "model_version",
    ]
    for optional in ("name", "market"):
        if optional in frame.columns:
            columns.append(optional)
    detail_columns = [
        "pred_return",
        "pred_prob_top20",
        "pred_return_score",
        "pred_risk",
        "confidence",
        "momentum_score",
        "value_score",
        "quality_score",
        "supply_demand_score",
        "consensus_upside_pct",
        "consensus_momentum_30_90",
        "target_up_count_30d",
        "target_down_count_30d",
        "new_coverage_count_30d",
        "target_revision_balance_30d",
        "consensus_revision_score",
        "target_upside_score",
        "analyst_reliability_score",
        "weighted_analyst_reliability_score",
        "dnn_score",
        "sentiment_score",
        "liquidity_score",
        "koru_impact_score",
        "koru_overlay_weight",
        "risk_score",
        "foreign_net_value_20d_sum",
        "institution_net_value_20d_sum",
        "retail_net_value_20d_sum",
        "retail_overheat_score",
        "rsi_14",
        "trading_value_ma20",
    ]
    for optional in detail_columns:
        if optional in frame.columns:
            columns.append(optional)
    return frame[columns].sort_values("rank").reset_index(drop=True)


def _reasons(row: pd.Series) -> list[str]:
    reasons: list[str] = []
    if row.get("pred_prob_top20", 0) >= 0.6:
        reasons.append("Top20 진입 확률이 상대적으로 높음")
    if row.get("pred_return_score", 0) >= 0.7:
        reasons.append("예측 초과수익률 순위가 상위권")
    if row.get("value_score", 0.5) >= 0.7:
        reasons.append("밸류에이션 점수가 상위권")
    if row.get("quality_score", 0.5) >= 0.7:
        reasons.append("재무/품질 점수가 양호")
    if row.get("momentum_score", 0) >= 0.7:
        reasons.append("가격 모멘텀 점수가 우수")
    if row.get("supply_demand_score", 0.5) >= 0.7:
        reasons.append("외국인/기관 수급 점수가 양호")
    if row.get("consensus_revision_score", 0.5) >= 0.7:
        reasons.append("컨센서스 목표가 흐름이 개선")
    if row.get("target_upside_score", 0.5) >= 0.7:
        reasons.append("컨센서스 목표가 괴리율이 긍정적")
    if row.get("analyst_reliability_score", 0.5) >= 0.7:
        reasons.append("최근 리포트 작성 애널리스트 신뢰도 점수가 양호")
    if row.get("new_coverage_count_30d", 0) > 0:
        reasons.append("최근 신규 커버리지 리포트 발생")
    if row.get("liquidity_score", 0) >= 0.7:
        reasons.append("거래대금 기반 유동성이 양호")
    if row.get("sentiment_score", 0.5) >= 0.7:
        reasons.append("뉴스/이벤트 감성 점수가 긍정적")
    if row.get("koru_overlay_weight", 0) > 0 and row.get("koru_impact_score", 0.5) >= 0.7:
        reasons.append("KORU 레버리지 심리 점수가 우호적")
    if row.get("foreign_net_value_20d_sum", 0) > 0:
        reasons.append("외국인 20일 누적 순매수 양수")
    if row.get("institution_net_value_20d_sum", 0) > 0:
        reasons.append("기관 20일 누적 순매수 양수")
    if len(reasons) < 3:
        reasons.append("종합 점수 기준 상위 추천군")
    while len(reasons) < 3:
        reasons.append("추가 정량 근거는 후속 데이터 수집 후 보강 필요")
    return reasons[:5]
