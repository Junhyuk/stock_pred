"""Next-close math for the Model B return head.

The head predicts the next close over this close, minus one. A session with no
next close is not a label. The quoted index is this close times one plus that
return.
"""
from __future__ import annotations

from typing import Any

import numpy as np


def expected_index(close: float, expected_return: float) -> float:
    """Index implied by a simple next-session return."""
    return float(close) * (1.0 + float(expected_return))


def next_close_returns(closes) -> np.ndarray:
    """Return next_close / close - 1. The last session is left unlabeled."""
    values = np.asarray(closes, dtype=np.float64)
    returns = np.full(values.shape, np.nan, dtype=np.float64)
    if len(values) >= 2:
        returns[:-1] = values[1:] / values[:-1] - 1.0
    return returns


def period_close_mae(
    closes,
    predicted_returns,
    actual_returns,
    dates,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Mean absolute next-close error on walk-forward sessions inside a date span.

    ``end`` is exclusive. Sessions without a finite prediction are not scored.
    """
    close = np.asarray(closes, dtype=np.float64)
    predicted = np.asarray(predicted_returns, dtype=np.float64)
    actual = np.asarray(actual_returns, dtype=np.float64)
    if not len(close) == len(predicted) == len(actual) == len(dates):
        raise ValueError("close, return, and date sequences must be aligned")
    error = np.abs(close * (1.0 + predicted) - close * (1.0 + actual))
    mask = np.isfinite(error)
    if start is not None:
        mask &= np.fromiter((day >= start for day in dates), dtype=bool, count=len(dates))
    if end is not None:
        mask &= np.fromiter((day < end for day in dates), dtype=bool, count=len(dates))
    if not mask.any():
        raise ValueError("no walk-forward sessions in the requested period")
    chosen = [day for day, keep in zip(dates, mask) if keep]
    return {
        "n": int(mask.sum()),
        "close_mae": float(error[mask].mean()),
        "start": chosen[0],
        "end": chosen[-1],
    }
