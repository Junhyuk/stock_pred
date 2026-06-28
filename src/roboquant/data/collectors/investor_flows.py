from __future__ import annotations

from datetime import datetime

import pandas as pd

FLOW_COLUMNS = [
    "date",
    "symbol",
    "foreign_buy_value",
    "foreign_sell_value",
    "foreign_net_value",
    "institution_buy_value",
    "institution_sell_value",
    "institution_net_value",
    "retail_buy_value",
    "retail_sell_value",
    "retail_net_value",
    "pension_net_value",
    "trust_net_value",
    "private_fund_net_value",
    "source",
    "collected_at",
]

INVESTOR_MAP = {
    "foreign": "외국인",
    "institution": "기관합계",
    "retail": "개인",
    "pension": "연기금",
    "trust": "투신",
    "private_fund": "사모",
}


def fetch_investor_flows(
    start_date: str,
    end_date: str,
    markets: list[str] | None = None,
    investor_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Fetch investor trading-value flow panel from pykrx."""
    try:
        from pykrx import stock
    except Exception as exc:
        raise RuntimeError("pykrx is required for investor flow collection") from exc

    markets = markets or ["KOSPI", "KOSDAQ"]
    investor_map = investor_map or INVESTOR_MAP
    business_dates = pd.bdate_range(start_date, end_date)
    frames: list[pd.DataFrame] = []

    for target_date in business_dates:
        date_string = target_date.strftime("%Y%m%d")
        for market in markets:
            market_frame: pd.DataFrame | None = None
            for standard_name, pykrx_name in investor_map.items():
                raw = stock.get_market_trading_value_and_volume_by_ticker(
                    date_string,
                    date_string,
                    market=market,
                    investor=pykrx_name,
                )
                normalized = _normalize_investor_frame(raw, standard_name, date_string)
                if normalized.empty:
                    continue
                market_frame = (
                    normalized
                    if market_frame is None
                    else market_frame.merge(normalized, on=["date", "symbol"], how="outer")
                )
            if market_frame is not None and not market_frame.empty:
                market_frame["source"] = "pykrx"
                market_frame["collected_at"] = datetime.utcnow()
                frames.append(market_frame)

    if not frames:
        return _empty_flows()
    frame = pd.concat(frames, ignore_index=True)
    for column in FLOW_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame[FLOW_COLUMNS].drop_duplicates(["date", "symbol"])


def _normalize_investor_frame(
    raw: pd.DataFrame,
    standard_name: str,
    target_date: str,
) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["date", "symbol"])
    frame = raw.reset_index()
    symbol_column = _find_symbol_column(frame)
    frame = frame.rename(columns={symbol_column: "symbol"})
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)

    if "날짜" in frame.columns:
        frame["date"] = pd.to_datetime(frame["날짜"]).dt.date
    elif "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
    else:
        frame["date"] = pd.to_datetime(target_date).date()

    output = pd.DataFrame({"date": frame["date"], "symbol": frame["symbol"]})
    if standard_name in {"foreign", "institution", "retail"}:
        output[f"{standard_name}_buy_value"] = _coerce_first_available(
            frame,
            ["매수거래대금", "매수", "buy_value", "buy"],
        )
        output[f"{standard_name}_sell_value"] = _coerce_first_available(
            frame,
            ["매도거래대금", "매도", "sell_value", "sell"],
        )
        output[f"{standard_name}_net_value"] = _coerce_first_available(
            frame,
            ["순매수거래대금", "순매수", "net_value", "net"],
        )
    else:
        output[f"{standard_name}_net_value"] = _coerce_first_available(
            frame,
            ["순매수거래대금", "순매수", "net_value", "net"],
        )
    return output.dropna(subset=["date", "symbol"])


def _find_symbol_column(frame: pd.DataFrame):
    for column in ("티커", "종목코드", "Symbol", "Code", "index"):
        if column in frame.columns:
            return column
    return frame.columns[0]


def _coerce_first_available(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    for column in columns:
        if column in frame.columns:
            return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(pd.NA, index=frame.index, dtype="Float64")


def _empty_flows() -> pd.DataFrame:
    return pd.DataFrame(columns=FLOW_COLUMNS)
