from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time
from time import sleep
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from roboquant.utils import yyyymmdd

KST = ZoneInfo("Asia/Seoul")
KOSPI = "KOSPI"
KOSDAQ = "KOSDAQ"
KOSPI_SELECTION_RULE = "fixed_user_anchor_v1"
KOSDAQ_SELECTION_RULE = "point_in_time_market_cap_top3_v1"
FINAL_CUTOFF_KST = time(18, 20)
ANCHOR_COLUMNS = [
    "asof_date",
    "market",
    "symbol",
    "name",
    "selection_rule",
    "market_cap_rank",
    "market_cap",
    "weight",
    "security_type",
    "is_final",
    "source",
    "created_at",
]


class MarketAnchorCollectionError(RuntimeError):
    """Raised when a point-in-time anchor universe cannot be reconstructed."""


def fetch_market_anchor_constituents_by_date(
    target_date: str | date,
    settings: Mapping[str, Any],
    *,
    stock_api: Any | None = None,
    now: datetime | None = None,
    name_lookup: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """Collect the fixed KOSPI anchors and date-specific KOSDAQ Top-N anchors.

    The KOSDAQ selection is derived from the same day's KRX market-cap table;
    it never substitutes a current constituent list for historical dates.
    """

    asof = _to_date(target_date)
    client = stock_api or _load_pykrx_stock()
    anchors = [str(symbol).zfill(6) for symbol in settings.get("kospi_anchors", [])]
    if not anchors:
        raise MarketAnchorCollectionError("market_anchor_shadow.kospi_anchors is required")
    top_n = max(1, int(settings.get("kosdaq_top_n", 3)))
    source = str(settings.get("source") or "pykrx")
    stamp = _utcnow()
    final = _is_final(asof, now=now)
    names = {str(symbol).zfill(6): str(name) for symbol, name in (name_lookup or {}).items()}

    kospi = _market_cap_snapshot(client, asof, KOSPI)
    kosdaq = _market_cap_snapshot(client, asof, KOSDAQ)
    kospi_rows = _select_fixed_kospi(
        kospi,
        anchors=anchors,
        asof_date=asof,
        source=source,
        created_at=stamp,
        is_final=final,
        name_lookup=names,
        client=client,
    )
    kosdaq_rows = _select_dynamic_kosdaq(
        kosdaq,
        top_n=top_n,
        asof_date=asof,
        source=source,
        created_at=stamp,
        is_final=final,
        name_lookup=names,
        client=client,
    )
    return pd.concat([kospi_rows, kosdaq_rows], ignore_index=True)[ANCHOR_COLUMNS]


def fetch_market_anchor_history(
    dates: list[date],
    settings: Mapping[str, Any],
    *,
    stock_api: Any | None = None,
    sleep_seconds: float | None = None,
    sleep_fn: Callable[[float], None] = sleep,
    name_lookup: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """Fetch a bounded ordered history suitable for resumable callers/tests."""

    frames: list[pd.DataFrame] = []
    pause = max(0.0, float(settings.get("sleep_seconds", 0.2) if sleep_seconds is None else sleep_seconds))
    for index, current in enumerate(sorted(set(dates))):
        frames.append(
            fetch_market_anchor_constituents_by_date(
                current,
                settings,
                stock_api=stock_api,
                name_lookup=name_lookup,
            )
        )
        if pause and index + 1 < len(dates):
            sleep_fn(pause)
    if not frames:
        return pd.DataFrame(columns=ANCHOR_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def _market_cap_snapshot(client: Any, asof_date: date, market: str) -> pd.DataFrame:
    try:
        raw = client.get_market_cap_by_ticker(yyyymmdd(asof_date), market=market)
    except Exception as exc:
        raise MarketAnchorCollectionError(f"{asof_date.isoformat()} {market} market-cap fetch failed: {exc}") from exc
    if raw is None or raw.empty:
        raise MarketAnchorCollectionError(f"{asof_date.isoformat()} {market} market-cap response is empty")
    frame = raw.reset_index().copy()
    symbol_column = _find_column(frame, ("티커", "종목코드", "Symbol", "Code", "index"))
    cap_column = _find_column(frame, ("시가총액", "market_cap", "MarketCap"))
    frame = frame.rename(columns={symbol_column: "symbol"})
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.zfill(6)
    frame["market_cap"] = pd.to_numeric(
        frame[cap_column].astype(str).str.replace(",", "", regex=False),
        errors="coerce",
    )
    frame = frame.dropna(subset=["symbol", "market_cap"])
    frame = frame[frame["market_cap"] > 0.0].sort_values(
        ["market_cap", "symbol"], ascending=[False, True]
    )
    frame["market_cap_rank"] = range(1, len(frame) + 1)
    if frame.empty:
        raise MarketAnchorCollectionError(f"{asof_date.isoformat()} {market} has no valid market-cap rows")
    return frame[["symbol", "market_cap", "market_cap_rank"]].reset_index(drop=True)


def _select_fixed_kospi(
    snapshot: pd.DataFrame,
    *,
    anchors: list[str],
    asof_date: date,
    source: str,
    created_at: datetime,
    is_final: bool,
    name_lookup: Mapping[str, str],
    client: Any,
) -> pd.DataFrame:
    selected = snapshot[snapshot["symbol"].isin(anchors)].copy()
    missing = sorted(set(anchors).difference(selected["symbol"]))
    if missing:
        raise MarketAnchorCollectionError(
            f"{asof_date.isoformat()} KOSPI missing fixed anchor market caps: {', '.join(missing)}"
        )
    selected = selected.set_index("symbol").loc[anchors].reset_index()
    return _finalize_selection(
        selected,
        market=KOSPI,
        selection_rule=KOSPI_SELECTION_RULE,
        asof_date=asof_date,
        source=source,
        created_at=created_at,
        is_final=is_final,
        name_lookup=name_lookup,
        client=client,
    )


def _select_dynamic_kosdaq(
    snapshot: pd.DataFrame,
    *,
    top_n: int,
    asof_date: date,
    source: str,
    created_at: datetime,
    is_final: bool,
    name_lookup: Mapping[str, str],
    client: Any,
) -> pd.DataFrame:
    selected_rows: list[dict[str, Any]] = []
    for item in snapshot.to_dict(orient="records"):
        symbol = str(item["symbol"])
        name = _resolve_name(symbol, name_lookup=name_lookup, client=client)
        if not _is_ordinary_share(name):
            continue
        selected_rows.append({**item, "name": name})
        if len(selected_rows) >= top_n:
            break
    if len(selected_rows) < top_n:
        raise MarketAnchorCollectionError(
            f"{asof_date.isoformat()} KOSDAQ selected {len(selected_rows)}/{top_n} ordinary-share anchors"
        )
    selected = pd.DataFrame(selected_rows)
    return _finalize_selection(
        selected,
        market=KOSDAQ,
        selection_rule=KOSDAQ_SELECTION_RULE,
        asof_date=asof_date,
        source=source,
        created_at=created_at,
        is_final=is_final,
        name_lookup=name_lookup,
        client=client,
    )


def _finalize_selection(
    selected: pd.DataFrame,
    *,
    market: str,
    selection_rule: str,
    asof_date: date,
    source: str,
    created_at: datetime,
    is_final: bool,
    name_lookup: Mapping[str, str],
    client: Any,
) -> pd.DataFrame:
    output = selected.copy()
    if "name" not in output.columns:
        output["name"] = output["symbol"].map(
            lambda symbol: _resolve_name(str(symbol), name_lookup=name_lookup, client=client)
        )
    total_market_cap = pd.to_numeric(output["market_cap"], errors="coerce").sum()
    if not total_market_cap or pd.isna(total_market_cap):
        raise MarketAnchorCollectionError(f"{asof_date.isoformat()} {market} selected market cap is invalid")
    output["asof_date"] = asof_date
    output["market"] = market
    output["selection_rule"] = selection_rule
    output["weight"] = pd.to_numeric(output["market_cap"], errors="coerce") / float(total_market_cap)
    output["security_type"] = "ORDINARY_SHARE"
    output["is_final"] = bool(is_final)
    output["source"] = source
    output["created_at"] = created_at
    return output[ANCHOR_COLUMNS]


def _resolve_name(symbol: str, *, name_lookup: Mapping[str, str], client: Any) -> str | None:
    if symbol in name_lookup:
        return name_lookup[symbol]
    try:
        name = client.get_market_ticker_name(symbol)
    except Exception:
        return None
    value = str(name or "").strip()
    return value or None


def _is_ordinary_share(name: str | None) -> bool:
    raw = str(name or "").strip()
    text = raw.upper()
    if not text:
        return False
    if any(token in text for token in ("ETF", "ETN", "스팩", "SPAC", "PREFERRED")):
        return False
    # Korean preferred-share labels are suffixes.  Matching any "우" would
    # incorrectly remove ordinary companies whose name merely contains it.
    return re.search(r"(?:우|[0-9]우(?:B)?)$", raw, flags=re.IGNORECASE) is None


def _load_pykrx_stock() -> Any:
    try:
        from pykrx import stock
    except Exception as exc:  # pragma: no cover - environment-specific import failure
        raise MarketAnchorCollectionError("pykrx is required for anchor market-cap collection") from exc
    return stock


def _find_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str:
    lookup = {str(column).strip().lower(): str(column) for column in frame.columns}
    for candidate in candidates:
        resolved = lookup.get(candidate.lower())
        if resolved:
            return resolved
    raise MarketAnchorCollectionError(
        f"Expected one of {list(candidates)} in market-cap response columns {list(frame.columns)}"
    )


def _to_date(value: str | date) -> date:
    return pd.to_datetime(value).date()


def _is_final(asof_date: date, *, now: datetime | None) -> bool:
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    current = current.astimezone(KST)
    return asof_date < current.date() or (
        asof_date == current.date() and current.time() >= FINAL_CUTOFF_KST
    )


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
