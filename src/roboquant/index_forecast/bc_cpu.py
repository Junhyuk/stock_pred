"""CPU implementation of the published domestic postclose B and C recipes."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike, NDArray
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from threadpoolctl import threadpool_limits

KST = "Asia/Seoul"
MINIMUM_TRAIN = 252
FOLD_SIZE = 60
RANGE_LEVEL = 0.80
METHODS = {"KOSPI": "hist_gradient", "KOSDAQ": "regime_prior"}


def _aware_time(value: datetime | str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("close receipts and the forecast cutoff must be timezone-aware")
    return stamp


def _session_timing(size, sessions, close_received_at, prediction_cutoff):
    if sessions is None:
        if close_received_at or prediction_cutoff is not None:
            raise ValueError("session dates are required with receipt or cutoff timestamps")
        positions = np.arange(size)
        return positions, positions, np.zeros(size - 1, dtype=int)

    days = pd.DatetimeIndex(sessions).as_unit("ns")
    if (
        len(days) != size
        or days.hasnans
        or days.tz is not None
        or not days.equals(days.normalize())
        or not days.is_monotonic_increasing
        or not days.is_unique
    ):
        raise ValueError("expected one distinct session date per close in chronological order")
    local_days = days.tz_localize(KST)
    scheduled_cutoffs = local_days + np.timedelta64(18, "h")
    cutoffs = scheduled_cutoffs.asi8.copy()
    if prediction_cutoff is not None:
        cutoffs[-1] = _aware_time(prediction_cutoff).value
    available = (local_days + np.timedelta64(15 * 60 + 30, "m")).asi8.copy()
    if close_received_at:
        for position, day in enumerate(local_days):
            receipt = close_received_at.get(day.date().isoformat())
            if receipt is None:
                continue
            stamp = _aware_time(receipt).tz_convert(KST)
            if position == size - 1 and stamp.value > cutoffs[-1]:
                raise ValueError("live anchor receipt is after the forecast cutoff")
            # Same-session receipts refine publication timing. Bulk historical imports
            # keep their reconstructed label timing at the cash close.
            if stamp.date() == day.date():
                available[position] = stamp.value
    return cutoffs, available, days.year.to_numpy()[1:]


def _folds(eligible, available, cutoffs, years) -> Iterator[tuple[NDArray, NDArray]]:
    indices = np.arange(len(eligible))
    first = 0
    while first < len(indices):
        train = indices[eligible & (indices < first) & (available <= cutoffs[first])]
        if len(train) < MINIMUM_TRAIN:
            first += 1
            continue
        last = min(first + FOLD_SIZE, len(indices))
        while years[last - 1] != years[first]:
            last -= 1
        test = indices[first:last][eligible[first:last]]
        if len(test):
            yield train, test
        first = last


def _b_forecast(close, returns, volatility, live_volatility, eligible, validation):
    supported = returns[eligible]
    p_up = float((np.count_nonzero(supported > 0) + 1) / (len(supported) + 2))
    if len(validation) < FOLD_SIZE:
        raise ValueError("Model B needs 60 chronologically held-out errors for its prediction band")
    tail = validation[-120:]
    scaled_errors = np.sort(np.abs(returns[tail]) / np.maximum(volatility[tail], 0.003))
    rank = min(len(tail), math.ceil((len(tail) + 1) * RANGE_LEVEL))
    radius = float(max(live_volatility, 0.003) * scaled_errors[rank - 1])
    return {
        "close": close,
        "p_up": p_up,
        "p_down": 1.0 - p_up,
        "expected_return": 0.0,
        "expected_index": close,
        "interval": {
            "low": (1.0 - radius) * close,
            "high": (1.0 + radius) * close,
            "level": RANGE_LEVEL,
        },
    }


def _histogram_probability(train_features, target, score_features, *, minimum):
    positives = np.count_nonzero(target)
    if len(target) < minimum or min(positives, len(target) - positives) < 8:
        return None
    classifier = HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_iter=80,
        max_leaf_nodes=7,
        min_samples_leaf=12,
        l2_regularization=2.0,
        early_stopping=False,
        random_state=42,
    )
    return classifier.fit(train_features, target).predict_proba(score_features)[:, 1]


def _c_components(train_features, extreme, upside, score_features, method):
    volatility, live_volatility = train_features[:, 2], score_features[:, 2]
    events = np.count_nonzero(extreme)
    occurrence_prior = (events + 1) / (len(extreme) + 2)
    direction_prior = (np.count_nonzero(upside[extreme]) + 1) / (events + 2)
    edges = np.unique(np.quantile(volatility, [1 / 3, 2 / 3]))
    regimes = np.searchsorted(edges, volatility, side="right")
    regime_count = np.bincount(regimes, minlength=len(edges) + 1)
    regime_events = np.bincount(regimes[extreme], minlength=len(edges) + 1)
    regime_rate = (regime_events + 20 * occurrence_prior) / (regime_count + 20)
    q, p = None, None
    if method == "hist_gradient":
        q = _histogram_probability(train_features, extreme, score_features, minimum=16)
        p = _histogram_probability(
            train_features[extreme], upside[extreme], score_features, minimum=30
        )
    if q is None:
        q = regime_rate[np.searchsorted(edges, live_volatility, side="right")]
    if p is None:
        p = np.full(len(score_features), direction_prior)
    return q, p


def _calibrate(raw, labels, predicted):
    positives = np.count_nonzero(labels)
    if len(labels) < 60 or min(positives, len(labels) - positives) < 8:
        return float(predicted[0])

    def logit(values):
        probabilities = np.clip(values, 1e-5, 1 - 1e-5)
        return (np.log(probabilities) - np.log1p(-probabilities)).reshape(-1, 1)

    fit = LogisticRegression(C=1.0, max_iter=300, random_state=42).fit(logit(raw), labels)
    return float(fit.predict_proba(logit(predicted))[0, 1])


def _c_forecast(close, features, returns, live, eligible, folds, method):
    # Float close ratios can land slightly inside the exact ±3% label boundaries.
    threshold = 0.03 - 4 * np.finfo(float).eps
    upside = returns >= threshold
    extreme = upside | (returns <= -threshold)
    with threadpool_limits(limits=1):
        q_live, p_live = _c_components(
            features[eligible], extreme[eligible], upside[eligible], live, method
        )
        q, p = float(q_live[0]), float(p_live[0])
        if method == "hist_gradient":
            validation = np.concatenate([test for _, test in folds])
            q_oof, p_oof = zip(
                *(
                    _c_components(
                        features[train], extreme[train], upside[train], features[test], method
                    )
                    for train, test in folds
                ),
                strict=True,
            )
            events = extreme[validation]
            q = _calibrate(np.concatenate(q_oof), events, q_live)
            p = _calibrate(np.concatenate(p_oof)[events], upside[validation][events], p_live)
    return {
        "close": close,
        "method": method,
        "p_down": q * (1.0 - p),
        "p_normal": 1.0 - q,
        "p_up": q * p,
        "p_extreme": q,
        "p_up_given_extreme": p,
    }


def forecast_market(
    market: str,
    closes: ArrayLike,
    *,
    run_c: bool,
    sessions: Sequence[date | str] | None = None,
    close_received_at: Mapping[str, datetime | str] | None = None,
    prediction_cutoff: datetime | str | None = None,
) -> dict:
    """Fit the selected recipes to ordered cash closes and score the final anchor.

    Preserve gaps as NaN/None so windows that touch them remain ineligible. Supply
    session dates for calendar-year fold boundaries, including a date for each gap.
    Same-session historical receipts can refine reconstructed label availability.
    """
    if market not in METHODS:
        raise ValueError(f"unknown Korean index market {market!r}")
    values = np.asarray(closes, dtype=float)
    if values.ndim != 1 or np.isinf(values).any() or (values <= 0).any():
        raise ValueError("expected a one-dimensional history of positive closes and missing values")
    if len(values) < 21 or not np.isfinite(values[-21:]).all():
        raise ValueError("the forecast anchor needs 21 consecutive closes")

    cutoffs, available, years = _session_timing(
        len(values), sessions, close_received_at, prediction_cutoff
    )
    prices = pd.Series(values)
    daily = prices.pct_change(fill_method=None)
    feature_matrix = np.column_stack(
        (
            daily,
            prices.pct_change(5, fill_method=None),
            daily.rolling(20, min_periods=20).std(),
        )
    )
    # Rows describe an as-of close and its next-session label. The live target is
    # absent, while the final observed close can mature the prior row's label.
    next_returns = daily.to_numpy()[1:]
    matured = np.isfinite(next_returns) & (available[1:] <= cutoffs[-1])
    features = feature_matrix[:-1][matured]
    returns = next_returns[matured]
    eligible = np.isfinite(features).all(axis=1)
    if np.count_nonzero(eligible) < MINIMUM_TRAIN:
        raise ValueError("the forecast needs 252 eligible historical returns")
    folds = list(_folds(eligible, available[1:][matured], cutoffs[:-1][matured], years[matured]))
    validation = np.concatenate([test for _, test in folds]) if folds else np.array([], dtype=int)
    close = float(values[-1])
    model_b = _b_forecast(
        close, returns, features[:, 2], feature_matrix[-1, 2], eligible, validation
    )
    model_c = (
        _c_forecast(close, features, returns, feature_matrix[-1:], eligible, folds, METHODS[market])
        if run_c
        else None
    )
    return {"b": model_b, "c": model_c}
