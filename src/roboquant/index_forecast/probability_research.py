"""Pre-registered p_up research candidates and the premarket US shadow family.

Research only: nothing here changes the incumbent reliable forecast, its
published outputs or ``production_weight`` (always 0). Every candidate reuses
``fit_protocol.make_fit_snapshot`` so anchors, purge, label maturity and
cutoff eligibility are identical to the incumbent; preprocessing is fitted on
each anchor's training rows only. Historical inputs are retrospective
reconstructions whose original publication times are unverified.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import asdict
from typing import Any

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from roboquant.index_forecast.dataset import PRICE_FEATURES
from roboquant.index_forecast.fit_protocol import (
    content_hash,
    make_fit_snapshot,
    restore_fit_snapshot,
    session_anchor,
)
from roboquant.index_forecast.reliable import ReliableSettings, _matrix, _prepare, _timestamp
from roboquant.index_forecast.us_market_inputs import US_FEATURES

OHLC_FEATURES = ["intraday_return_1d", "range_1d", "clv_1d"]
CREDIT_FEATURES = ["credit_log_change_1", "credit_log_change_5", "credit_log_change_20"]
RATE_FEATURES = ["kr3y_change_5_bp", "us10y_change_5_bp", "us1y_change_5_bp"]
REGIME_FEATURES = ["volatility_20d", "return_20d"]
LOGISTIC_C = 0.01
PSEUDO_COUNT = 20.0

# Frozen by reports/accuracy_research_20261121/preregistration.json
# (sha256 42f0d118cf6afa078a3b3150d5bb8f6d3e1d8a0eef50b5220856cfbc2ff89daf).
PREMARKET_US_SHADOW = {
    "id": "C6_pre_price_us_l2",
    "issuance_stage": "premarket_final",
    "cutoff": "08:00 KST on the target XKRX session; US session must close (+15 min) before it",
    "features": [*PRICE_FEATURES, *US_FEATURES],
    "model": {"estimator": "LogisticRegression", "C": LOGISTIC_C, "penalty": "l2", "max_iter": 2000,
              "preprocessing": ["median_imputer", "standard_scaler"], "recalibration": None},
    "target": "P(next XKRX session close-to-close return > 0)",
    "production_weight": 0.0,
}


def shadow_spec_hash(spec: dict | None = None) -> str:
    return content_hash(spec or PREMARKET_US_SHADOW)


# ------------------------------------------------------------------ features

def add_ohlc_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Same-session bar shape; known at that session's close, before 18:00 KST."""
    out = frame.copy()
    o, h, low, c = (pd.to_numeric(out[k], errors="coerce") for k in ("open", "high", "low", "close"))
    valid = (o > 0) & (h > 0) & (low > 0) & (c > 0) & (h >= low)
    out["intraday_return_1d"] = (c / o - 1).where(valid)
    out["range_1d"] = np.log(h / low).where(valid)
    out["clv_1d"] = ((c - low) - (h - c)) / (h - low).where(valid & (h > low))
    return out


def lagged_series_features(days: list[str], series: pd.DataFrame, *, lag_sessions: int,
                           changes: dict[str, tuple[int, str]]) -> pd.DataFrame:
    """A dated observation D becomes usable at the cutoff of the lag-th XKRX session after D.

    ``days`` is the ordered XKRX as-of grid. ``changes`` maps an output column to
    (observations back, 'log' or 'bp'). The lag is a declared reconstruction
    assumption for unknown original publication times, never a verified clock.
    Missing history stays NaN; nothing is filled with zero.
    """
    if lag_sessions < 1:
        raise ValueError("lag_sessions must be >= 1")
    obs = series.dropna(subset=["observation_date", "value"]).sort_values("observation_date", kind="stable")
    obs = obs.drop_duplicates("observation_date", keep="last")
    dates = obs["observation_date"].astype(str).tolist()
    values = obs["value"].astype(float).to_numpy()
    usable_at = [bisect_right(days, day) + lag_sessions - 1 for day in dates]
    rows, pointer = [], -1
    for index, day in enumerate(days):
        while pointer + 1 < len(dates) and usable_at[pointer + 1] <= index:
            pointer += 1
        row: dict[str, Any] = {"asof_date": day}
        for column, (back, kind) in changes.items():
            if pointer - back < 0:
                row[column] = np.nan
            elif kind == "log":
                row[column] = float(np.log(values[pointer] / values[pointer - back]))
            elif kind == "bp":
                row[column] = float((values[pointer] - values[pointer - back]) * 100.0)
            else:
                raise ValueError(f"unsupported change kind: {kind}")
        row["_latest_observation_date"] = dates[pointer] if pointer >= 0 else None
        rows.append(row)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ models

