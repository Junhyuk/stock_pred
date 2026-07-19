from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

KST = ZoneInfo("Asia/Seoul")
MARKETS = ("KOSPI", "KOSDAQ")
INVESTOR_LABELS = (
    "금융투자",
    "보험",
    "투신",
    "사모",
    "은행",
    "기타금융",
    "연기금",
    "기타법인",
    "개인",
    "외국인",
    "기타외국인",
    "기관합계",
)
TOP_PURCHASE_INVESTORS = ("외국인", "기관합계", "개인")
FINAL_STAGE = "final"
PRELIMINARY_STAGE = "preliminary"
FLOW_STAGES = (FINAL_STAGE, PRELIMINARY_STAGE)
FINAL_CUTOFF_KST = time(18, 20)


class MarketInvestorFlowError(RuntimeError):
    """Base exception for a market-wide investor-flow collection failure."""


class FinalityGuardError(MarketInvestorFlowError):
    """Raised when a same-day final collection starts before the final cutoff."""


class IncompleteInvestorFlowError(MarketInvestorFlowError):
    """Raised when a market response does not contain all 12 investor categories."""


@dataclass(frozen=True)
class MarketInvestorFlowSnapshot:
    run: dict[str, Any]
    flows: pd.DataFrame
    top_purchases: pd.DataFrame


def new_run_id() -> str:
    return uuid4().hex


def collect_market_investor_flow_snapshot(
    trade_date: str | date,
    *,
    stage: str = FINAL_STAGE,
    markets: Iterable[str] = MARKETS,
    top_n: int = 30,
    include_top_purchases: bool = True,
    now: datetime | None = None,
    run_id: str | None = None,
    stock_api: Any | None = None,
) -> MarketInvestorFlowSnapshot:
    """Collect raw market-wide investor flows without aggregating ticker panels.

    ``get_market_trading_value_by_investor`` receives the market as its third
    positional argument in the installed pykrx version.  Keeping that call
    positional avoids coupling this collector to a parameter-name change.
    """

    target_date = _to_date(trade_date)
    normalized_stage = _validate_stage(stage)
    resolved_markets = tuple(str(market).upper() for market in markets)
    if not resolved_markets:
        raise ValueError("At least one market is required")
    if any(market not in MARKETS for market in resolved_markets):
        raise ValueError(f"Unsupported markets: {resolved_markets}")
    _validate_finality(target_date, normalized_stage, now=now)

    client = stock_api or _load_pykrx_stock()
    current_run_id = run_id or new_run_id()
    started_at = _utcnow()
    date_text = target_date.strftime("%Y%m%d")
    is_final = normalized_stage == FINAL_STAGE
    availability_policy = (
        "after_close_18_20_kst" if is_final else "preliminary_not_model_eligible"
    )

    flow_frames: list[pd.DataFrame] = []
    for market in resolved_markets:
        raw = client.get_market_trading_value_by_investor(
            date_text,
            date_text,
            market,
            etf=False,
            etn=False,
            elw=False,
        )
        flow_frames.append(
            normalize_market_investor_flows(
                raw,
                trade_date=target_date,
                market=market,
                source="pykrx",
                run_id=current_run_id,
                is_final=is_final,
                availability_policy=availability_policy,
                collected_at=started_at,
            )
        )

    flows = pd.concat(flow_frames, ignore_index=True)
    top_purchases = pd.DataFrame(columns=_top_purchase_columns())
    top_status = "skipped"
    top_error: str | None = None
    if include_top_purchases:
        try:
            top_frames: list[pd.DataFrame] = []
            for market in resolved_markets:
                for investor in TOP_PURCHASE_INVESTORS:
                    raw = client.get_market_net_purchases_of_equities(
                        date_text,
                        date_text,
                        market=market,
                        investor=investor,
                    )
                    top_frames.append(
                        normalize_top_net_purchases(
                            raw,
                            trade_date=target_date,
                            market=market,
                            investor=investor,
                            top_n=top_n,
                            source="pykrx",
                            run_id=current_run_id,
                            is_final=is_final,
                            collected_at=started_at,
                        )
                    )
            top_purchases = pd.concat(top_frames, ignore_index=True)
            top_status = "ready"
        except Exception as exc:  # Primary 12-category flows remain useful and final.
            top_status = "failed"
            top_error = str(exc)

    run = {
        "run_id": current_run_id,
        "trade_date": target_date,
        "stage": normalized_stage,
        "source": "pykrx",
        "status": "ready" if is_final else "preliminary",
        "is_final": is_final,
        "expected_investor_rows": len(resolved_markets) * len(INVESTOR_LABELS),
        "received_investor_rows": int(len(flows)),
        "top_purchases_status": top_status,
        "error_message": top_error,
        "started_at": started_at,
        "completed_at": _utcnow(),
    }
    return MarketInvestorFlowSnapshot(
        run=run,
        flows=flows,
        top_purchases=top_purchases,
    )


