from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from roboquant.features.analyst_features import compute_target_price_features

ANALYST_FEATURE_COLUMNS = [
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
]


def compute_consensus_history(
    reports: pd.DataFrame,
    analyst_scores: pd.DataFrame | None = None,
) -> pd.DataFrame:
    report_frame = compute_target_price_features(reports)
    if report_frame.empty:
        return _empty_consensus()

    report_frame = report_frame.dropna(subset=["report_date", "symbol"]).copy()
    report_frame["date"] = pd.to_datetime(report_frame["report_date"], errors="coerce")
    report_frame["symbol"] = report_frame["symbol"].astype(str).str.zfill(6)
    report_frame["target_price"] = pd.to_numeric(report_frame["target_price"], errors="coerce")
    report_frame["current_price_at_report"] = pd.to_numeric(
        report_frame["current_price_at_report"], errors="coerce"
    )
    score_map = _latest_score_map(analyst_scores)
    rows: list[dict] = []

    for symbol, group in report_frame.sort_values("date").groupby("symbol"):
        dates = sorted(group["date"].dropna().unique())
        for current_date in dates:
            asof = pd.Timestamp(current_date)
            window_30 = group[(group["date"] > asof - pd.Timedelta(days=30)) & (group["date"] <= asof)]
            window_90 = group[(group["date"] > asof - pd.Timedelta(days=90)) & (group["date"] <= asof)]
            if window_90.empty:
                continue
            target_30 = pd.to_numeric(window_30["target_price"], errors="coerce").dropna()
            target_90 = pd.to_numeric(window_90["target_price"], errors="coerce").dropna()
            if target_90.empty:
                continue

            avg_30 = float(target_30.mean()) if not target_30.empty else np.nan
            avg_90 = float(target_90.mean())
            current_price = _latest_current_price(window_30, window_90)
            up_count = int(pd.to_numeric(window_30["target_upgrade_flag"], errors="coerce").fillna(0).sum())
            down_count = int(pd.to_numeric(window_30["target_downgrade_flag"], errors="coerce").fillna(0).sum())
            report_count_30 = int(len(window_30))
            revision_balance = (up_count - down_count) / max(report_count_30, 1)
            consensus_momentum = avg_30 / avg_90 - 1.0 if np.isfinite(avg_30) and avg_90 > 0 else np.nan
            upside_pct = avg_90 / current_price * 100.0 - 100.0 if current_price > 0 else np.nan
            reliability, weighted_reliability = _window_reliability(window_90, score_map)

            rows.append(
                {
                    "date": asof.date(),
                    "symbol": symbol,
                    "stock_name": _first_non_null(window_90.get("stock_name")),
                    "consensus_target_avg": avg_90,
                    "consensus_target_median": float(target_90.median()),
                    "consensus_target_high": float(target_90.max()),
                    "consensus_target_low": float(target_90.min()),
                    "consensus_target_std": float(target_90.std()) if len(target_90) > 1 else 0.0,
                    "report_count_30d": report_count_30,
                    "report_count_90d": int(len(window_90)),
                    "target_up_count_30d": up_count,
                    "target_down_count_30d": down_count,
                    "new_coverage_count_30d": int(window_30["new_coverage_flag"].fillna(False).sum()),
                    "rating_buy_ratio": _rating_buy_ratio(window_90),
                    "consensus_upside_pct": upside_pct,
                    "consensus_momentum_30_90": consensus_momentum,
                    "target_revision_balance_30d": revision_balance,
                    "consensus_revision_score": _consensus_revision_score(revision_balance, consensus_momentum),
                    "target_upside_score": _target_upside_score(upside_pct),
                    "analyst_reliability_score": reliability,
                    "weighted_analyst_reliability_score": weighted_reliability,
                    "updated_at": _utcnow(),
                }
            )

    if not rows:
        return _empty_consensus()
    return pd.DataFrame(rows)[_empty_consensus().columns]


def attach_consensus_features(
    features: pd.DataFrame,
    consensus_history: pd.DataFrame | None,
    missing_factor_default: float = 0.5,
) -> pd.DataFrame:
    if features.empty:
        return features

    frame = features.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    if consensus_history is None or consensus_history.empty:
        output = _fill_analyst_defaults(frame, missing_factor_default)
        output["date"] = pd.to_datetime(output["date"]).dt.date
        return output

    consensus = consensus_history.copy()
    consensus["date"] = pd.to_datetime(consensus["date"], errors="coerce")
    consensus["symbol"] = consensus["symbol"].astype(str).str.zfill(6)
    keep_columns = ["date", "symbol", *[column for column in ANALYST_FEATURE_COLUMNS if column in consensus.columns]]
    consensus = consensus[keep_columns].dropna(subset=["date", "symbol"]).sort_values(["symbol", "date"])
    if consensus.empty:
        output = _fill_analyst_defaults(frame, missing_factor_default)
        output["date"] = pd.to_datetime(output["date"]).dt.date
        return output

    output_frames: list[pd.DataFrame] = []
    for symbol, feature_group in frame.sort_values(["symbol", "date"]).groupby("symbol", sort=False):
        consensus_group = consensus[consensus["symbol"] == symbol].sort_values("date")
        if consensus_group.empty:
            output_frames.append(feature_group)
            continue
        joined = pd.merge_asof(
            feature_group.sort_values("date"),
            consensus_group.drop(columns=["symbol"]).sort_values("date"),
            on="date",
            direction="backward",
        )
        joined["symbol"] = symbol
        output_frames.append(joined)

    output = pd.concat(output_frames, ignore_index=True)
    output = _fill_analyst_defaults(output, missing_factor_default)
    output["date"] = pd.to_datetime(output["date"]).dt.date
    return output


