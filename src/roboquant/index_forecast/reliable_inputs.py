"""Point-in-time inputs for the isolated, research-only next-session forecast.

No membership or participant observation is inferred from a current security list.
Missing release times and incomplete panels make the relevant group unavailable.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

KST = ZoneInfo("Asia/Seoul")
BREADTH_FEATURES = ["breadth_mean_return_1d", "breadth_up_share_1d", "breadth_above_ma20_share"]
PARTICIPANTS = ("foreign", "institution", "pension")
PARTICIPANT_FEATURES = [f"{p}_net_to_turnover{suffix}" for p in PARTICIPANTS
                        for suffix in ("", "_5d", "_20d")]


def utc_timestamp(value: Any) -> pd.Timestamp:
    """Interpret explicit naive timestamps as KST, preserving even midnight."""
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return pd.NaT
        return (stamp.tz_localize(KST) if stamp.tzinfo is None else stamp).tz_convert("UTC")
    except (TypeError, ValueError):
        return pd.NaT


def session_schedule(start: Any, end: Any, sessions: Any = None) -> tuple[pd.DataFrame, str]:
    """Return verified exchange session closes; fixtures may supply explicit sessions."""
    if sessions is None:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XKRX")
        start_day = max(pd.Timestamp(start).date(), calendar.first_session.date())
        end_day = min(pd.Timestamp(end).date(), calendar.last_session.date())
        if start_day > end_day:
            raise ValueError("no exchange sessions in requested window")
        labels = calendar.sessions_in_range(start_day, end_day)
        closes = [calendar.session_close(label) for label in labels]
        opens = [calendar.session_open(label) for label in labels]
        return pd.DataFrame({"date": [label.date().isoformat() for label in labels],
                             "open_at": pd.to_datetime(opens, utc=True),
                             "close_at": pd.to_datetime(closes, utc=True)}), "exchange_calendars:XKRX"
    if isinstance(sessions, pd.DataFrame):
        frame = sessions.copy()
        dates = frame["date"] if "date" in frame else frame.index
        close_column = next((c for c in ("close_at", "market_close", "close") if c in frame), None)
        open_column = next((c for c in ("open_at", "market_open", "open") if c in frame), None)
        closes = frame[close_column].map(utc_timestamp).tolist() if close_column else None
        opens = frame[open_column].map(utc_timestamp).tolist() if open_column else None
    else:
        dates, closes, opens = sessions, None, None
    dates = [pd.Timestamp(day).date().isoformat() for day in dates]
    if closes is None:
        closes = [utc_timestamp(day + "T15:30:00+09:00") for day in dates]
    if opens is None:
        opens = [utc_timestamp(day + "T09:00:00+09:00") for day in dates]
    return pd.DataFrame({"date": dates, "open_at": opens, "close_at": closes}).sort_values("date"), "explicit_session_fixture"


def resolve_forecast_context(asof_date: Any, *, now: datetime | None = None,
                             sessions: Any = None, target_date: Any = None) -> dict:
    issued = utc_timestamp(now if now is not None else datetime.now(KST))
    asof = pd.Timestamp(asof_date).date().isoformat()
    result = {"status": "blocked_calendar_unavailable", "asof_date": asof,
              "expected_asof_date": None, "target_date": None,
              "issued_at": issued.isoformat(), "prediction_cutoff": issued.isoformat(),
              "stage": "preliminary", "calendar_source": None, "reasons": []}
    try:
        local_day = issued.tz_convert(KST).normalize().tz_localize(None)
        schedule, source = session_schedule(local_day - timedelta(days=45),
                                             local_day + timedelta(days=45), sessions)
        schedule = schedule.dropna(subset=["close_at"]).sort_values("date")
        closed = schedule.loc[schedule["close_at"] <= issued]
        if closed.empty:
            raise ValueError("no completed session in available exchange calendar")
        expected = closed.iloc[-1]["date"]
        result.update(calendar_source=source, expected_asof_date=expected)
        if asof != expected:
            return {**result, "status": "blocked_stale_input", "reasons": ["index_close_is_not_latest_completed_session"]}
        upcoming = schedule.loc[schedule["date"] > expected]
        if upcoming.empty:
            raise ValueError("next session unavailable in exchange calendar")
        target = upcoming.iloc[0]["date"]
        target_cutoff = utc_timestamp(target + "T08:00:00+09:00")
        target_open = upcoming.iloc[0]["open_at"]
        if pd.isna(target_open):
            raise ValueError("next session open unavailable in exchange calendar")
        result.update(calendar_source=source, expected_asof_date=expected, target_date=target,
                      prediction_cutoff=min(issued, target_cutoff).isoformat(),
                      scheduled_cutoff=target_cutoff.isoformat(), issuance_deadline=target_open.isoformat(),
                      target_close_at=upcoming.iloc[0]["close_at"].isoformat(),
                      stage="premarket_final" if issued >= target_cutoff else "preliminary")
        if target_date is not None and pd.Timestamp(target_date).date().isoformat() != target:
            return {**result, "status": "blocked_invalid_target", "reasons": ["target_is_not_next_krx_session"]}
        if issued >= target_open:
            return {**result, "status": "blocked_issuance_deadline", "stage": "late",
                    "reasons": ["actual_issue_at_or_after_target_session_open"]}
        return {**result, "status": "ready"}
    except (ImportError, ValueError, TypeError, KeyError, OverflowError) as exc:
        return {**result, "reasons": [f"calendar_unavailable:{type(exc).__name__}"]}


def _dated(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_datetime(frame[column], errors="coerce").dt.strftime("%Y-%m-%d")


def eligible_stock_price_revisions(prices: pd.DataFrame, cutoff: Any, *,
                                    timestamp_parser=utc_timestamp) -> pd.DataFrame:
    """Select verified final revisions; contradictory ordering ties are unusable.

    An explicit numeric revision orders equal release timestamps. Otherwise the
    observed ingestion sequence orders versions. Equal ordering keys with
    different economic values are ambiguous, so that symbol/date is excluded.
    """
    required = {"date", "symbol", "close", "available_at", "is_final", "revision"}
    first_seen = [c for c in ("ingested_at", "first_seen_at") if c in prices]
    if prices.empty or not required.issubset(prices) or not first_seen:
        return prices.iloc[0:0].copy()
    frame = prices.copy()
    frame["_price_date"] = _dated(frame, "date")
    frame["_price_available"] = pd.to_datetime(frame["available_at"].map(timestamp_parser), utc=True)
    observed = [pd.to_datetime(frame[c].map(timestamp_parser), utc=True) for c in first_seen]
    seen = pd.concat(observed, axis=1)
    frame["_price_seen"] = seen.max(axis=1).where(seen.notna().all(axis=1))
    frame["_price_revision"] = pd.to_numeric(frame["revision"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    stamp = timestamp_parser(cutoff)
    valid = ((frame["_price_available"] <= stamp) & (frame["_price_seen"] <= stamp) &
             frame["is_final"].astype(str).str.lower().isin({"true", "1", "yes"}) &
             frame["revision"].notna() & frame["revision"].astype(str).str.strip().ne("") &
             frame["close"].gt(0) & np.isfinite(frame["close"]))
    frame = frame.loc[valid].copy()
    payload = [c for c in ("close", "traded_value", "turnover", "volume", "price_adjustment",
                           "corporate_action_status", "source", "source_url", "is_final") if c in frame]
    selected = []
    for _, versions in frame.groupby(["symbol", "_price_date"], sort=True):
        versions = versions.loc[versions["_price_available"] == versions["_price_available"].max()]
        if versions["_price_revision"].notna().all():
            versions = versions.loc[versions["_price_revision"] == versions["_price_revision"].max()]
        versions = versions.loc[versions["_price_seen"] == versions["_price_seen"].max()]
        if len(versions.drop_duplicates(payload)) != 1:
            continue
        # Identical retry rows (or equivalent payloads) have no economic conflict.
        versions = versions.assign(_stable_revision=versions["revision"].astype(str)).sort_values("_stable_revision")
        selected.append(versions.iloc[[-1]])
    if not selected:
        return prices.iloc[0:0].copy()
    result = pd.concat(selected, ignore_index=True)
    return result.drop(columns=[c for c in result if c.startswith("_price_") or c == "_stable_revision"])


def build_stock_breadth(prices: pd.DataFrame, membership: pd.DataFrame, *, market: str,
                        asof_cutoffs: dict, target_count: int | None = None) -> pd.DataFrame:
    columns = ["asof_date", *BREADTH_FEATURES, "breadth_member_count"]
    required = {"snapshot_date", "market", "symbol", "market_cap", "security_type", "available_at"}
    if not required.issubset(membership) or not {"date", "symbol", "close", "available_at"}.issubset(prices):
        return pd.DataFrame(columns=columns)
    count = target_count if target_count is not None else {"KOSPI": 100, "KOSDAQ": 50}[market]
    members, panel = membership.copy(), prices.copy()
    members["_date"] = _dated(members, "snapshot_date")
    members["_available"] = members["available_at"].map(utc_timestamp)
    members["market_cap"] = pd.to_numeric(members["market_cap"], errors="coerce")
    panel["_date"] = _dated(panel, "date")
    panel["close"] = pd.to_numeric(panel["close"], errors="coerce")
    panel["_available"] = panel["available_at"].map(utc_timestamp)
    output = []
    for day, cutoff_value in sorted(asof_cutoffs.items()):
        day, cutoff = str(pd.Timestamp(day).date()), utc_timestamp(cutoff_value)
        known = members.loc[(members["market"] == market) & (members["_date"] <= day) &
                            (members["_available"] <= cutoff)]
        if known.empty:
            continue
        # A snapshot is a complete cross-section, not an invitation to combine
        # securities from different future/current constituent lists.
        known = known.loc[known["_date"] == known["_date"].max()]
        known = known.sort_values("_available", kind="stable").drop_duplicates("symbol", keep="last")
        valid = known["security_type"].astype(str).str.upper().isin({"COMMON", "COMMON_STOCK", "보통주"})
        active = pd.Series(False, index=known.index)
        for flag in ("is_suspended", "suspended"):
            if flag in known:
                active |= known[flag].astype(str).str.lower().isin({"false", "0", "no"})
        if "trading_status" in known:
            active |= known["trading_status"].astype(str).str.upper().isin({"ACTIVE", "NORMAL", "정상"})
        valid &= active
        for flag in ("is_suspended", "suspended", "is_etf", "is_etn", "is_spac", "is_preferred"):
            if flag in known:
                valid &= ~known[flag].astype(str).str.lower().isin({"true", "1", "yes"})
        if "trading_status" in known:
            valid &= known["trading_status"].astype(str).str.upper().isin({"ACTIVE", "NORMAL", "정상"})
        if "name" in known:
            valid &= ~known["name"].astype(str).str.contains(r"스팩|SPAC|ETF|ETN", case=False, regex=True)
        known = known.loc[valid & known["market_cap"].gt(0)]
        selected = known.sort_values(["market_cap", "symbol"], ascending=[False, True]).head(count)
        if len(selected) != count:
            continue
        available_prices = eligible_stock_price_revisions(panel.loc[panel["_date"] <= day], cutoff)
        dates = sorted(available_prices["_date"].dropna().unique())[-20:]
        if len(dates) < 20 or dates[-1] != day:
            continue
        returns, above = [], []
        for symbol in selected["symbol"]:
            history = available_prices.loc[available_prices["symbol"] == symbol]
            series = history.set_index("_date")["close"].reindex(dates)
            if series.isna().any() or not np.isfinite(series).all() or (series <= 0).any():
                break
            returns.append(float(series.iloc[-1] / series.iloc[-2] - 1))
            above.append(bool(series.iloc[-1] > series.mean()))
        if len(returns) == count:
            output.append({"asof_date": day, "breadth_mean_return_1d": float(np.mean(returns)),
                           "breadth_up_share_1d": float(np.mean(np.asarray(returns) > 0)),
                           "breadth_above_ma20_share": float(np.mean(above)), "breadth_member_count": count})
    return pd.DataFrame(output, columns=columns)


def _eligible_flows(flows: pd.DataFrame, market: str, cutoff: Any, asof: str) -> pd.DataFrame:
    required = {"trade_date", "market", "investor", "net_value", "turnover", "is_final", "available_at"}
    if not required.issubset(flows):
        return pd.DataFrame()
    data = flows.copy()
    data["_date"] = _dated(data, "trade_date")
    data["_available"] = data["available_at"].map(utc_timestamp)
    aliases = {"FOREIGN": "foreign", "외국인": "foreign", "INSTITUTION": "institution",
               "기관": "institution", "기관합계": "institution", "PENSION": "pension", "연기금": "pension", "연기금등": "pension"}
    data["_participant"] = data["investor"].astype(str).str.upper().map(aliases)
    data["net_value"] = pd.to_numeric(data["net_value"], errors="coerce")
    data["turnover"] = pd.to_numeric(data["turnover"], errors="coerce")
    final = data["is_final"].astype(str).str.lower().isin({"true", "1", "yes"})
    data = data.loc[(data["market"] == market) & final & (data["_date"] <= asof) &
                    (data["_available"] <= utc_timestamp(cutoff)) & data["turnover"].gt(0)]
    data = data.dropna(subset=["_date", "_participant", "net_value", "turnover"])
    return data.sort_values("_available", kind="stable").drop_duplicates(["_date", "_participant"], keep="last")


def build_participant_features(flows: pd.DataFrame, *, market: str, asof_cutoffs: dict) -> pd.DataFrame:
    output = []
    for day, cutoff in sorted(asof_cutoffs.items()):
        day = str(pd.Timestamp(day).date())
        eligible = _eligible_flows(flows, market, cutoff, day)
        if eligible.empty:
            continue
        dates = sorted(eligible["_date"].unique())[-20:]
        if len(dates) < 20 or dates[-1] != day:
            continue
        row: dict = {"asof_date": day}
        for participant in PARTICIPANTS:
            series = eligible.loc[eligible["_participant"] == participant].set_index("_date").reindex(dates)
            if series["net_value"].isna().any() or series["turnover"].isna().any():
                break
            row[f"{participant}_net_to_turnover"] = float(series["net_value"].iloc[-1] / series["turnover"].iloc[-1])
            for width in (5, 20):
                row[f"{participant}_net_to_turnover_{width}d"] = float(series["net_value"].iloc[-width:].sum() /
                                                                          series["turnover"].iloc[-width:].sum())
        if all(c in row for c in PARTICIPANT_FEATURES):
            output.append(row)
    return pd.DataFrame(output, columns=["asof_date", *PARTICIPANT_FEATURES])


def summarize_participant_pressure(flows: pd.DataFrame, *, market: str, cutoff: Any) -> dict:
    """Observed net trading is pressure evidence, never a verified held position."""
    stamp = utc_timestamp(cutoff)
    data = _eligible_flows(flows, market, stamp, stamp.tz_convert(KST).date().isoformat())
    missing = ["foreign_futures_options", "program_trades", "short_sales", "borrow_balance",
               "credit_balance", "etf_flows", "basis", "open_interest", "options_iv_skew"]
    result = {"status": "missing", "market": market, "cutoff": stamp.isoformat(),
              "observed": {}, "inferred": "inconclusive", "confidence": "unavailable",
              "contradictions": [], "unverified": ["net buying does not identify current holdings or intent"],
              "missing": ["foreign_cash", "institution_cash", "pension_cash", *missing]}
    if data.empty:
        return result
    latest = data.loc[data["_date"] == data["_date"].max()]
    observed = {str(r["_participant"]): {"trade_date": r["_date"], "net_value": float(r["net_value"]),
                  "net_to_turnover": float(r["net_value"] / r["turnover"]),
                  "available_at": r["_available"].isoformat()} for _, r in latest.iterrows()}
    signs = [np.sign(r["net_value"]) for r in observed.values() if r["net_value"] != 0]
    contradictions = ["participant_cash_flows_disagree"] if signs and min(signs) < max(signs) else []
    return {**result, "status": "partial", "observed": observed,
            "inferred": "mixed_cash_pressure" if contradictions else "cash_buying_pressure" if signs and min(signs) > 0 else "cash_selling_pressure" if signs and max(signs) < 0 else "inconclusive",
            "confidence": "limited_cash_flow_evidence", "contradictions": contradictions,
            "missing": [f"{p}_cash" for p in PARTICIPANTS if p not in observed] + missing}