def normalize_market_investor_flows(
    raw: pd.DataFrame,
    *,
    trade_date: date,
    market: str,
    source: str,
    run_id: str,
    is_final: bool,
    availability_policy: str,
    collected_at: datetime,
) -> pd.DataFrame:
    """Normalize a pykrx market-level response and require all 12 categories."""

    if raw is None or raw.empty:
        raise IncompleteInvestorFlowError(f"{market}: pykrx returned no investor-flow rows")
    frame = raw.reset_index().copy()
    investor_column = _find_column(frame, ("투자자구분", "투자자", "Investor", "index"))
    buy_column = _find_column(frame, ("매수", "매수거래대금", "매수금액", "buy_value"))
    sell_column = _find_column(frame, ("매도", "매도거래대금", "매도금액", "sell_value"))
    net_column = _find_column(frame, ("순매수", "순매수거래대금", "순매수금액", "net_value"))
    output = pd.DataFrame(
        {
            "trade_date": trade_date,
            "market": str(market).upper(),
            "investor": frame[investor_column].map(_normalize_investor_label),
            "buy_value": _to_number(frame[buy_column]),
            "sell_value": _to_number(frame[sell_column]),
            "net_value": _to_number(frame[net_column]),
            "source": source,
            "run_id": run_id,
            "is_final": bool(is_final),
            "availability_policy": availability_policy,
            "collected_at": collected_at,
        }
    )
    output = output[output["investor"].isin(INVESTOR_LABELS)].copy()
    if output["investor"].duplicated().any():
        duplicates = sorted(output.loc[output["investor"].duplicated(), "investor"].unique())
        raise IncompleteInvestorFlowError(f"{market}: duplicate investor categories: {duplicates}")
    missing = [investor for investor in INVESTOR_LABELS if investor not in set(output["investor"])]
    if missing:
        raise IncompleteInvestorFlowError(f"{market}: missing investor categories: {', '.join(missing)}")
    if output[["buy_value", "sell_value", "net_value"]].isna().any().any():
        raise IncompleteInvestorFlowError(f"{market}: non-numeric investor trading values")
    output["investor"] = pd.Categorical(output["investor"], categories=INVESTOR_LABELS, ordered=True)
    return output.sort_values("investor").reset_index(drop=True)


def normalize_top_net_purchases(
    raw: pd.DataFrame,
    *,
    trade_date: date,
    market: str,
    investor: str,
    top_n: int,
    source: str,
    run_id: str,
    is_final: bool,
    collected_at: datetime,
) -> pd.DataFrame:
    """Normalize the pykrx investor-specific net-purchase ranking."""

    if raw is None or raw.empty:
        raise MarketInvestorFlowError(f"{market}/{investor}: pykrx returned no top net-purchase rows")
    frame = raw.reset_index().copy()
    symbol_column = _find_column(frame, ("티커", "종목코드", "Symbol", "Code", "index"))
    name_column = _find_optional_column(frame, ("종목명", "종목", "Name", "name"))
    net_column = _find_column(frame, ("순매수거래대금", "순매수", "net_buy_value", "net"))
    output = pd.DataFrame(
        {
            "symbol": frame[symbol_column].astype(str).str.strip().str.zfill(6),
            "name": frame[name_column].astype(str).str.strip() if name_column else None,
            "net_buy_value": _to_number(frame[net_column]),
        }
    )
    output = output.dropna(subset=["symbol", "net_buy_value"])
    output = output.sort_values("net_buy_value", ascending=False).head(max(1, int(top_n))).reset_index(drop=True)
    if output.empty:
        raise MarketInvestorFlowError(f"{market}/{investor}: no normalized top net-purchase rows")
    output.insert(0, "rank", range(1, len(output) + 1))
    output.insert(0, "investor", investor)
    output.insert(0, "market", str(market).upper())
    output.insert(0, "trade_date", trade_date)
    output["source"] = source
    output["run_id"] = run_id
    output["is_final"] = bool(is_final)
    output["collected_at"] = collected_at
    return output[_top_purchase_columns()]


