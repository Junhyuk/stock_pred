"""Construct predictor rows for the cash indices at their Korean issue cutoffs."""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .bc_morning import BASE_SYMBOLS, us_return_features
from .market_closes import effective_index_eod_frame
from .reliable_inputs import KST, session_schedule, utc_timestamp
from .session_input_store import FLOWS, MARGIN, YIELDS, read_inputs

PARENTS = {"KOSPI200": "KOSPI", "KOSDAQ150": "KOSDAQ"}
US_INPUTS = dict(zip((*BASE_SYMBOLS, "KORU"), ("us_sp500", "us_nasdaq", "us_sox", "us_ewy", "us_koru"), strict=True))


FEATURE_COLUMNS = (
    *US_INPUTS.values(), "fx_usdkrw_return", "credit_" + MARGIN.lower(),
    *("flow_" + name.lower() for name in sorted(FLOWS)),
    *("rate_" + name.lower() for name in sorted(YIELDS)),
)


def issue_window(now):
    current = utc_timestamp(now)
    local = current.tz_convert(KST)
    calendar, _ = session_schedule(local.date() - timedelta(days=21), local.date() + timedelta(days=30))
    # Choose the session the day's 08:00 job intended, even when it starts after the open.
    before = calendar[calendar.close_at <= local.normalize() + np.timedelta64(8, "h")]
    anchor = before.iloc[-1]
    target = calendar[calendar.date > anchor.date].iloc[0]
    return {
        "asof_date": anchor.date, "target_date": target.date, "open_at": target.open_at,
        "issue_kind": "final" if target.date == str(local.date()) else "preliminary",
        "allowed": local.weekday() != 6 and local.hour >= 8 and current < target.open_at,
    }


def _as_known(observations, cuts, dates, *, max_age_days):
    """Replay receipt order. A late backfill of an older date must not displace a newer observation."""
    out = np.full(len(cuts), np.nan)
    if observations.empty:
        return out
    rows = observations.copy()
    received = pd.to_datetime(rows.received_at, utc=True, errors="coerce", format="mixed")
    published = pd.to_datetime(rows.published_at, utc=True, errors="coerce", format="mixed")
    rows["known"] = received.where(published.isna() | received.ge(published), published)
    rows = rows.loc[received.notna()].sort_values(["known", "observation_date"], kind="stable")
    events = list(rows.itertuples())
    pos, latest, value = 0, None, np.nan
    for i, (cut, day) in enumerate(zip(cuts, dates, strict=True)):
        while pos < len(events) and events[pos].known <= cut:
            row = events[pos]
            observed = pd.Timestamp(row.observation_date)
            if observed <= day and (latest is None or observed >= latest):
                latest, value = observed, row.value
            pos += 1
        if latest is not None and (day - latest).days <= max_age_days:
            out[i] = value
    return out


def context_features(dates, *, target_date, cutoff, broad_db=None, inputs_db=None, us_csv=None):
    """Full exchange-session frame; the last row uses the actual receipt cutoff, including Saturdays."""
    dates = pd.DatetimeIndex(dates)
    result = pd.DataFrame(index=dates)
    target_days = dates[1:].append(pd.DatetimeIndex([target_date]))
    cuts = list((target_days.tz_localize(KST) + np.timedelta64(8, "h")).tz_convert("UTC"))
    cuts[-1] = utc_timestamp(cutoff)
    cuts = pd.DatetimeIndex(cuts)
    if us_csv and Path(us_csv).is_file():
        obs = pd.read_csv(us_csv)
        values = us_return_features(obs, cuts, tuple(US_INPUTS))
        for j, name in enumerate(US_INPUTS.values()):
            result[name] = values[:, j]
    if broad_db and Path(broad_db).is_file():
        with sqlite3.connect(Path(broad_db).resolve().as_uri() + "?mode=ro", uri=True) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "macro_series" in tables:
                fx = pd.read_sql_query("""SELECT observation_date, value, published_at, ingested_at AS received_at
                    FROM macro_series WHERE series_code='YAHOO_USDKRW' ORDER BY revision_no""", conn)
                series = pd.Series(_as_known(fx, cuts, dates, max_age_days=7), index=dates)
                result["fx_usdkrw_return"] = series.pct_change(fill_method=None)
        if "index_eod" in tables:
            prices = effective_index_eod_frame(broad_db, cutoff=cutoff)
            # Reconstruct original closes, never applying a later correction to an earlier predictor row.
            available = pd.to_datetime(prices.ingested_at, utc=True, errors="coerce", format="mixed")
            prices = prices.loc[available.le(cuts[-1])]
            for market in PARENTS.values():
                panel = prices.loc[prices.index_code.eq(market)].copy()
                panel.index = pd.to_datetime(panel.session_date)
                result[market] = pd.to_numeric(panel.close, errors="coerce").reindex(dates)
    if inputs_db and Path(inputs_db).is_file():
        rows = read_inputs(Path(inputs_db), as_of=cutoff)
        rows = rows[rows.is_final.ne(False)]
        for (market, series), group in rows.groupby(["market", "series"]):
            if series in YIELDS:
                key = "rate_" + series.lower()
            elif market in PARENTS.values() and series in FLOWS | {MARGIN}:
                key = market + ("_credit_" if series == MARGIN else "_flow_") + series.lower()
            else:
                continue
            result[key] = _as_known(group, cuts, dates, max_age_days=7 if series in YIELDS | {MARGIN} else 0)
    return result


def add_context(target, context, market, live_moves):
    frame = target[["close"]].reindex(context.index).join(context)
    parent = PARENTS[market]
    # Nullable trigger: a single observed extreme is sufficient, but both moves are needed to rule it out.
    parents = frame.reindex(columns=list(PARENTS.values()))
    cents = (parents * 100).round()
    extreme = (cents - cents.shift()).abs() * 100 >= cents.shift() * 3
    complete = (parents.notna() & parents.shift().notna()).all(axis=1)
    triggered = extreme.any(axis=1)
    frame["broad_trigger"] = triggered.astype("boolean").where(complete | triggered)
    live = trigger_value(live_moves)
    frame.loc[frame.index[-1], "broad_trigger"] = pd.NA if np.isnan(live) else bool(live)
    frame["parent_close"] = parents[parent]
    for name in list(frame):
        if any(name.startswith(m + "_") for m in PARENTS.values()):
            if name.startswith(parent + "_"):
                frame[name.removeprefix(parent + "_")] = frame[name]
            del frame[name]
    frame = frame.drop(columns=list(PARENTS.values()), errors="ignore")
    return frame.reindex(columns=["close", "parent_close", "broad_trigger", *FEATURE_COLUMNS])


def trigger_value(moves):
    if any(abs(x) >= 0.03 for x in moves.values()):
        return 1.0
    return 0.0 if set(moves) == set(PARENTS.values()) else np.nan
