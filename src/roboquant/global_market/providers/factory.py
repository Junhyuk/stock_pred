from __future__ import annotations

from collections.abc import Mapping
from os import environ

from roboquant.global_market.providers.base import GlobalMarketProvider
from roboquant.global_market.providers.fred import FredProvider
from roboquant.global_market.providers.yfinance_poc import YFinancePocProvider


def get_global_market_provider(
    name: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> GlobalMarketProvider:
    values = environ if env is None else env
    provider_name = str(name or values.get("GLOBAL_MARKET_PROVIDER") or "fred").strip().lower()
    if provider_name == "fred":
        return FredProvider(values)
    if provider_name == "yfinance_poc":
        return YFinancePocProvider()
    if provider_name in {"kis_global", "broker_global"}:
        raise NotImplementedError("approved global market provider endpoint is not configured yet")
    raise ValueError(f"Unknown global market provider: {provider_name}")
