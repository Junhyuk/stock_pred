from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class DataQualityReport:
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            raise ValueError("; ".join(self.errors))


PRICE_REQUIRED_COLUMNS = {
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "trading_value",
}


def validate_prices(prices: pd.DataFrame) -> DataQualityReport:
    errors: list[str] = []
    warnings: list[str] = []

    missing = sorted(PRICE_REQUIRED_COLUMNS.difference(prices.columns))
    if missing:
        errors.append(f"prices_daily is missing columns: {missing}")
        return DataQualityReport(errors=errors, warnings=warnings)

    if prices.empty:
        errors.append("prices_daily is empty")
        return DataQualityReport(errors=errors, warnings=warnings)

    duplicate_count = prices.duplicated(["date", "symbol"]).sum()
    if duplicate_count:
        errors.append(f"prices_daily has {duplicate_count} duplicate date/symbol rows")

    if prices[["date", "symbol", "close"]].isna().any().any():
        errors.append("prices_daily has null date, symbol, or close values")

    if (prices["close"] <= 0).any():
        errors.append("prices_daily has non-positive close prices")

    if (prices["volume"] < 0).any():
        errors.append("prices_daily has negative volume values")

    zero_trading_value = (prices["trading_value"].fillna(0) <= 0).mean()
    if zero_trading_value > 0.1:
        warnings.append("more than 10% of rows have zero or missing trading_value")

    return DataQualityReport(errors=errors, warnings=warnings)