def _fit_logistic(train: pd.DataFrame, features: list[str], C: float = LOGISTIC_C) -> dict:
    y = (train["label_return"].to_numpy(dtype=float) > 0).astype(int)
    if np.unique(y).size < 2:
        raise ValueError("training labels need both classes")
    model = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(),
                          LogisticRegression(C=C, max_iter=2000))
    x = _matrix(train, features)
    model.fit(x, y)
    fitted = model.predict_proba(x)[:, 1]
    return {"kind": "logistic", "model": model, "train_n": len(train),
            "train_brier": float(np.mean((fitted - y) ** 2)),
            "coef": model[-1].coef_.ravel().tolist(), "intercept": float(model[-1].intercept_[0])}


def _fit_regime_cells(train: pd.DataFrame, features: list[str]) -> dict:
    y = train["label_return"].to_numpy(dtype=float) > 0
    vol = pd.to_numeric(train["volatility_20d"], errors="coerce").to_numpy()
    trend = pd.to_numeric(train["return_20d"], errors="coerce").to_numpy()
    threshold = float(np.median(vol))
    overall = float((y.sum() + 1) / (len(y) + 2))
    cells = {}
    for high in (False, True):
        for up in (False, True):
            mask = ((vol > threshold) == high) & ((trend > 0) == up)
            cells[(high, up)] = float((y[mask].sum() + PSEUDO_COUNT * overall) / (mask.sum() + PSEUDO_COUNT))
    return {"kind": "regime_cells", "threshold": threshold, "cells": cells, "overall": overall,
            "train_n": len(train)}


def _predict(fitted: dict, row: pd.DataFrame, features: list[str]) -> float:
    if fitted["kind"] == "logistic":
        return float(fitted["model"].predict_proba(_matrix(row, features))[0, 1])
    vol = float(row["volatility_20d"].iloc[0])
    trend = float(row["return_20d"].iloc[0])
    return fitted["cells"][(vol > fitted["threshold"], trend > 0)]


def walk_candidate(data: pd.DataFrame, grid: pd.DataFrame, features: list[str], kind: str,
                   config: ReliableSettings, *, C: float = LOGISTIC_C) -> tuple[list[dict], list[dict]]:
    """Mirror reliable._walk's anchor schedule and snapshot eligibility exactly.

    ``data`` and ``grid`` are ``reliable._prepare`` outputs (labeled history and
    unlabeled prediction grid). Returns OOF records and per-anchor fit metadata.
    """
    if kind not in {"logistic", "regime_cells"}:
        raise ValueError(f"unsupported candidate kind: {kind}")
    grid_cutoffs = dict(zip(grid["_asof_label"], grid["_cutoff"], strict=True))
    fitter = (lambda train, cols: _fit_logistic(train, cols, C)) if kind == "logistic" else _fit_regime_cells
    fit, last_anchor = None, None
    records, fits = [], []
    for position in range(config.initial_train_days + config.purge_days, len(data)):
        row = data.iloc[[position]]
        anchor = session_anchor(row["_asof_label"].iloc[0], config.calendar_name,
                                config.refit_origin, config.retrain_step_days)
        if anchor is None:
            continue
        if anchor["anchor_asof"] in grid_cutoffs:
            anchor = {**anchor, "prediction_cutoff": grid_cutoffs[anchor["anchor_asof"]].isoformat()}
        else:
            anchor = {**anchor, "grid_cutoff_missing": True}
        if anchor["anchor_asof"] != last_anchor:
            snapshot = make_fit_snapshot(data, features, anchor, asdict(config))
            fit = None
            if snapshot["status"] == "ready":
                train = restore_fit_snapshot(snapshot, features, asdict(config))
                fit = fitter(train, features)
                meta = {"anchor_asof": anchor["anchor_asof"], "fit_cutoff": snapshot["fit_cutoff"],
                        "train_n": snapshot["train_n"], "train_last_asof": snapshot["train_last_asof"],
                        "snapshot_hash": snapshot["snapshot_hash"]}
                meta.update({k: fit[k] for k in ("coef", "intercept", "train_brier", "threshold", "overall") if k in fit})
                if "cells" in fit:
                    meta["cells"] = {f"{'high' if a else 'low'}_vol_{'up' if b else 'down'}_trend": v
                                     for (a, b), v in fit["cells"].items()}
                fits.append(meta)
            last_anchor = anchor["anchor_asof"]
        if fit is None or not np.isfinite(_matrix(row, features)).all():
            continue
        records.append({"asof_date": row["_asof_label"].iloc[0], "p_up": _predict(fit, row, features),
                        "actual_return": float(row["label_return"].iloc[0]), "fit_asof": last_anchor})
    return records, fits