def _fill_analyst_defaults(frame: pd.DataFrame, missing_factor_default: float) -> pd.DataFrame:
    output = frame.copy()
    for column in ANALYST_FEATURE_COLUMNS:
        if column not in output.columns:
            output[column] = missing_factor_default
        output[column] = pd.to_numeric(output[column], errors="coerce").fillna(missing_factor_default)
    return output


def _latest_score_map(analyst_scores: pd.DataFrame | None) -> dict[tuple[str, str], float]:
    if analyst_scores is None or analyst_scores.empty:
        return {}
    scores = analyst_scores.copy()
    scores["as_of_date"] = pd.to_datetime(scores["as_of_date"], errors="coerce")
    scores = scores.sort_values("as_of_date").dropna(subset=["analyst_name", "broker_name"])
    latest = scores.drop_duplicates(["analyst_name", "broker_name"], keep="last")
    return {
        (str(row["analyst_name"]), str(row["broker_name"])): float(row["reliability_score"])
        for _, row in latest.iterrows()
        if pd.notna(row.get("reliability_score"))
    }


def _window_reliability(window: pd.DataFrame, score_map: dict[tuple[str, str], float]) -> tuple[float, float]:
    scores = []
    weights = []
    if not score_map:
        return 0.5, 0.5
    max_date = pd.to_datetime(window["date"]).max()
    for _, row in window.iterrows():
        key = (str(row.get("analyst_name")), str(row.get("broker_name")))
        score = score_map.get(key)
        if score is None:
            continue
        age_days = max((max_date - pd.Timestamp(row["date"])).days, 0)
        weight = 1.0 / (1.0 + age_days / 30.0)
        scores.append(score)
        weights.append(weight)
    if not scores:
        return 0.5, 0.5
    values = np.asarray(scores, dtype=float)
    weight_values = np.asarray(weights, dtype=float)
    return float(np.mean(values)), float(np.average(values, weights=weight_values))


def _latest_current_price(window_30: pd.DataFrame, window_90: pd.DataFrame) -> float:
    for window in (window_30, window_90):
        values = pd.to_numeric(window.sort_values("date")["current_price_at_report"], errors="coerce").dropna()
        if not values.empty:
            return float(values.iloc[-1])
    return np.nan


def _rating_buy_ratio(window: pd.DataFrame) -> float:
    rating = window.get("investment_rating", pd.Series("", index=window.index)).astype("string").fillna("")
    if rating.empty:
        return np.nan
    return float(rating.str.contains("매수|buy|outperform", case=False, regex=True, na=False).mean())


def _first_non_null(series: pd.Series | None):
    if series is None:
        return None
    non_null = series.dropna()
    return None if non_null.empty else non_null.iloc[-1]


def _consensus_revision_score(balance: float, momentum: float) -> float:
    balance_component = (float(balance) + 1.0) / 2.0 if pd.notna(balance) else 0.5
    momentum_component = 0.5 if pd.isna(momentum) else (float(momentum) + 0.2) / 0.4
    return float(np.clip(0.70 * balance_component + 0.30 * momentum_component, 0.0, 1.0))


def _target_upside_score(upside_pct: float) -> float:
    if pd.isna(upside_pct):
        return 0.5
    return float(np.clip((float(upside_pct) + 20.0) / 80.0, 0.0, 1.0))


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _empty_consensus() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "date",
            "symbol",
            "stock_name",
            "consensus_target_avg",
            "consensus_target_median",
            "consensus_target_high",
            "consensus_target_low",
            "consensus_target_std",
            "report_count_30d",
            "report_count_90d",
            "target_up_count_30d",
            "target_down_count_30d",
            "new_coverage_count_30d",
            "rating_buy_ratio",
            "consensus_upside_pct",
            "consensus_momentum_30_90",
            "target_revision_balance_30d",
            "consensus_revision_score",
            "target_upside_score",
            "analyst_reliability_score",
            "weighted_analyst_reliability_score",
            "updated_at",
        ]
    )
