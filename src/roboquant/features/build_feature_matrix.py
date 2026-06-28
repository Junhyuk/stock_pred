from __future__ import annotations

import pandas as pd

from roboquant.features.consensus_features import ANALYST_FEATURE_COLUMNS, attach_consensus_features
from roboquant.features.flow_features import compute_flow_features
from roboquant.features.market_features import compute_market_features
from roboquant.features.price_features import compute_price_features
from roboquant.koru import attach_koru_features
from roboquant.signals.news_signals import attach_news_signal_features
from roboquant.signals.telegram_signals import attach_telegram_market_features
from roboquant.us_sector_linkage import attach_us_sector_features

FACTOR_COLUMNS = [
    "market_cap_score",
    "value_score",
    "quality_score",
    "supply_demand_score",
    "sentiment_score",
    "consensus_revision_score",
    "target_upside_score",
    "analyst_reliability_score",
    "weighted_analyst_reliability_score",
]


def build_feature_matrix(
    prices: pd.DataFrame,
    horizons: dict[str, int],
    investor_flows: pd.DataFrame | None = None,
    market_metrics: pd.DataFrame | None = None,
    consensus_history: pd.DataFrame | None = None,
    koru_linkage: pd.DataFrame | None = None,
    telegram_market: pd.DataFrame | None = None,
    news_signals: pd.DataFrame | None = None,
    us_sector_linkage: pd.DataFrame | None = None,
    symbols: pd.DataFrame | None = None,
    config: dict | None = None,
    missing_factor_default: float = 0.5,
) -> pd.DataFrame:
    price_features = compute_price_features(prices, horizons)
    market_features = compute_market_features(market_metrics if market_metrics is not None else pd.DataFrame())
    flow_features = compute_flow_features(
        investor_flows if investor_flows is not None else pd.DataFrame(),
        prices=prices,
        market_metrics=market_metrics,
    )

    features = price_features.copy()
    if not market_features.empty:
        features = features.merge(market_features, on=["date", "symbol"], how="left")
    if not flow_features.empty:
        features = features.merge(flow_features, on=["date", "symbol"], how="left")
    features = attach_consensus_features(
        features,
        consensus_history=consensus_history,
        missing_factor_default=missing_factor_default,
    )
    features = attach_koru_features(
        features,
        koru_linkage,
        missing_factor_default=missing_factor_default,
    )
    features = attach_telegram_market_features(features, telegram_market)
    features = attach_news_signal_features(features, news_signals)
    features = attach_us_sector_features(
        features,
        us_sector_linkage,
        symbols=symbols,
        config=config,
        missing_factor_default=missing_factor_default,
    )

    for column in [*FACTOR_COLUMNS, *ANALYST_FEATURE_COLUMNS]:
        if column not in features.columns:
            features[column] = missing_factor_default
        features[column] = pd.to_numeric(features[column], errors="coerce").fillna(missing_factor_default)

    features["liquidity_score"] = pd.to_numeric(features["liquidity_score"], errors="coerce").fillna(
        missing_factor_default
    )
    base_risk = pd.to_numeric(features["risk_score"], errors="coerce").fillna(missing_factor_default)
    retail_overheat = (
        features["retail_overheat_score"]
        if "retail_overheat_score" in features.columns
        else pd.Series(0.0, index=features.index)
    )
    retail_risk = pd.to_numeric(retail_overheat, errors="coerce").fillna(0.0)
    low_supply_risk = 1.0 - features["supply_demand_score"].clip(0.0, 1.0)
    features["risk_score"] = pd.concat(
        [base_risk, retail_risk.clip(0.0, 1.0), low_supply_risk],
        axis=1,
    ).mean(axis=1)

    return features.sort_values(["date", "symbol", "horizon"]).reset_index(drop=True)