def forecast_premarket_us_shadow(history: pd.DataFrame, latest: pd.DataFrame, *,
                                 settings: ReliableSettings | None = None,
                                 spec: dict | None = None,
                                 prediction_grid: pd.DataFrame | None = None) -> dict:
    """Research-only premarket p_up from the frozen shadow spec; never a production signal.

    ``history`` holds labeled premarket-clock rows (prediction_cutoff = target
    08:00 KST); ``latest`` is the current unlabeled row whose US features were
    joined at that cutoff. The fit uses the scheduled anchor's eligible rows only.
    """
    config = settings or ReliableSettings(issuance_protocol="premarket_v1")
    frozen = spec or PREMARKET_US_SHADOW
    features = list(frozen["features"])
    base = {"status": "not_ready", "shadow_id": frozen["id"], "spec_hash": shadow_spec_hash(frozen),
            "production_weight": 0.0, "role": "research_shadow_not_displayed", "p_up": None, "p_down": None,
            "p_flat": 0.0, "gate_reasons": []}
    if latest.empty or any(c not in latest for c in features) or "prediction_cutoff" not in latest:
        return {**base, "gate_reasons": ["missing_latest_shadow_inputs"]}
    current = latest.iloc[[-1]]
    cutoff = _timestamp(current["prediction_cutoff"].iloc[0], session_close=False)
    if pd.isna(cutoff) or not np.isfinite(_matrix(current, features)).all():
        return {**base, "gate_reasons": ["invalid_cutoff_or_missing_shadow_feature_values"]}
    asof = pd.Timestamp(current["asof_date"].iloc[0]).date().isoformat()
    anchor = session_anchor(asof, config.calendar_name, config.refit_origin, config.retrain_step_days)
    if anchor is None:
        return {**base, "gate_reasons": ["before_refit_protocol_origin"]}
    data = _prepare(history)
    if data.empty:
        return {**base, "gate_reasons": ["missing_labeled_history"]}
    data = data.loc[data["_asof_label"] < asof].reset_index(drop=True)
    # Same anchor clock as the walk: the anchor session's own scheduled cutoff.
    grid = _prepare(prediction_grid if prediction_grid is not None else history, keep_unlabeled=True)
    anchor_rows = grid.loc[grid["_asof_label"] == anchor["anchor_asof"], "_cutoff"] if not grid.empty else pd.Series(dtype=object)
    anchor_cutoff = (cutoff if anchor["anchor_asof"] == asof
                     else anchor_rows.iloc[0] if not anchor_rows.empty else None)
    if anchor_cutoff is None:
        return {**base, **anchor, "gate_reasons": ["anchor_row_missing"]}
    snapshot = make_fit_snapshot(data, features, {**anchor, "prediction_cutoff": pd.Timestamp(anchor_cutoff).isoformat()},
                                 asdict(config))
    if snapshot["status"] != "ready":
        return {**base, "gate_reasons": [snapshot.get("reason", "fit_snapshot_not_ready")]}
    fitted = _fit_logistic(restore_fit_snapshot(snapshot, features, asdict(config)), features,
                           float(frozen["model"]["C"]))
    p_up = _predict(fitted, current, features)
    return {**base, "status": "shadow_research", "asof_date": asof, "prediction_cutoff": cutoff.isoformat(),
            "p_up": p_up, "p_down": 1.0 - p_up, "features": features, "fit_asof": snapshot["anchor_asof"],
            "fit_cutoff": snapshot["fit_cutoff"], "train_n": snapshot["train_n"],
            "train_last_asof": snapshot["train_last_asof"], "fit_snapshot_hash": snapshot["snapshot_hash"],
            "fit_provenance": "retrospective_reconstruction; original publications unverified"}