def failed_market_investor_flow_run(
    *,
    trade_date: str | date,
    stage: str,
    error: Exception | str,
    started_at: datetime,
    run_id: str | None = None,
    markets: Iterable[str] = MARKETS,
) -> pd.DataFrame:
    resolved_markets = tuple(markets)
    return pd.DataFrame(
        [
            {
                "run_id": run_id or new_run_id(),
                "trade_date": _to_date(trade_date),
                "stage": _validate_stage(stage),
                "source": "pykrx",
                "status": "failed",
                "is_final": _validate_stage(stage) == FINAL_STAGE,
                "expected_investor_rows": len(resolved_markets) * len(INVESTOR_LABELS),
                "received_investor_rows": 0,
                "top_purchases_status": "not_run",
                "error_message": str(error),
                "started_at": started_at,
                "completed_at": _utcnow(),
            }
        ]
    )


def completed_trade_dates(conn, *, limit: int) -> list[date]:
    """Return dates where both market benchmarks exist, newest dates last."""

    rows = conn.execute(
        """
        SELECT date
        FROM benchmark_daily
        WHERE benchmark IN ('KOSPI', 'KOSDAQ', '1001', '2001')
        GROUP BY date
        HAVING COUNT(DISTINCT benchmark) >= 2
        ORDER BY date DESC
        LIMIT ?
        """,
        [max(1, int(limit))],
    ).fetchall()
    return sorted(pd.to_datetime([item[0] for item in rows]).date.tolist()) if rows else []


def final_flow_exists(conn, trade_date: date) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM market_investor_flows_daily
        WHERE trade_date = ? AND source = 'pykrx' AND is_final = TRUE
        """,
        [trade_date],
    ).fetchone()
    return bool(row and int(row[0]) >= len(MARKETS) * len(INVESTOR_LABELS))


def _validate_finality(target_date: date, stage: str, *, now: datetime | None) -> None:
    if stage != FINAL_STAGE:
        return
    current = _as_kst(now)
    if target_date > current.date():
        raise FinalityGuardError(f"{target_date.isoformat()}: future trade date is not final")
    if target_date == current.date() and current.time() < FINAL_CUTOFF_KST:
        raise FinalityGuardError(
            f"{target_date.isoformat()}: final market flow is available after "
            f"{FINAL_CUTOFF_KST.strftime('%H:%M')} KST"
        )


def _validate_stage(stage: str) -> str:
    value = str(stage or "").strip().lower()
    if value not in FLOW_STAGES:
        raise ValueError(f"Unsupported market investor-flow stage: {stage}")
    return value


def _load_pykrx_stock():
    try:
        from pykrx import stock
    except Exception as exc:  # pragma: no cover - environment-specific import failure
        raise MarketInvestorFlowError("pykrx is required for market investor-flow collection") from exc
    return stock


def _find_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str:
    column = _find_optional_column(frame, candidates)
    if column is None:
        raise MarketInvestorFlowError(f"Expected one of {list(candidates)} in response columns {list(frame.columns)}")
    return column


def _find_optional_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    lookup = {str(column).strip().lower(): str(column) for column in frame.columns}
    for candidate in candidates:
        resolved = lookup.get(candidate.lower())
        if resolved is not None:
            return resolved
    return None


def _normalize_investor_label(value: Any) -> str:
    text = str(value).strip().replace(" ", "")
    aliases = {
        "기타금융": "기타금융",
        "기타법인": "기타법인",
        "기타외국인": "기타외국인",
        "기관합계": "기관합계",
        "외국인": "외국인",
        "금융투자": "금융투자",
        "보험": "보험",
        "투신": "투신",
        "사모": "사모",
        "은행": "은행",
        "연기금": "연기금",
        "개인": "개인",
    }
    return aliases.get(text, text)


def _to_number(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(cleaned, errors="coerce")


def _to_date(value: str | date) -> date:
    return pd.to_datetime(value).date()


def _as_kst(now: datetime | None) -> datetime:
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    return current.astimezone(KST)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _top_purchase_columns() -> list[str]:
    return [
        "trade_date",
        "market",
        "investor",
        "rank",
        "symbol",
        "name",
        "net_buy_value",
        "source",
        "run_id",
        "is_final",
        "collected_at",
    ]
