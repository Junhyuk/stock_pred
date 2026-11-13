"""US completed-session inputs with explicit retrospective versus live clocks.

Provider daily date labels and the legacy midnight ``source_timestamp`` are not
publication times. Original historical releases/revisions remain unverified.
"""
from __future__ import annotations

import warnings
from datetime import datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

US_SYMBOLS = ("^GSPC", "^IXIC", "^SOX", "^VIX")
US_FEATURES = ["us_spx_return_1d", "us_nasdaq_return_1d", "us_sox_return_1d",
               "us_vix_level", "us_vix_return_1d", "sox_nasdaq_spread_1d"]
SCHEMA_VERSION = "us_market_observation_v2"
NY = ZoneInfo("America/New_York")
HISTORICAL_MODE = "RETROSPECTIVE_END_OF_SESSION_RECONSTRUCTION"
CALENDAR_SOURCES = ["https://www.nyse.com/trade/hours-calendars",
                    "https://www.nasdaq.com/market-activity/stock-market-holiday-schedule",
                    "https://www.cboe.com/tradable_products/vix/faqs"]


def us_evidence_card(report: dict, *, own_market: bool = False) -> dict:
    return {"group": "us_own_market_price" if own_market else "us_market",
            "economic_hypothesis": "own-index momentum and volatility may summarize short-horizon conditions" if own_market else "completed US equity/semiconductor returns and volatility may describe global risk conditions before Korea opens",
            "data_acquisition": "public daily snapshot with source URL, actual receipt and calendar normalization",
            "actual_publication_timestamp": "unknown; session close plus buffer is an explicit reconstruction assumption",
            "cost": "no paid data acquisition; provider terms remain source-specific",
            "missing_rate": "see eligible rows, expected/selected sessions and explicit exclusion reasons",
            "history_length": report.get("comparison", report.get("eligible_rows")),
            "leakage_risk": "unknown original revisions; global knowledge cutoff, per-row availability and live receipt checks required",
            "baseline_gain": "see aligned OOF metrics and confidence intervals; no gain assumed",
            "regime_performance": "see own candidate year/volatility/trend slices; retrospective only",
            "ablation_result": "fixed candidate groups; same eligible dates; previously viewed history is not prospective proof"}


def _utc(value: Any) -> pd.Timestamp:
    try:
        stamp = pd.Timestamp(value)
        return stamp.tz_convert("UTC") if stamp.tzinfo is not None and not pd.isna(stamp) else pd.NaT
    except (TypeError, ValueError, OverflowError):
        return pd.NaT


@lru_cache(maxsize=16)
def us_close_schedule(start: str, end: str) -> pd.DataFrame:
    """US equity common-session calendar; VIX has a separate closing bound.

    Nasdaq and NYSE share the documented regular/holiday/early-close schedule.
    VIX ordinary-day calculation ends 16:15 ET. An authoritative early-close
    index cutoff is not established here: retain the conservative 16:15 ET
    bound and label it as an assumption rather than inventing 13:15 ET.
    """
    # This dependency currently constructs pandas Timedelta strings during
    # import/calendar initialization. Isolate that known NumPy compatibility
    # warning; other deprecations remain errors in the strict test suite.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="The 'generic' unit for NumPy timedelta is deprecated.*", category=DeprecationWarning)
        from roboquant.index_forecast.market_calendar import exchange_schedule

        calendar, labels = exchange_schedule("XNYS", start, end)
    rows = []
    for label in labels:
        day = label.date().isoformat()
        equity_close = calendar.session_close(label).tz_convert("UTC")
        early = equity_close.tz_convert(NY).hour < 16
        for symbol in US_SYMBOLS:
            close = (pd.Timestamp(datetime.combine(label.date(), time(16, 15), tzinfo=NY)).tz_convert("UTC")
                     if symbol == "^VIX" else equity_close)
            rows.append({"symbol": symbol, "session_date": day, "session_close_at": close.isoformat(),
                         "session_open_at": calendar.session_open(label).isoformat(),
                         "calendar_source": "exchange_calendars:XNYS; Nasdaq shared schedule",
                         "close_policy": "VIX_early_close_conservative_1615_ET_assumption" if symbol == "^VIX" and early
                         else "VIX_regular_calculation_1615_ET" if symbol == "^VIX" else "equity_exchange_close",
                         "timezone": "America/New_York"})
    return pd.DataFrame(rows)


