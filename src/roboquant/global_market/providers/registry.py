from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GlobalSymbol:
    symbol: str
    display_name: str
    market_group: str
    source_name: str


YFINANCE_DAILY_SYMBOLS: dict[str, GlobalSymbol] = {
    "^GSPC": GlobalSymbol("^GSPC", "S&P 500", "US_INDEX", "yfinance_poc"),
    "^IXIC": GlobalSymbol("^IXIC", "Nasdaq Composite", "US_INDEX", "yfinance_poc"),
    "^DJI": GlobalSymbol("^DJI", "Dow Jones Industrial Average", "US_INDEX", "yfinance_poc"),
    "^SOX": GlobalSymbol("^SOX", "Philadelphia Semiconductor Index", "SEMICONDUCTOR", "yfinance_poc"),
    "SOXX": GlobalSymbol("SOXX", "iShares Semiconductor ETF", "SEMICONDUCTOR", "yfinance_poc"),
    "SMH": GlobalSymbol("SMH", "VanEck Semiconductor ETF", "SEMICONDUCTOR", "yfinance_poc"),
    "^VIX": GlobalSymbol("^VIX", "CBOE Volatility Index", "VOLATILITY", "yfinance_poc"),
    "TSM": GlobalSymbol("TSM", "TSMC ADR", "SEMICONDUCTOR", "yfinance_poc"),
    "NVDA": GlobalSymbol("NVDA", "NVIDIA", "SEMICONDUCTOR", "yfinance_poc"),
    "DRIV": GlobalSymbol("DRIV", "Global X Autonomous & Electric Vehicles ETF", "AUTO", "yfinance_poc"),
    "XLY": GlobalSymbol("XLY", "Consumer Discretionary Select Sector SPDR ETF", "US_SECTOR_ETF", "yfinance_poc"),
    "TSLA": GlobalSymbol("TSLA", "Tesla", "AUTO", "yfinance_poc"),
    "GM": GlobalSymbol("GM", "General Motors", "AUTO", "yfinance_poc"),
    "F": GlobalSymbol("F", "Ford Motor", "AUTO", "yfinance_poc"),
    "XLI": GlobalSymbol("XLI", "Industrial Select Sector SPDR ETF", "US_SECTOR_ETF", "yfinance_poc"),
    "XLF": GlobalSymbol("XLF", "Financial Select Sector SPDR ETF", "US_SECTOR_ETF", "yfinance_poc"),
    "XLV": GlobalSymbol("XLV", "Health Care Select Sector SPDR ETF", "US_SECTOR_ETF", "yfinance_poc"),
    "IBB": GlobalSymbol("IBB", "iShares Biotechnology ETF", "BIOTECH", "yfinance_poc"),
    "XBI": GlobalSymbol("XBI", "SPDR S&P Biotech ETF", "BIOTECH", "yfinance_poc"),
    "XLE": GlobalSymbol("XLE", "Energy Select Sector SPDR ETF", "US_SECTOR_ETF", "yfinance_poc"),
    "XLB": GlobalSymbol("XLB", "Materials Select Sector SPDR ETF", "US_SECTOR_ETF", "yfinance_poc"),
    "LIT": GlobalSymbol("LIT", "Global X Lithium & Battery Tech ETF", "MATERIALS_BATTERY", "yfinance_poc"),
    "KORU": GlobalSymbol("KORU", "Direxion Daily MSCI South Korea Bull 3X ETF", "US_KOREA_ETF", "yfinance_poc"),
    "EWY": GlobalSymbol("EWY", "iShares MSCI South Korea ETF", "US_KOREA_ETF", "yfinance_poc"),
    "SPY": GlobalSymbol("SPY", "SPDR S&P 500 ETF Trust", "US_ETF", "yfinance_poc"),
    "QQQ": GlobalSymbol("QQQ", "Invesco QQQ Trust", "US_ETF", "yfinance_poc"),
    "USDKRW=X": GlobalSymbol("USDKRW=X", "USD/KRW", "FX", "yfinance_poc"),
    "^N225": GlobalSymbol("^N225", "Nikkei 225", "ASIA_INDEX", "yfinance_poc"),
    "^TWII": GlobalSymbol("^TWII", "Taiwan Weighted", "ASIA_INDEX", "yfinance_poc"),
}

YFINANCE_INTRADAY_SYMBOLS: dict[str, GlobalSymbol] = {
    "ES=F": GlobalSymbol("ES=F", "S&P 500 E-mini Futures", "US_FUTURES", "yfinance_poc"),
    "NQ=F": GlobalSymbol("NQ=F", "Nasdaq 100 Futures", "US_FUTURES", "yfinance_poc"),
    "KORU": GlobalSymbol("KORU", "Direxion Daily MSCI South Korea Bull 3X ETF", "US_KOREA_ETF", "yfinance_poc"),
    "EWY": GlobalSymbol("EWY", "iShares MSCI South Korea ETF", "US_KOREA_ETF", "yfinance_poc"),
    "SPY": GlobalSymbol("SPY", "SPDR S&P 500 ETF Trust", "US_ETF", "yfinance_poc"),
    "QQQ": GlobalSymbol("QQQ", "Invesco QQQ Trust", "US_ETF", "yfinance_poc"),
    "SOXX": GlobalSymbol("SOXX", "iShares Semiconductor ETF", "SEMICONDUCTOR", "yfinance_poc"),
    "DRIV": GlobalSymbol("DRIV", "Global X Autonomous & Electric Vehicles ETF", "AUTO", "yfinance_poc"),
    "XLY": GlobalSymbol("XLY", "Consumer Discretionary Select Sector SPDR ETF", "US_SECTOR_ETF", "yfinance_poc"),
    "XLI": GlobalSymbol("XLI", "Industrial Select Sector SPDR ETF", "US_SECTOR_ETF", "yfinance_poc"),
    "XLF": GlobalSymbol("XLF", "Financial Select Sector SPDR ETF", "US_SECTOR_ETF", "yfinance_poc"),
    "XLV": GlobalSymbol("XLV", "Health Care Select Sector SPDR ETF", "US_SECTOR_ETF", "yfinance_poc"),
    "XLE": GlobalSymbol("XLE", "Energy Select Sector SPDR ETF", "US_SECTOR_ETF", "yfinance_poc"),
    "XLB": GlobalSymbol("XLB", "Materials Select Sector SPDR ETF", "US_SECTOR_ETF", "yfinance_poc"),
    "USDKRW=X": GlobalSymbol("USDKRW=X", "USD/KRW", "FX", "yfinance_poc"),
}

FRED_DAILY_SERIES: dict[str, GlobalSymbol] = {
    "VIXCLS": GlobalSymbol("VIXCLS", "CBOE VIX", "VOLATILITY", "fred"),
    "DGS10": GlobalSymbol("DGS10", "US Treasury 10Y", "RATE", "fred"),
    "DGS2": GlobalSymbol("DGS2", "US Treasury 2Y", "RATE", "fred"),
    "T10Y3M": GlobalSymbol("T10Y3M", "US 10Y-3M Spread", "RATE", "fred"),
    "DCOILWTICO": GlobalSymbol("DCOILWTICO", "WTI Crude Oil", "COMMODITY", "fred"),
    "DCOILBRENTEU": GlobalSymbol("DCOILBRENTEU", "Brent Crude Oil", "COMMODITY", "fred"),
}
