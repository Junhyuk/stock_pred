"""Pure scoring helpers for the chronological Model B/C evaluation.

Every function takes arrays that are already aligned in time order. Nothing
here fits a model, so nothing here can see a later label by accident.
"""
from __future__ import annotations

import math

import numpy as np

EPS = 1e-6


def brier(p, y) -> np.ndarray:
    """Per-event squared error of an up probability."""
    p = np.asarray(p, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    return (p - y) ** 2


def log_loss(p, y) -> np.ndarray:
    """Per-event log loss with p clipped to [1e-6, 1 - 1e-6]."""
    p = np.clip(np.asarray(p, dtype=np.float64), EPS, 1.0 - EPS)
    y = np.asarray(y, dtype=np.float64)
    return -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))


def hit(p, y) -> np.ndarray:
    """1 when p >= 0.5 and the move was up, or p < 0.5 and it was not."""
    p = np.asarray(p, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    return ((p >= 0.5) == (y >= 0.5)).astype(np.float64)


def ece(p, y, bins: int) -> float:
    """Expected calibration error with equal-width bins on [0, 1]."""
    p = np.asarray(p, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if len(p) == 0:
        return float("nan")
    edges = np.linspace(0.0, 1.0, int(bins) + 1)
    which = np.clip(np.digitize(p, edges[1:-1], right=False), 0, int(bins) - 1)
    total = 0.0
    for b in range(int(bins)):
        mask = which == b
        if mask.any():
            total += mask.sum() * abs(p[mask].mean() - y[mask].mean())
    return float(total / len(p))


def summarize(p, y, bins: int) -> dict:
    p = np.asarray(p, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if len(p) == 0:
        return {"n": 0}
    return {
        "n": int(len(p)),
        "brier": float(brier(p, y).mean()),
        "log_loss": float(log_loss(p, y).mean()),
        "hit": float(hit(p, y).mean()),
        f"ece{bins}": ece(p, y, bins),
        "mean_p": float(p.mean()),
        "sd_p": float(p.std()),
        "up_rate": float(y.mean()),
    }


def block_bootstrap(difference, *, block: int = 10, draws: int = 2000, seed: int = 42) -> dict:
    """Paired moving-block bootstrap of a mean loss difference.

    ``difference`` is candidate loss minus comparator loss, in time order.
    Negative means the candidate is better. The one-sided p tests
    H0: mean >= 0 with the centered bootstrap distribution.
    """
    d = np.asarray(difference, dtype=np.float64)
    n = len(d)
    if n == 0:
        return {"n": 0}
    size = max(1, min(int(block), n))
    rng = np.random.default_rng(seed)
    count = math.ceil(n / size)
    means = np.empty(int(draws))
    for i in range(int(draws)):
        starts = rng.integers(0, n - size + 1, size=count)
        idx = (starts[:, None] + np.arange(size)).ravel()[:n]
        means[i] = d[idx].mean()
    mean = float(d.mean())
    low, high = np.quantile(means, [0.025, 0.975])
    se = float(means.std(ddof=1)) if draws > 1 else float("nan")
    centered = means - mean
    p_one = float((1 + np.sum(centered <= mean)) / (draws + 1))
    return {
        "n": int(n),
        "mean": mean,
        "low": float(low),
        "high": float(high),
        "se": se,
        "mde_80_power": float(2.8 * se),
        "p_one_sided": p_one,
        "block": size,
        "draws": int(draws),
        "seed": int(seed),
    }


def holm(pvalues: dict) -> dict:
    """Holm-Bonferroni adjusted p-values, monotone and capped at 1."""
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(items)
    adjusted = {}
    running = 0.0
    for rank, (key, value) in enumerate(items):
        running = max(running, min(1.0, (m - rank) * float(value)))
        adjusted[key] = running
    return adjusted


def expanding_event_up_rate(y, is_event) -> np.ndarray:
    """Laplace up-rate of earlier events only, for every position.

    Position i uses events at positions < i (their labels matured before the
    issue at i). Returns (k + 1) / (n + 2).
    """
    y = np.asarray(y, dtype=np.float64)
    event = np.asarray(is_event, dtype=bool)
    ups = np.concatenate([[0.0], np.cumsum(np.where(event, y, 0.0))[:-1]])
    counts = np.concatenate([[0.0], np.cumsum(event.astype(np.float64))[:-1]])
    return (ups + 1.0) / (counts + 2.0)


def previous_event_direction(y, is_event) -> np.ndarray:
    """Direction (0/1) of the latest earlier event; 0.5 before the first."""
    y = np.asarray(y, dtype=np.float64)
    event = np.asarray(is_event, dtype=bool)
    out = np.full(len(y), 0.5)
    last = 0.5
    for i in range(len(y)):
        out[i] = last
        if event[i]:
            last = float(y[i])
    return out


def abs_return_weights(returns, rows) -> np.ndarray:
    """|r| weights normalised to mean 1 over the given training rows only."""
    r = np.abs(np.asarray(returns, dtype=np.float64)[rows])
    scale = r.mean()
    if not np.isfinite(scale) or scale <= 0:
        return np.ones(len(r))
    return r / scale


def conditional_expected_return(p, returns, is_event) -> np.ndarray:
    """(2p - 1) * mean |r| of earlier events, a train-only extreme-day return."""
    r = np.abs(np.asarray(returns, dtype=np.float64))
    event = np.asarray(is_event, dtype=bool)
    sums = np.concatenate([[0.0], np.cumsum(np.where(event, r, 0.0))[:-1]])
    counts = np.concatenate([[0.0], np.cumsum(event.astype(np.float64))[:-1]])
    magnitude = np.where(counts > 0, sums / np.maximum(counts, 1.0), np.nan)
    return (2.0 * np.asarray(p, dtype=np.float64) - 1.0) * magnitude