def normalize_us_observations(raw: pd.DataFrame, *, buffer_minutes: int = 15) -> dict:
    """Normalize a newly fetched snapshot; never stamp historical first-seen dates."""
    if buffer_minutes < 0:
        raise ValueError("buffer_minutes must be nonnegative")
    required = {"symbol", "session_date", "close", "source", "source_url", "fetched_at"}
    columns = ["schema_version", "symbol", "session_date", "close", "source", "source_url",
               "session_close_at", "available_at", "first_seen_at", "fetched_at", "revision", "is_final",
               "availability_basis", "original_publication_verified", "calendar_source", "close_policy", "timezone"]
    report = {"schema_version": SCHEMA_VERSION, "historical_mode": HISTORICAL_MODE,
              "original_publication_verified": False, "buffer_minutes": buffer_minutes,
              "excluded_incomplete_bars": 0, "excluded_invalid_rows": 0, "calendar_sources": CALENDAR_SOURCES,
              "vix_early_close_policy": "conservative 16:15 ET bound, not verified early-close publication",
              "note": "close+buffer is a reconstruction assumption; first_seen/fetched timestamps are actual collection times"}
    if raw.empty or not required.issubset(raw):
        return {"observations": pd.DataFrame(columns=columns), "report": {**report, "status": "missing"}}
    data = raw.copy()
    data["session_date"] = pd.to_datetime(data["session_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    data = data.dropna(subset=["session_date"])
    if data.empty:
        return {"observations": pd.DataFrame(columns=columns), "report": {**report, "status": "missing"}}
    schedule = us_close_schedule(data.session_date.min(), data.session_date.max())
    lookup = {(r.symbol, r.session_date): r for r in schedule.itertuples()}
    rows = []
    for row in data.to_dict("records"):
        session = lookup.get((row["symbol"], row["session_date"]))
        fetched = _utc(row["fetched_at"])
        close = pd.to_numeric(row["close"], errors="coerce")
        if session is None or pd.isna(fetched) or not np.isfinite(close) or close <= 0 or not row["source"] or not row["source_url"]:
            report["excluded_invalid_rows"] += 1
            continue
        available = _utc(session.session_close_at) + timedelta(minutes=buffer_minutes)
        if available > fetched:
            report["excluded_incomplete_bars"] += 1
            continue
        rows.append({"schema_version": SCHEMA_VERSION, "symbol": row["symbol"], "session_date": row["session_date"],
                     "close": float(close), "source": str(row["source"]), "source_url": str(row["source_url"]),
                     "session_close_at": session.session_close_at, "available_at": available.isoformat(),
                     "first_seen_at": fetched.isoformat(), "fetched_at": fetched.isoformat(), "revision": 0,
                     "is_final": True, "availability_basis": "session_close_plus_buffer_assumption",
                     "original_publication_verified": False, "calendar_source": session.calendar_source,
                     "close_policy": session.close_policy, "timezone": session.timezone})
    observations = pd.DataFrame(rows, columns=columns).sort_values(["symbol", "session_date"], kind="stable").reset_index(drop=True)
    return {"observations": observations, "report": {**report, "status": "ready" if rows else "missing",
             "rows": len(rows), "fetched_at": sorted({r["fetched_at"] for r in rows})}}


def _version(frame: pd.DataFrame) -> pd.Series | None:
    if frame.empty:
        return None
    for column in ("_available", "_revision", "_seen"):
        frame = frame.loc[frame[column] == frame[column].max()]
    if frame["close"].nunique() != 1:
        return None
    return frame.iloc[0]


def build_us_features(observations: pd.DataFrame, *, asof_cutoffs: dict,
                      live_asof_date: str | None = None, max_stale_sessions: int = 0,
                      buffer_minutes: int = 15, reporting_cutoff: Any = None) -> dict:
    """Join each instrument by UTC availability, never by calendar date shortcut."""
    report = {"status": "missing", "historical_mode": HISTORICAL_MODE,
              "original_publication_verified": False, "max_stale_sessions": max_stale_sessions,
              "buffer_minutes": buffer_minutes, "feature_list_frozen": US_FEATURES,
              "latest_snapshot": {}, "source_rows": len(observations), "eligible_rows": 0,
              "calendar_sources": CALENDAR_SOURCES, "forecast_uplift": "unmeasured",
              "limitations": ["historical provider publication/revision times unknown", "retrospective trial; terminal history previously viewed",
                              "VIX early-close uses conservative 16:15 ET assumption", "no FX/rates features without trusted release clocks"]}
    required = {"schema_version", "symbol", "session_date", "close", "available_at", "session_close_at", "first_seen_at", "revision", "is_final"}
    empty = {"features": pd.DataFrame(columns=["asof_date", *US_FEATURES]), "report": report}
    if observations.empty or not required.issubset(observations) or not asof_cutoffs:
        return empty
    cuts = {str(pd.Timestamp(day).date()): _utc(value) for day, value in asof_cutoffs.items()}
    knowledge_cutoff = (_utc(reporting_cutoff) if reporting_cutoff is not None
                        else max((c for c in cuts.values() if not pd.isna(c)), default=pd.NaT))
    data = observations.copy()
    data["_available"] = pd.to_datetime(data.available_at.map(_utc), utc=True)
    data["_seen"] = pd.to_datetime(data.first_seen_at.map(_utc), utc=True)
    data["_close_at"] = pd.to_datetime(data.session_close_at.map(_utc), utc=True)
    data["_revision"] = pd.to_numeric(data.revision, errors="coerce")
    data["close"] = pd.to_numeric(data.close, errors="coerce")
    data = data.loc[(data.schema_version == SCHEMA_VERSION) & data.is_final.astype(str).str.lower().isin({"true", "1", "yes"}) &
                    data.close.gt(0) & np.isfinite(data.close) & data._revision.ge(0) &
                    data._seen.notna() & (data._seen <= knowledge_cutoff)]
    if data.empty or not any(not pd.isna(c) for c in cuts.values()):
        return empty
    first = pd.Timestamp(data.session_date.min()) - timedelta(days=10)
    last = max(c for c in cuts.values() if not pd.isna(c)).tz_convert(NY).date() + timedelta(days=7)
    schedule = us_close_schedule(first.date().isoformat(), last.isoformat())
    timelines = {symbol: schedule.loc[schedule.symbol == symbol].reset_index(drop=True) for symbol in US_SYMBOLS}
    output, snapshot = [], {}
    for day, cutoff in sorted(cuts.items()):
        values, snapshot = {"asof_date": day}, {}
        for symbol, timeline in timelines.items():
            finished = timeline.loc[pd.to_datetime(timeline.session_close_at, utc=True) <= cutoff]
            expected_index = int(finished.index[-1]) if not finished.empty else -1
            expected = timeline.iloc[expected_index].session_date if expected_index >= 0 else None
            info = {"expected_session": expected, "selected_session": None, "stale_sessions": None, "reasons": []}
            known = data.loc[(data.symbol == symbol) & (data._close_at <= cutoff) & (data._available <= cutoff)]
            if live_asof_date == day:
                if not known.empty and (known._seen > cutoff).any() and not ((known._seen <= cutoff) & (known.session_date == expected)).any():
                    info["reasons"].append("first_seen_after_cutoff")
                known = known.loc[known._seen <= cutoff]
            else:
                # Initial snapshot revision zero is the declared reconstruction;
                # newly observed later corrections cannot masquerade as old data.
                known = known.loc[(known._revision == 0) | (known._seen <= cutoff)]
            selected = None
            for candidate_day in sorted(known.session_date.unique(), reverse=True):
                selected = _version(known.loc[known.session_date == candidate_day])
                if selected is not None:
                    break
            if selected is None or expected_index < 0:
                info["reasons"].append("no_available_completed_us_bar")
            else:
                indexes = timeline.index[timeline.session_date == selected.session_date]
                selected_index = int(indexes[0]) if len(indexes) else -1
                stale = expected_index - selected_index
                info.update(selected_session=selected.session_date, stale_sessions=stale,
                            session_close_at=selected.session_close_at, available_at=selected.available_at,
                            first_seen_at=selected.first_seen_at, source=selected.get("source"),
                            close_policy=selected.get("close_policy"))
                if stale < 0 or stale > max_stale_sessions:
                    info["reasons"].append("missing_expected_us_session")
                previous_day = timeline.iloc[selected_index - 1].session_date if selected_index > 0 else None
                previous = _version(known.loc[known.session_date == previous_day]) if previous_day else None
                if previous is None:
                    info["reasons"].append("missing_previous_us_session")
                elif not info["reasons"]:
                    prefix = {"^GSPC": "us_spx", "^IXIC": "us_nasdaq", "^SOX": "us_sox", "^VIX": "us_vix"}[symbol]
                    values[prefix + "_return_1d"] = float(selected.close / previous.close - 1)
                    if symbol == "^VIX":
                        values["us_vix_level"] = float(selected.close)
            future_today = timeline.loc[(timeline.session_date == cutoff.tz_convert(NY).date().isoformat()) &
                                         (pd.to_datetime(timeline.session_close_at, utc=True) > cutoff)]
            info["current_session_in_progress"] = not future_today.empty
            snapshot[symbol] = info
        if all(column in values for column in US_FEATURES if column != "sox_nasdaq_spread_1d"):
            values["sox_nasdaq_spread_1d"] = values["us_sox_return_1d"] - values["us_nasdaq_return_1d"]
            output.append(values)
    report.update(status="ready" if output and output[-1]["asof_date"] == sorted(cuts)[-1] else "unavailable",
                  latest_snapshot=snapshot, eligible_rows=len(output),
                  reporting_cutoff=knowledge_cutoff.isoformat(),
                  first_seen_policy="local snapshot receipt, not original provider first-seen; historical reconstruction never exceeds global reporting knowledge")
    return {"features": pd.DataFrame(output, columns=["asof_date", *US_FEATURES]), "report": report}
