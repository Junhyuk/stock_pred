"""Frozen premarket Model B Ridge recipe for the local CPU runner."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike, NDArray
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.model_selection import GridSearchCV, TimeSeriesSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from roboquant.index_forecast.bc_cpu import (
    KST,
    MINIMUM_TRAIN,
    RANGE_LEVEL,
    _aware_time,
    _calibrate,
    _folds,
)
from roboquant.index_forecast.reliable_inputs import session_schedule
from roboquant.index_forecast.us_market_inputs import (
    NY,
    SCHEMA_VERSION,
    us_close_schedule,
)

BASE_SYMBOLS = ("^GSPC", "^IXIC", "^SOX", "EWY")
MARKET_SYMBOLS = {"KOSPI": (*BASE_SYMBOLS, "KORU"), "KOSDAQ": BASE_SYMBOLS}
EXPECTED_COLUMNS = {
    "schema_version",
    "symbol",
    "session_date",
    "close",
    "session_close_at",
    "available_at",
    "first_seen_at",
    "revision",
    "is_final",
}
BUFFER = np.timedelta64(15, "m")


def _bar_version(rows, *, cutoff, historical):
    first_seen = min((row[2] for row in rows if row[1] == 0), default=None)
    eligible = [
        (available, revision, seen, close)
        for available, revision, seen, close in rows
        if available <= cutoff
        and (
            (revision == 0 and seen == first_seen and (historical or seen <= cutoff))
            or (revision > 0 and seen <= cutoff)
        )
    ]
    if not eligible:
        return np.nan
    identity = max(row[:3] for row in eligible)
    prices = {row[3] for row in eligible if row[:3] == identity}
    return float(prices.pop()) if len(prices) == 1 else np.nan


def us_return_features(
    observations: pd.DataFrame, cutoffs: pd.DatetimeIndex, symbols: Sequence[str]
) -> NDArray:
    """Return scheduled last/previous US-session moves at each KRX cutoff.

    Revision zero may reconstruct a historical session, but only from data already
    seen at the live knowledge cutoff. Revisions and the two live bars require
    actual receipt no later than the row's cutoff.
    """
    if (
        cutoffs.empty
        or cutoffs.hasnans
        or cutoffs.tz is None
        or not cutoffs.is_monotonic_increasing
        or not cutoffs.is_unique
    ):
        raise ValueError("expected ordered timezone-aware US feature cutoffs")
    features = np.full((len(cutoffs), len(symbols)), np.nan)
    if observations.empty or not EXPECTED_COLUMNS.issubset(observations):
        return features

    first = cutoffs[0].tz_convert(NY).date() - timedelta(days=14)
    last = cutoffs[-1].tz_convert(NY).date()
    calendar = us_close_schedule(first.isoformat(), last.isoformat())
    calendar = calendar.loc[calendar.symbol.eq("^GSPC"), ["session_date", "session_close_at"]]
    release = pd.DatetimeIndex(pd.to_datetime(calendar.session_close_at, utc=True)) + BUFFER
    expected = release.searchsorted(cutoffs, side="right") - 1
    release_by_date = pd.Series(release, index=calendar.session_date)

    data = observations.loc[observations.symbol.isin(symbols)].copy()
    clock_columns = ("session_close_at", "available_at", "first_seen_at")
    for name in clock_columns:
        values = data[name].astype("string")
        zoned = values.str.contains(r"(?:Z|[+-]\d\d:\d\d)$", regex=True, na=False)
        data[name] = pd.to_datetime(values.where(zoned), utc=True, format="mixed", errors="coerce")
    data["revision"] = pd.to_numeric(data.revision, errors="coerce")
    data["close"] = pd.to_numeric(data.close, errors="coerce")
    official_release = data.session_date.map(release_by_date)
    valid = (
        data.schema_version.eq(SCHEMA_VERSION)
        & data.is_final.astype(str).str.lower().isin({"true", "1", "yes"})
        & np.isfinite(data.close)
        & data.close.gt(0)
        & np.isfinite(data.revision)
        & data.revision.ge(0)
        & data.revision.mod(1).eq(0)
        & data.session_close_at.eq(official_release - BUFFER)
        & data.available_at.ge(official_release)
        & data.first_seen_at.ge(data.available_at)
        & data.first_seen_at.le(cutoffs[-1])
    )
    versions = {
        key: tuple(rows.itertuples(index=False, name=None))
        for key, rows in data.loc[valid].groupby(["symbol", "session_date"])[
            ["available_at", "revision", "first_seen_at", "close"]
        ]
    }
    dates = calendar.session_date.to_numpy()
    for i, (cutoff, position) in enumerate(zip(cutoffs, expected, strict=True)):
        if position < 1:
            continue
        historical = i < len(cutoffs) - 1
        for j, symbol in enumerate(symbols):
            current, previous = (
                _bar_version(
                    versions.get((symbol, dates[k]), ()), cutoff=cutoff, historical=historical
                )
                for k in (position, position - 1)
            )
            if np.isfinite(current) and np.isfinite(previous):
                change = current / previous
                # Multi-fold jumps are inconsistent with the split-continuous KORU data
                # used by this recipe. Its collector also verifies reported split events.
                if symbol != "KORU" or 0.5 <= change <= 1.5:
                    features[i, j] = change - 1
    return features


def _domestic_features(
    closes: ArrayLike,
    sessions: pd.DataFrame,
    target: str,
    close_received_at: Mapping[str, datetime | str],
    cutoff: pd.Timestamp,
) -> tuple[NDArray, NDArray, NDArray, pd.DatetimeIndex, NDArray]:
    prices = pd.Series(np.asarray(closes, dtype=float))
    if (
        len(sessions) != len(prices)
        or len(prices) < 21
        or np.isinf(prices).any()
        or prices.le(0).any()
        or not np.isfinite(prices.iloc[-21:]).all()
    ):
        raise ValueError("expected positive KRX closes with 21 consecutive anchor bars")

    anchor = sessions.date.iloc[-1]
    days = pd.DatetimeIndex([*sessions.date, target])
    if (
        days.tz is not None
        or days.hasnans
        or not days.is_unique
        or not days.is_monotonic_increasing
        or not days.equals(days.normalize())
    ):
        raise ValueError("expected distinct ordered KRX dates through the anchor and target")
    upcoming, _ = session_schedule(anchor, target)
    if upcoming.date.tolist() != [anchor, target]:
        raise ValueError("the morning target must be the next scheduled KRX session")
    if pd.isna(upcoming.open_at.iloc[-1]) or cutoff >= upcoming.open_at.iloc[-1]:
        raise ValueError("the morning cutoff must precede the target open")
    cash_closes = (
        pd.DatetimeIndex([_aware_time(x) for x in sessions.close_at])
        .tz_convert("UTC")
        .as_unit("ns")
    )
    if (
        not cash_closes.is_monotonic_increasing
        or not cash_closes.is_unique
        or cash_closes.tz_convert(KST).strftime("%Y-%m-%d").tolist()
        != days[:-1].strftime("%Y-%m-%d").tolist()
    ):
        raise ValueError("KRX cash-close schedule does not match the ordered sessions")
    anchor_received = close_received_at.get(anchor)
    if anchor_received is None or not cash_closes[-1] <= _aware_time(anchor_received) <= cutoff:
        raise ValueError("live anchor receipt must follow the anchor close and precede the cutoff")
    morning = days[1:].tz_localize(KST) + np.timedelta64(8, "h")
    cutoffs = morning[:-1].append(pd.DatetimeIndex([cutoff.tz_convert(KST)])).as_unit("ns")

    available = cash_closes.asi8.copy()
    for position, day in enumerate(sessions.date):
        receipt = close_received_at.get(day)
        if receipt is None:
            continue
        stamp = _aware_time(receipt).tz_convert(KST)
        if stamp.date().isoformat() == day and cash_closes[position] <= stamp <= cutoff:
            available[position] = stamp.value
    available[-1] = _aware_time(anchor_received).value
    daily = prices.pct_change(fill_method=None)
    features = np.column_stack(
        (
            daily,
            prices.pct_change(5, fill_method=None),
            daily.rolling(20, min_periods=20).std(),
        )
    )
    return features, daily.to_numpy()[1:], available[1:], cutoffs, days[1:].year.to_numpy()


def _center(train_x, y, test_x):
    solver = GridSearchCV(
        make_pipeline(StandardScaler(), Ridge()),
        {"ridge__alpha": (0.1, 1.0, 10.0, 100.0, 1000.0)},
        cv=TimeSeriesSplit(n_splits=3, gap=1),
        scoring="neg_mean_absolute_error",
    )
    return solver.fit(train_x, y * 100).predict(test_x) / 100


def _direction(train_x, y, test_x):
    up = y > 0
    if up.all() or not up.any():
        return np.full(len(test_x), (up.sum() + 1) / (len(up) + 2))
    classifier = make_pipeline(
        StandardScaler(), LogisticRegression(C=0.01, max_iter=300, random_state=42)
    )
    return classifier.fit(train_x, up).predict_proba(test_x)[:, 1]


def _out_of_fold(x, y, eligible, availability, cutoffs, years, method):
    held_out = np.full(len(y), np.nan)
    for train, test in _folds(eligible, availability, cutoffs, years):
        held_out[test] = method(x[train], y[train], x[test])
    return held_out


def forecast_morning_market(
    market: str,
    closes: ArrayLike,
    *,
    sessions: pd.DataFrame,
    target: str,
    observations: pd.DataFrame,
    prediction_cutoff: datetime | str,
    close_received_at: Mapping[str, datetime | str] | None = None,
) -> dict:
    """Fit and score the prior research B price and direction heads at a live cutoff.

    The KRX schedule includes the anchor and preceding sessions. Historical
    training uses scheduled label times unless a same-session receipt is known.
    """
    if market not in MARKET_SYMBOLS:
        raise ValueError(f"unknown Korean index market {market!r}")
    cutoff = _aware_time(prediction_cutoff)
    domestic, returns, availability, cutoffs, years = _domestic_features(
        closes, sessions, target, close_received_at or {}, cutoff
    )
    symbols = MARKET_SYMBOLS[market]
    us = us_return_features(observations, cutoffs, symbols)
    if not np.isfinite(us[-1]).all():
        missing = ", ".join(
            name for name, value in zip(symbols, us[-1], strict=True) if not np.isfinite(value)
        )
        raise ValueError(f"missing cutoff-observed US session returns: {missing}")

    features = np.column_stack((domestic, us))
    direction = features[:, : len(BASE_SYMBOLS) + domestic.shape[1]]
    known = np.isfinite(returns) & (availability <= cutoff.value)
    eligible_close = known & np.isfinite(features[:-1]).all(axis=1)
    eligible_direction = known & np.isfinite(direction[:-1]).all(axis=1)
    if min(eligible_close.sum(), eligible_direction.sum()) < MINIMUM_TRAIN:
        raise ValueError(
            "the morning forecast needs 252 eligible historical returns for both heads"
        )

    with threadpool_limits(limits=1):
        held_close = _out_of_fold(
            features, returns, eligible_close, availability, cutoffs.asi8[:-1], years[:-1], _center
        )
        held_direction = _out_of_fold(
            direction,
            returns,
            eligible_direction,
            availability,
            cutoffs.asi8[:-1],
            years[:-1],
            _direction,
        )
        tail = np.flatnonzero(np.isfinite(held_close))[-120:]
        calibrated_rows = np.flatnonzero(np.isfinite(held_direction))
        if min(len(tail), len(calibrated_rows)) < 60:
            raise ValueError(
                "the morning forecast needs 60 chronologically held-out residuals and probabilities"
            )
        center = float(
            _center(features[:-1][eligible_close], returns[eligible_close], features[-1:])[0]
        )
        raw_up = _direction(
            direction[:-1][eligible_direction], returns[eligible_direction], direction[-1:]
        )
        p_up = _calibrate(held_direction[calibrated_rows], returns[calibrated_rows] > 0, raw_up)

    normalized = np.abs(returns[tail] - held_close[tail]) / np.maximum(domestic[tail, 2], 0.003)
    rank = min(len(tail), math.ceil((len(tail) + 1) * RANGE_LEVEL))
    radius = float(max(domestic[-1, 2], 0.003) * np.sort(normalized)[rank - 1])
    price = float(np.asarray(closes)[-1])
    low, expected, high = (price * (1 + r) for r in (center - radius, center, center + radius))
    if not all(np.isfinite([low, expected, high])) or not 0 < low <= expected <= high:
        raise ValueError("the morning forecast produced an invalid index range")
    return {
        "close": price,
        "p_up": p_up,
        "p_down": 1.0 - p_up,
        "expected_return": center,
        "expected_index": expected,
        "interval": {"low": low, "high": high, "level": RANGE_LEVEL},
    }