# ------------------------------------------------------------------ scoring

def ece10(p: np.ndarray, y: np.ndarray) -> float:
    """Expected calibration error with 10 equal-width probability bins."""
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    bins = np.minimum((p * 10).astype(int), 9)
    return float(sum((bins == b).mean() * abs(p[bins == b].mean() - y[bins == b].mean())
                     for b in range(10) if (bins == b).any()))


def reliability(p: np.ndarray, y: np.ndarray) -> list[dict]:
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    bins = np.minimum((p * 10).astype(int), 9)
    return [{"lower": b / 10, "upper": (b + 1) / 10, "n": int((bins == b).sum()),
             "mean_p_up": float(p[bins == b].mean()), "observed_up_rate": float(y[bins == b].mean())}
            for b in range(10) if (bins == b).any()]


def probability_metrics(p: np.ndarray, y: np.ndarray) -> dict:
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    clipped = np.clip(p, 1e-12, 1 - 1e-12)
    return {"n": int(len(p)), "brier": float(np.mean((p - y) ** 2)),
            "log_loss": float(np.mean(-y * np.log(clipped) - (1 - y) * np.log(1 - clipped))),
            "direction_hit": float(np.mean((p >= .5) == (y == 1))), "ece10": ece10(p, y),
            "mean_p": float(p.mean()), "sharpness_std": float(p.std()), "observed_up_rate": float(y.mean())}


def abstain_curve(p: np.ndarray, y: np.ndarray, taus: tuple[float, ...] = (0, .02, .05, .10, .15, .20)) -> list[dict]:
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    out = []
    for tau in taus:
        mask = np.abs(p - .5) >= tau
        out.append({"min_abs_p_minus_half": tau, "coverage": float(mask.mean()), "n": int(mask.sum()),
                    "hit": float(np.mean((p[mask] >= .5) == (y[mask] == 1))) if mask.any() else None})
    return out


def block_bootstrap(difference: np.ndarray, *, samples: int = 2000, block: int = 20, seed: int = 42) -> dict:
    """Same moving-block scheme as reliable._bootstrap, plus a one-sided p-value.

    ``p_one_sided_not_better`` estimates P(mean difference >= 0) for a loss
    difference where negative means the candidate is better.
    """
    difference = np.asarray(difference, dtype=float)
    n = len(difference)
    if n == 0:
        raise ValueError("empty difference")
    block = min(block, n)
    rng = np.random.default_rng(seed)
    means = np.empty(samples)
    for i in range(samples):
        starts = rng.integers(0, n - block + 1, size=math.ceil(n / block))
        indices = (starts[:, None] + np.arange(block)).ravel()[:n]
        means[i] = difference[indices].mean()
    low, high = np.quantile(means, [.025, .975])
    return {"mean": float(difference.mean()), "low": float(low), "high": float(high),
            "p_one_sided_not_better": float((1 + np.sum(means >= 0)) / (samples + 1)),
            "samples": samples, "block_days": block, "random_state": seed, "method": "paired_moving_block"}


def holm(p_values: dict[str, float], alpha: float = .05) -> dict[str, dict]:
    """Holm step-down adjusted p-values and family-wise rejections."""
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    m = len(ordered)
    adjusted, running = {}, 0.0
    for rank, (key, p) in enumerate(ordered):
        running = max(running, min(1.0, (m - rank) * p))
        adjusted[key] = {"p_raw": float(p), "p_holm": float(running), "reject_at_alpha": bool(running < alpha)}
    return adjusted
