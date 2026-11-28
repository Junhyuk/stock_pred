"""Bounded, target-specific CPU forecasts with an immutable terminal experiment.

Input rows are chronological *as-of sessions*, including the live anchor. A label
at t is the direct target-index close[t+1]/close[t]-1. ``broad_trigger[t]`` is the
issuance condition observed at t, never the target's future extreme label. All
source receipt/PIT filtering belongs to the caller; historical reconstructions
remain reconstructions. Missing target prices are never replaced by parent data.

Persist ``evaluation_cache`` privately. Once made, its development selection and
terminal period/metrics never move. Appended labels only extend calibration and
fit the frozen recipes. A changed historical prefix/config/feature contract fails
closed: starting a different experiment requires explicitly omitting the cache.
Neither historical scores nor this module authorize production promotion.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

ALGORITHM_VERSION = "target_abc_cpu_v1"
SCHEMA_VERSION = "benchmark_models_v1"
THRESHOLD = 0.03
EXTREME_BOUNDARY = THRESHOLD - 4 * np.finfo(float).eps
PRICE_FEATURES = ["return_1d", "return_5d", "return_20d", "volatility_5d",
                  "volatility_20d", "ma_gap_5d", "ma_gap_20d"]
SUPPORTED_TARGETS = {"KOSPI", "KOSDAQ", "KOSPI200", "KOSDAQ150"}


@dataclass(frozen=True)
class BenchmarkSettings:
    initial_train: int = 504
    calibration_rows: int = 60
    development_rows: int = 252
    terminal_rows: int = 252
    fold_size: int = 60
    purge_rows: int = 1
    min_trigger_rows: int = 40
    bootstrap_samples: int = 1000
    bootstrap_block: int = 20
    calibration_window: int = 252
    random_state: int = 42

    def __post_init__(self):
        if any(value < 1 for key, value in asdict(self).items()
               if key not in {"purge_rows", "random_state"}):
            raise ValueError("model windows and budgets must be positive")
        if self.purge_rows < 1:
            raise ValueError("at least one purged observation is required")
        if self.fold_size > 252 or self.bootstrap_samples > 5000:
            raise ValueError("unbounded evaluation settings")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_safe(v) for v in value]
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


def _unavailable(reason, *, code, target, cutoff, asof=None, cache=None):
    return {"schema_version": SCHEMA_VERSION, "model_version": ALGORITHM_VERSION,
            "status": "not_ready", "reason": reason, "index_code": code,
            "asof_date": asof, "target_date": target, "cutoff": str(cutoff),
            "cache_reused": False, "evaluation_cache": cache,
            "models": {name: {"status": "not_ready", "reason": reason,
                "model_version": ALGORITHM_VERSION, "p_up": None, "p_down": None,
                "expected_index": None, "expected_return": None, "interval": None,
                "production_weight": 0.0} for name in "ABC"}}


def _prepare(frame: pd.DataFrame, extra: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("expected a chronological DatetimeIndex of sessions")
    days = frame.index
    if (days.hasnans or days.tz is not None or not days.is_monotonic_increasing
            or not days.is_unique or not days.equals(days.normalize())):
        raise ValueError("expected distinct chronological naive session dates")
    if "close" not in frame:
        raise ValueError("direct target close is required")
    data = pd.DataFrame(index=days)
    data["close"] = pd.to_numeric(frame.close, errors="coerce")
    if np.isinf(data.close).any() or data.close.le(0).any():
        raise ValueError("target closes must be positive finite values or missing")
    close = data.close
    daily = close.pct_change(fill_method=None)
    data["return_1d"] = daily
    for length in (5, 20):
        data[f"return_{length}d"] = close.pct_change(length, fill_method=None)
        data[f"volatility_{length}d"] = daily.rolling(length, min_periods=length).std()
        data[f"ma_gap_{length}d"] = close / close.rolling(length, min_periods=length).mean() - 1
    if "parent_return" in frame:
        data["parent_return"] = pd.to_numeric(frame.parent_return, errors="coerce")
    elif "parent_close" in frame:
        parent = pd.to_numeric(frame.parent_close, errors="coerce")
        data["parent_return"] = parent.pct_change(fill_method=None)
    if "parent_return" in data:
        data["parent_relative_return"] = daily - data.parent_return
    for name in extra:
        data[name] = pd.to_numeric(frame[name], errors="coerce") if name in frame else np.nan
    data["label_return"] = close.shift(-1) / close - 1
    data["label_target_date"] = pd.Series(days, index=days).shift(-1)
    # Nullable trigger observations remain unknown. Strings such as 'False'
    # must not accidentally become True via Python truthiness.
    trigger = frame.get("broad_trigger", pd.Series(index=days, dtype=object))
    data["broad_trigger"] = trigger.map(
        lambda v: bool(v) if isinstance(v, (bool, np.bool_)) else None)
    data["trend_regime"] = np.where(data.ma_gap_20d > 0, "positive", "nonpositive")
    median = data.volatility_20d.expanding(min_periods=20).median().shift(1)
    data["volatility_regime"] = np.where(data.volatility_20d > median, "high", "low")
    good = np.isfinite(data[[*PRICE_FEATURES, "label_return"]]).all(axis=1)
    return data, data.loc[good].copy()


def _prefix_hash(frame: pd.DataFrame, columns: list[str], end: str) -> str:
    prefix = frame.loc[:end].reindex(columns=columns)
    rows = [[day.date().isoformat(), *[_safe(None if pd.isna(v) else v) for v in values]]
            for day, values in zip(prefix.index, prefix.itertuples(index=False, name=None), strict=True)]
    return _hash({"columns": columns, "rows": rows})


def _sealed_hash(frame, cache):
    # The final observed close is the last sealed label, but its *feature row*
    # is still live. Morning US updates to that row must not reopen the test.
    return _hash({"prices": _prefix_hash(frame.loc[cache["price_prefix_start"]:], ["close"], cache["price_prefix_end"]),
                  "features": _prefix_hash(frame.loc[cache["feature_prefix_start"]:], cache["fingerprint_columns"], cache["prefix_end"])})


def _extend_calibration(history, spec, prior, pending, start, cfg):
    """Mature the actual pending prediction before any reconstructed new rows."""
    if pending and pending["date"] in history.index.strftime("%Y-%m-%d"):
        position = int(history.index.get_loc(pd.Timestamp(pending["date"])))
        if position >= start:
            prior = _walk(history, spec, start, position, cfg, prior=prior)
            observation = copy.deepcopy(pending)
            observation["actual_return"] = float(history.iloc[position].label_return)
            observation["calibration_evidence"] = "issued_live_inputs"
            prior.append(observation)
            start = position + 1
    return _walk(history, spec, start, len(history), cfg, prior=prior)


def _candidate_sets(data: pd.DataFrame, dev_end: int, extra: list[str], cfg):
    parent = [c for c in ("parent_return", "parent_relative_return") if c in data]
    usable = [c for c in [*parent, *extra]
              if np.isfinite(data[c].iloc[:dev_end]).sum() >= cfg.initial_train]
    parent = [c for c in parent if c in usable]
    external = [c for c in extra if c in usable]
    base = {"kind": "prior", "features": list(PRICE_FEATURES), "group": "price", "id": "expanding_prior"}
    price = {"kind": "logit", "c": 0.1, "features": list(PRICE_FEATURES), "group": "price", "id": "price_logit"}
    tree = {"kind": "lightgbm", "features": list(PRICE_FEATURES), "group": "price", "id": "price_lightgbm"}
    ensemble = {**tree, "kind": "ensemble", "id": "price_ensemble"}
    a = [base, price, tree, ensemble]
    if parent:
        a.append({**price, "features": [*PRICE_FEATURES, *parent], "group": "price_parent", "id": "parent_logit"})
    b = copy.deepcopy(a)
    full = [*PRICE_FEATURES, *parent, *external]
    if external:
        b.extend([{**price, "features": full, "group": "all_available", "id": "external_logit"},
                  {**tree, "kind": "hist", "features": full, "group": "all_available", "id": "external_hist"}])
        # At most five source-family ablations, registered before terminal data.
        families = sorted({c.split("_", 1)[0] for c in external})[:5]
        for family in families:
            columns = [c for c in full if c not in external or not c.startswith(family + "_")]
            if columns != full and columns != PRICE_FEATURES:
                b.append({**price, "features": columns, "group": "without_" + family,
                          "id": "without_" + family + "_logit"})
    c = [{**base, "kind": "c_prior", "id": "three_class_prior"},
         {**base, "kind": "c_regime", "id": "volatility_class_prior"},
         {**price, "kind": "c_logit", "features": full, "group": "all_available", "id": "three_class_logit"}]
    return {"A": a, "B": b, "C": c}, {"requested": extra, "eligible": usable,
        "excluded": [name for name in [*parent, *extra] if name not in usable],
        "availability_decided_before_terminal": True,
        "development_missing_rate": {name: float(data[name].iloc[:dev_end].isna().mean()) for name in extra}}


def _classifier(x, labels, live, *, kind, strength=0.1):
    positives = np.count_nonzero(labels)
    if min(positives, len(labels) - positives) < 8:
        return np.full(len(live), (positives + 1) / (len(labels) + 2))
    if kind == "lightgbm":
        from lightgbm import LGBMClassifier

        estimator = LGBMClassifier(**_tree_parameters())
    elif kind == "hist":
        estimator = HistGradientBoostingClassifier(max_iter=40, max_leaf_nodes=7,
            min_samples_leaf=20, l2_regularization=10, learning_rate=0.05,
            early_stopping=False, random_state=42)
    else:
        estimator = make_pipeline(StandardScaler(), LogisticRegression(
            C=strength, max_iter=250, random_state=42))
    return estimator.fit(x, labels).predict_proba(live)[:, 1]


def _tree_parameters():
    return {"n_estimators": 80, "num_leaves": 7, "min_child_samples": 40,
            "learning_rate": 0.05, "reg_lambda": 10.0, "colsample_bytree": 0.7,
            "n_jobs": 1, "random_state": 42, "verbosity": -1}


def _regressor(x, labels, live, kind):
    if kind == "lightgbm":
        from lightgbm import LGBMRegressor

        return LGBMRegressor(**_tree_parameters()).fit(x, labels * 100).predict(live) / 100
    return make_pipeline(StandardScaler(), Ridge(alpha=10.0)).fit(x, labels * 100).predict(live) / 100


def _fit_predict(train: pd.DataFrame, rows: pd.DataFrame, spec: dict) -> dict:
    names, kind = spec["features"], spec["kind"]
    complete = np.isfinite(train[names]).all(axis=1)
    training = train.loc[complete]
    if len(training) < 20:
        raise ValueError("insufficient complete candidate training rows")
    x, live = training[names].to_numpy(float), rows[names].to_numpy(float)
    valid = np.isfinite(live).all(axis=1)
    # Invalid historical candidate inputs are explicitly unscored. They are not
    # forward-filled and never alter the registered terminal dates.
    safe_live = np.where(np.isfinite(live), live, 0.0)
    r = training.label_return.to_numpy(float)
    up = r > 0
    rate = (up.sum() + 1) / (len(r) + 2)
    result = {"valid": valid, "train_n": len(training),
              "baseline_p": np.full(len(rows), rate), "train_returns": r}
    if kind.startswith("c_"):
        extreme = np.abs(r) >= EXTREME_BOUNDARY
        normal = ~extreme
        q = np.full(len(rows), (extreme.sum() + 1) / (len(r) + 2))
        p = np.full(len(rows), (np.count_nonzero(up & extreme) + 1) / (extreme.sum() + 2))
        if kind == "c_regime":
            occurrence_prior = float(q[0])
            cuts = np.unique(np.quantile(training.volatility_20d, [1 / 3, 2 / 3]))
            bins = np.searchsorted(cuts, training.volatility_20d, side="right")
            live_bins = np.searchsorted(cuts, rows.volatility_20d, side="right")
            for bucket in range(len(cuts) + 1):
                chosen = bins == bucket
                q[live_bins == bucket] = (np.count_nonzero(extreme & chosen) + 20 * occurrence_prior) / (chosen.sum() + 20)
        elif kind == "c_logit":
            q = _classifier(x, extreme, safe_live, kind="logit", strength=0.1)
            if extreme.sum() >= 30:
                p = _classifier(x[extreme], up[extreme], safe_live, kind="logit", strength=0.1)
        normal_up = (np.count_nonzero(up & normal) + 1) / (normal.sum() + 2)
        means = [float(r[r <= -EXTREME_BOUNDARY].mean()) if np.any(r <= -EXTREME_BOUNDARY) else None,
                 float(r[normal].mean()) if normal.any() else None,
                 float(r[r >= EXTREME_BOUNDARY].mean()) if np.any(r >= EXTREME_BOUNDARY) else None]
        result.update(q=q, conditional_p=p, normal_up=normal_up, class_means=means,
                      raw_p=q * p + (1 - q) * normal_up,
                      predicted=np.zeros(len(rows)),
                      baseline_classes=[(np.count_nonzero(r <= -EXTREME_BOUNDARY) + 1) / (len(r) + 3),
                                        (normal.sum() + 1) / (len(r) + 3),
                                        (np.count_nonzero(r >= EXTREME_BOUNDARY) + 1) / (len(r) + 3)])
    elif kind == "ensemble":
        result["raw_p"] = np.mean([_classifier(x, up, safe_live, kind=family) for family in ("logit", "lightgbm")], axis=0)
        result["predicted"] = np.mean([_regressor(x, r, safe_live, family) for family in ("logit", "lightgbm")], axis=0)
    else:
        result["raw_p"] = (np.full(len(rows), rate) if kind == "prior" else
                           _classifier(x, up, safe_live, kind=kind, strength=spec.get("c", 0.1)))
        result["predicted"] = (np.zeros(len(rows)) if kind == "prior" else
            _regressor(x, r, safe_live, kind))
    return result


def _calibrate(raw, labels, live, minimum):
    raw, labels = np.asarray(raw), np.asarray(labels, dtype=int)
    if len(labels) < minimum or min(np.count_nonzero(labels), np.count_nonzero(1 - labels)) < 8:
        return np.asarray(live, dtype=float)
    def logit(v):
        v = np.clip(np.asarray(v), 1e-5, 1 - 1e-5)
        return np.log(v / (1 - v)).reshape(-1, 1)
    model = LogisticRegression(C=1, max_iter=200, random_state=42)
    return model.fit(logit(raw), labels).predict_proba(logit(live))[:, 1]


def _radius(prior, scale, cfg, *, baseline=False):
    if len(prior) < cfg.calibration_rows:
        return None
    tail = prior[-min(120, cfg.calibration_window):]
    errors = sorted(abs(row["actual_return"] - (0.0 if baseline else row["predicted"]))
                    / max(row["scale"], 0.003) for row in tail)
    rank = min(len(errors), math.ceil((len(errors) + 1) * 0.8))
    return float(max(scale, 0.003) * errors[rank - 1])


def _records(train, rows, spec, prior, cfg):
    if np.isfinite(train[spec["features"]]).all(axis=1).sum() < cfg.initial_train:
        return []
    fit = _fit_predict(train, rows, spec)
    past = prior[-cfg.calibration_window:]
    if spec["kind"].startswith("c_"):
        q = _calibrate([r["raw_q"] for r in past], [abs(r["actual_return"]) >= EXTREME_BOUNDARY for r in past],
                       fit["q"], cfg.calibration_rows)
        events = [r for r in past if abs(r["actual_return"]) >= EXTREME_BOUNDARY]
        p = _calibrate([r["raw_conditional_p"] for r in events], [r["actual_return"] > 0 for r in events],
                       fit["conditional_p"], cfg.calibration_rows)
        classes = np.column_stack((q * (1 - p), 1 - q, q * p))
        direction = classes[:, 2] + classes[:, 1] * fit["normal_up"]
        means = fit["class_means"]
        predicted = (classes @ np.asarray(means) if all(m is not None for m in means)
                     else np.full(len(rows), float(np.mean(fit["train_returns"]))))
    else:
        direction = _calibrate([r["raw_p"] for r in past], [r["actual_return"] > 0 for r in past],
                               fit["raw_p"], cfg.calibration_rows)
        predicted = fit["predicted"]
        classes = None
    result = []
    for position, (day, row) in enumerate(rows.iterrows()):
        if not fit["valid"][position]:
            continue
        expected = float(predicted[position])
        if not np.isfinite(expected) or expected <= -1:
            continue
        scale = float(row.volatility_20d)
        radius = _radius(past, scale, cfg)
        baseline_radius = _radius(past, scale, cfg, baseline=True)
        record = {"date": day.date().isoformat(), "actual_return": _safe(row.label_return),
                  "p": float(direction[position]), "raw_p": float(fit["raw_p"][position]),
                  "predicted": expected, "baseline_p": float(fit["baseline_p"][position]),
                  "scale": scale, "low": max(-0.99, expected - radius) if radius is not None else None,
                  "high": expected + radius if radius is not None else None,
                  "baseline_radius": baseline_radius, "trigger": row.broad_trigger,
                  "trend_regime": row.trend_regime, "volatility_regime": row.volatility_regime,
                  "train_n": fit["train_n"], "calibration_evidence": "historical_reconstruction"}
        record["baselines"] = {"majority": record["baseline_p"],
                               "previous_direction": float(row.return_1d > 0),
                               "simple_trend": float(row.return_20d > 0)}
        if classes is not None:
            record.update(classes=classes[position].tolist(), raw_q=float(fit["q"][position]),
                          raw_conditional_p=float(fit["conditional_p"][position]),
                          class_means_available=all(m is not None for m in means),
                          baseline_classes=_safe(fit["baseline_classes"]))
        result.append(record)
    return result


def _walk(data, spec, start, end, cfg, *, prior=None):
    records = list(prior or [])
    for first in range(start, end, cfg.fold_size):
        train = data.iloc[:first - cfg.purge_rows]
        rows = data.iloc[first:min(first + cfg.fold_size, end)]
        records.extend(_records(train, rows, spec, records, cfg))
    return records


def _metrics(rows, *, c=False, baseline_id="majority"):
    if not rows:
        return {"n": 0, "brier": None, "log_loss": None, "hit_rate": None,
                "baseline_hit_rate": None, "baseline_brier": None, "mae_return": None,
                "baseline_mae_return": None, "coverage80": None, "interval_score80": None,
                "up_recall": None, "down_recall": None}
    actual = np.array([r["actual_return"] for r in rows])
    y = actual > 0
    p = np.clip([r["p"] for r in rows], 1e-6, 1 - 1e-6)
    baseline = np.clip([r.get("baselines", {}).get(baseline_id, r["baseline_p"]) for r in rows], 1e-6, 1 - 1e-6)
    majority = np.clip([r["baseline_p"] for r in rows], 1e-6, 1 - 1e-6)
    logistic = np.clip([r.get("baselines", {}).get("logistic", r["baseline_p"]) for r in rows], 1e-6, 1 - 1e-6)
    center = np.array([r["predicted"] for r in rows])
    out = {"n": len(rows), "brier": float(np.mean((p - y) ** 2)),
           "log_loss": float(-np.mean(y * np.log(p) + (1 - y) * np.log1p(-p))),
           "hit_rate": float(np.mean((p >= 0.5) == y)),
           "baseline_hit_rate": float(np.mean((baseline >= 0.5) == y)),
           "baseline_brier": float(np.mean((baseline - y) ** 2)),
           "baseline_log_loss": float(-np.mean(y * np.log(baseline) + (1 - y) * np.log1p(-baseline))),
           "mae_return": float(np.mean(np.abs(actual - center))),
           "baseline_mae_return": float(np.mean(np.abs(actual)))}
    out.update(up_recall=float(np.mean(p[y] >= .5)) if y.any() else None,
               down_recall=float(np.mean(p[~y] < .5)) if (~y).any() else None)
    out["probability_guardrail_brier"] = min(float(np.mean((prob - y) ** 2)) for prob in (majority, logistic))
    out["probability_guardrail_log_loss"] = min(float(-np.mean(y * np.log(prob) + (1 - y) * np.log1p(-prob)))
                                               for prob in (majority, logistic))
    out["reference_baselines"] = {}
    for name in ("majority", "previous_direction", "simple_trend", "logistic"):
        prob = np.clip([r.get("baselines", {}).get(name, r["baseline_p"]) for r in rows], 1e-6, 1 - 1e-6)
        out["reference_baselines"][name] = {"n": len(rows), "hit_rate": float(np.mean((prob >= .5) == y)),
            "brier": float(np.mean((prob - y) ** 2)),
            "probability_role": "uncalibrated_direction_reference" if name in {"previous_direction", "simple_trend"} else "probability_reference"}
    bands = [r for r in rows if r["low"] is not None]
    if bands:
        a, lo, hi = (np.array([r[k] for r in bands]) for k in ("actual_return", "low", "high"))
        out.update(coverage80=float(np.mean((a >= lo) & (a <= hi))),
                   interval_score80=float(np.mean(hi - lo + 10 * np.maximum(lo - a, 0) + 10 * np.maximum(a - hi, 0))))
    else:
        out.update(coverage80=None, interval_score80=None)
    bins = []
    for lower in np.arange(0, 1, 0.1):
        mask = (p >= lower) & (p < lower + 0.1)
        bins.append({"lower": round(float(lower), 1), "n": int(mask.sum()),
                     "mean_probability": float(p[mask].mean()) if mask.any() else None,
                     "observed_up_rate": float(y[mask].mean()) if mask.any() else None})
    out["calibration_bins"] = bins
    out["ece"] = float(sum(b["n"] / len(rows) * abs(b["mean_probability"] - b["observed_up_rate"])
                           for b in bins if b["n"]))
    if c:
        probabilities = np.clip(np.array([r["classes"] for r in rows]), 1e-9, 1)
        labels = np.where(actual <= -EXTREME_BOUNDARY, 0, np.where(actual >= EXTREME_BOUNDARY, 2, 1))
        baseline_classes = np.array([r["baseline_classes"] for r in rows])
        out.update(class_brier=float(np.mean(np.sum((probabilities - np.eye(3)[labels]) ** 2, axis=1))),
                   class_log_loss=float(-np.log(probabilities[np.arange(len(rows)), labels]).mean()),
                   class_accuracy=float(np.mean(np.argmax(probabilities, axis=1) == labels)),
                   baseline_class_brier=float(np.mean(np.sum((baseline_classes - np.eye(3)[labels]) ** 2, axis=1))),
                   baseline_class_log_loss=float(-np.log(baseline_classes[np.arange(len(rows)), labels]).mean()),
                   class_counts={name: int(np.count_nonzero(labels == i))
                                 for i, name in enumerate(("down_extreme", "normal", "up_extreme"))})
        predicted_class = np.argmax(probabilities, axis=1)
        out["class_recall"] = {name: float(np.mean(predicted_class[labels == i] == i)) if np.any(labels == i) else None
                               for i, name in enumerate(("down_extreme", "normal", "up_extreme"))}
    return out


def _difference_ci(rows, cfg, *, baseline_id="majority", hit=False):
    if not rows:
        return {"mean": None, "low": None, "high": None}
    y = np.array([r["actual_return"] > 0 for r in rows])
    probability = np.array([r["p"] for r in rows])
    baseline = np.array([r.get("baselines", {}).get(baseline_id, r["baseline_p"]) for r in rows])
    difference = (((probability >= .5) == y).astype(float) - ((baseline >= .5) == y).astype(float)
                  if hit else (probability - y) ** 2 - (baseline - y) ** 2)
    size, block = len(rows), min(cfg.bootstrap_block, len(rows))
    rng = np.random.default_rng(cfg.random_state)
    starts = rng.integers(0, size - block + 1, size=(cfg.bootstrap_samples, math.ceil(size / block)))
    positions = (starts[..., None] + np.arange(block)).reshape(cfg.bootstrap_samples, -1)[:, :size]
    samples = difference[positions].mean(axis=1)
    # Three forecast families per target are assessed together; do not pretend
    # that three independent 95% looks constitute one 95% promotion gate.
    alpha = .05 if hit else .05 / 3
    low, high = np.quantile(samples, [alpha / 2, 1 - alpha / 2])
    return {"mean": float(difference.mean()), "low": float(low), "high": float(high),
            "confidence_level": 1 - alpha, "method": "paired_moving_block",
            "block": block, "draws": cfg.bootstrap_samples}


def _select(candidates, histories, dev_dates, *, c, cfg, baseline_id="majority"):
    reports, eligible = [], []
    for spec in candidates:
        rows = [r for r in histories[spec["id"]] if r["date"] in dev_dates and (not c or r["trigger"] is True)]
        metrics = _metrics(rows, c=c, baseline_id=baseline_id)
        minimum = cfg.min_trigger_rows if c else cfg.development_rows
        reasons = []
        if len(rows) < minimum:
            reasons.append("insufficient_matched_development_rows")
        if rows and (metrics["brier"] > metrics["probability_guardrail_brier"] + 0.01
                     or metrics["log_loss"] > metrics["probability_guardrail_log_loss"] + 0.02
                     or (len(rows) >= 100 and metrics["ece"] > 0.10)
                     or metrics["mae_return"] > metrics["baseline_mae_return"] * 1.10):
            reasons.append("development_guardrail_failed")
        report = {"id": spec["id"], "group": spec["group"], "features": spec["features"],
                  "family": "linear" if spec["kind"] == "logit" else spec["kind"],
                  "metrics": metrics, "gate_reasons": reasons}
        reports.append(report)
        if not reasons:
            eligible.append((-metrics["hit_rate"], metrics["brier"], metrics["log_loss"], metrics["mae_return"],
                             len(spec["features"]), spec["id"], spec))
    # No candidate earns selection from the terminal period. Prior remains an
    # explicit, target-trained fallback when development evidence is too small.
    return min(eligible, key=lambda row: row[:-1])[-1] if eligible else candidates[0], reports


def _attach_logistic_reference(rows, logistic_rows):
    control = {r["date"]: r["p"] for r in logistic_rows}
    for row in rows:
        if row["date"] in control:
            row["baselines"]["logistic"] = control[row["date"]]


def _select_baseline(rows):
    if not rows:
        return "majority"
    metrics = _metrics(rows)
    order = {"majority": 0, "logistic": 1, "previous_direction": 2, "simple_trend": 3}
    return min(order, key=lambda name: (-metrics["reference_baselines"][name]["hit_rate"], order[name]))


def _evaluate(data, extra, cfg, version):
    n = len(data)
    terminal_start = n - cfg.terminal_rows
    development_start = terminal_start - cfg.development_rows
    start = development_start - cfg.calibration_rows
    sets, features = _candidate_sets(data, terminal_start, extra, cfg)
    control_spec = next(spec for spec in sets["A"] if spec["id"] == "price_logit")
    control_prior = _walk(data, control_spec, start, development_start, cfg)
    control_development = _walk(data, control_spec, development_start, terminal_start, cfg, prior=control_prior)
    _attach_logistic_reference(control_development, control_development)
    control_complete = _walk(data, control_spec, terminal_start, n, cfg, prior=control_development)
    _attach_logistic_reference(control_complete, control_complete)
    cohort_hash = hashlib.sha256(pd.util.hash_pandas_object(data, index=True).values.tobytes()).hexdigest()
    dev_dates = set(data.index[development_start:terminal_start].strftime("%Y-%m-%d"))
    cache = {"selected": {}, "reports": {}, "calibration": {}, "feature_evidence": features,
             "terminal_start": data.index[terminal_start].date().isoformat(),
             "terminal_end": data.index[-1].date().isoformat(),
             "last_scored_asof": data.index[-1].date().isoformat(), "cohort_hash": cohort_hash}
    for letter, candidates in sets.items():
        is_c = letter == "C"
        histories = {}
        for spec in candidates:
            if spec == control_spec:
                histories[spec["id"]] = copy.deepcopy(control_development)
            else:
                calibration = _walk(data, spec, start, development_start, cfg)
                histories[spec["id"]] = _walk(data, spec, development_start, terminal_start, cfg, prior=calibration)
                _attach_logistic_reference(histories[spec["id"]], control_development)
        baseline_rows = [r for r in control_development if r["date"] in dev_dates and (not is_c or r["trigger"] is True)]
        baseline_id = _select_baseline(baseline_rows)
        selected, development = _select(candidates, histories, dev_dates, c=is_c, cfg=cfg, baseline_id=baseline_id)
        prior = histories[selected["id"]]
        complete = _walk(data, selected, terminal_start, n, cfg, prior=prior)
        _attach_logistic_reference(complete, control_complete)
        terminal = complete[len(prior):]
        scored = [r for r in terminal if not is_c or r["trigger"] is True]
        metrics = _metrics(scored, c=is_c, baseline_id=baseline_id)
        ci = _difference_ci(scored, cfg, baseline_id=baseline_id)
        hit_ci = _difference_ci(scored, cfg, baseline_id=baseline_id, hit=True)
        reasons = []
        if len(terminal) < cfg.terminal_rows:
            reasons.append("incomplete_terminal_predictions")
        if is_c and len(scored) < cfg.min_trigger_rows:
            reasons.append("insufficient_triggered_oos")
        if hit_ci["low"] is None or hit_ci["low"] <= 0:
            reasons.append("direction_hit_gain_not_confirmed")
        if scored:
            if metrics["brier"] > metrics["probability_guardrail_brier"] + 0.01:
                reasons.append("direction_brier_guardrail_failed")
            if metrics["log_loss"] > metrics["probability_guardrail_log_loss"] + 0.02:
                reasons.append("log_loss_worse_than_baseline")
            if metrics["mae_return"] > metrics["baseline_mae_return"] * 1.05:
                reasons.append("mae_guardrail_failed")
            if metrics["coverage80"] is None or not 0.70 <= metrics["coverage80"] <= 0.90:
                reasons.append("coverage_guardrail_failed")
            if metrics["ece"] > (0.10 if is_c else 0.08):
                reasons.append("calibration_guardrail_failed")
            if is_c and metrics["class_brier"] > metrics["baseline_class_brier"]:
                reasons.append("class_brier_worse_than_baseline")
        model_version = version + ":" + letter + ":" + _hash({"selected": selected,
            "terminal_start": cache["terminal_start"], "terminal_end": cache["terminal_end"], "cohort": cohort_hash})[:12]
        recent_dates = set(data.index[-30:].strftime("%Y-%m-%d"))
        report = {**metrics, "model_version": model_version,
            "status": "accepted_research" if not reasons else "unconfirmed_research",
            "selected": selected, "development_candidates": development,
            "terminal_start": cache["terminal_start"], "terminal_end": cache["terminal_end"],
            "terminal_rows": cfg.terminal_rows, "scored_terminal_rows": len(terminal),
            "metric_scope": "prior_broad_triggered_sessions" if is_c else "all_sessions",
            "hit_rate_definition": "P(target next-session return > 0) >= 0.5",
            "class_definition": "target return <= -3%, normal, >= +3%" if is_c else None,
            "direction_baseline": baseline_id, "baseline_selected_in": "development",
            "brier_difference_ci": ci, "hit_rate_gain_ci95": hit_ci,
            "gate_reasons": reasons, "production_weight": 0.0,
            "selection_metric_order": ["direction_hit_rate", "direction_brier", "direction_log_loss", "mae_return", "complexity"],
            "selection_uses_terminal": False, "evaluation_protocol": "frozen_development_then_prequential_terminal_v1",
            "historical_input_mode": "caller_filtered_reconstruction_not_live_forward_evidence",
            "guardrails": {"brier_degradation_max": .01, "log_loss_degradation_max": .02,
                           "development_ece_max": .10, "terminal_mae_ratio_max": 1.05,
                           "probability_references": ["majority", "logistic"]},
            "windows": {"last_30": _metrics([r for r in scored if r["date"] in recent_dates], c=is_c, baseline_id=baseline_id),
                        "sealed_terminal": metrics},
            "all_sessions": _metrics(terminal, c=is_c, baseline_id=baseline_id),
            "regimes": {dimension: {name: _metrics([r for r in scored if r[dimension] == name], c=is_c, baseline_id=baseline_id)
                                    for name in sorted({r[dimension] for r in scored})}
                        for dimension in ("trend_regime", "volatility_regime")},
            "years": {year: _metrics([r for r in scored if r["date"].startswith(year)], c=is_c, baseline_id=baseline_id)
                      for year in sorted({r["date"][:4] for r in scored})}}
        cache["selected"][letter] = selected
        cache["reports"][letter] = report
        cache["calibration"][letter] = complete[-cfg.calibration_window:]
    return cache


def evaluate_and_predict(
    index_code: str, frame: pd.DataFrame, *, run_c: bool, target_date: str,
    cutoff: Any, feature_columns: Sequence[str] | None = None,
    evaluation_cache: Mapping | None = None,
    settings: BenchmarkSettings | Mapping | None = None,
    refresh_post_seal: bool = False,
) -> dict:
    """Return JSON-safe forecasts, sealed evidence, and a persistable private cache.

    Caller must persist the returned cache and strip it from public payloads.
    Additional numeric features must be explicitly named and already filtered by
    their actual availability at each row's cutoff. Missing selected live inputs
    abstain without changing the frozen recipe or its terminal evidence.
    """
    cfg = settings if isinstance(settings, BenchmarkSettings) else BenchmarkSettings(**dict(settings or {}))
    code = str(index_code).upper()
    if code not in SUPPORTED_TARGETS:
        raise ValueError("unsupported target index")
    if frame.attrs.get("index_code", code) != code:
        raise ValueError("target index identity mismatch")
    extra = list(dict.fromkeys(feature_columns or []))
    reserved = {"close", "open", "high", "low", "parent_close", "parent_return", "broad_trigger", *PRICE_FEATURES}
    if len(extra) > 32 or any(name in reserved or any(token in name.lower() for token in
        ("label", "target", "future", "next_", "actual", "outcome")) for name in extra):
        raise ValueError("invalid or unbounded feature contract")
    data, history = _prepare(frame, extra)
    stamp = pd.Timestamp(cutoff)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    target = pd.Timestamp(target_date)
    if data.empty:
        return _unavailable("insufficient_history", code=code, target=target_date, cutoff=cutoff)
    asof = data.index[-1].date().isoformat()
    if (target.tzinfo is not None or target <= data.index[-1]
            or data.index[-1].date() > stamp.tz_convert("Asia/Seoul").date()):
        raise ValueError("target and cutoff must follow the observed anchor")
    if stamp < data.index[-1].tz_localize("Asia/Seoul") + pd.Timedelta(hours=15, minutes=30):
        raise ValueError("anchor must be a completed cash session before the cutoff")
    if len(data) < 21 or not np.isfinite(data.close.iloc[-21:]).all():
        return _unavailable("incomplete_target_anchor", code=code, target=target_date,
                            cutoff=cutoff, asof=asof, cache=evaluation_cache)
    contract = {"algorithm": ALGORITHM_VERSION, "settings": asdict(cfg), "target": code,
                "features": extra, "parent_features": "parent_return" in data,
                "runtime": {name: package_version(name) for name in ("numpy", "pandas", "scikit-learn", "lightgbm")},
                "source_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    identity = _hash(contract)
    version = f"{ALGORITHM_VERSION}:{code}:{identity[:12]}"
    minimum = cfg.initial_train + cfg.purge_rows + cfg.calibration_rows + cfg.development_rows + cfg.terminal_rows
    if len(history) < minimum:
        return _unavailable("insufficient_history", code=code, target=target_date,
                            cutoff=cutoff, asof=asof, cache=evaluation_cache)
    reused = evaluation_cache is not None
    if reused:
        cache = copy.deepcopy(dict(evaluation_cache))
        if cache.get("cache_hash") != _hash({k: v for k, v in cache.items() if k != "cache_hash"}):
            return _unavailable("cache_integrity_failed", code=code, target=target_date,
                                cutoff=cutoff, asof=asof, cache=evaluation_cache)
        if (cache.get("contract_hash") != identity or cache.get("schema_version") != SCHEMA_VERSION
                or not {"selected", "reports", "calibration", "prefix_end", "price_prefix_end", "prefix_hash"} <= cache.keys()):
            return _unavailable("cache_incompatible", code=code, target=target_date,
                                cutoff=cutoff, asof=asof, cache=evaluation_cache)
        if (cache["price_prefix_end"] not in frame.index.strftime("%Y-%m-%d")
                or _sealed_hash(frame, cache) != cache["prefix_hash"]):
            return _unavailable("cache_history_changed", code=code, target=target_date,
                                cutoff=cutoff, asof=asof, cache=evaluation_cache)
        for records in cache["calibration"].values():
            for record in records:
                if record["date"] <= cache["terminal_end"]:
                    continue
                current = history.loc[pd.Timestamp(record["date"]), "label_return"] if pd.Timestamp(record["date"]) in history.index else None
                if current is None or not math.isclose(record["actual_return"], float(current), abs_tol=1e-12, rel_tol=1e-10):
                    if not refresh_post_seal or current is None:
                        return _unavailable("cache_post_seal_revision", code=code, target=target_date,
                                            cutoff=cutoff, asof=asof, cache=evaluation_cache)
                    # Preserve the issued prediction, correcting only its later
                    # observed outcome after the caller verified the revision.
                    record["actual_return"] = float(current)
                    cache["post_seal_corrections_applied"] = True
    else:
        with threadpool_limits(limits=1):
            cache = _evaluate(history, extra, cfg, version)
        used = {name for spec in cache["selected"].values() for name in spec["features"]}
        fingerprint_columns = [name for name in extra if name in used]
        if "parent_return" in used:
            fingerprint_columns.append("parent_return" if "parent_return" in frame else "parent_close")
        fingerprint_columns.append("broad_trigger")
        cache.update(schema_version=SCHEMA_VERSION, contract_hash=identity, contract=contract,
                     model_version=version, prefix_end=history.index[-1].date().isoformat(),
                     price_prefix_start=frame.index[0].date().isoformat(),
                     feature_prefix_start=history.index[0].date().isoformat(),
                     price_prefix_end=asof, fingerprint_columns=fingerprint_columns, pending={})
        cache["prefix_hash"] = _sealed_hash(frame, cache)
    live_data_hash = _prefix_hash(frame, ["close", *cache["fingerprint_columns"]], asof)
    models = {}
    with threadpool_limits(limits=1):
        for letter in "ABC":
            spec = cache["selected"][letter]
            prior = cache["calibration"][letter]
            if reused:
                new = int(history.index.searchsorted(pd.Timestamp(cache["last_scored_asof"]), side="right"))
                prior = _extend_calibration(history, spec, prior, cache.get("pending", {}).get(letter), new, cfg)
                cache["calibration"][letter] = prior[-cfg.calibration_window:]
            model_version = cache["reports"][letter]["model_version"]
            common = {"model_version": model_version, "evaluation": cache["reports"][letter],
                      "method": spec["id"], "features": spec["features"], "production_weight": 0.0,
                      "close": float(data.close.iloc[-1]), "asof_date": asof, "target_date": target_date,
                      "trained_through": history.index[-cfg.purge_rows - 1].date().isoformat(),
                      "p_up": None, "p_down": None, "expected_return": None, "expected_index": None,
                      "interval": None, "status": "not_ready"}
            if not np.isfinite(data[spec["features"]].iloc[-1]).all():
                models[letter] = ({**common, "status": "not_triggered", "p_normal": None}
                                 if letter == "C" and not run_c else
                                 {**common, "reason": "missing_selected_live_features"})
                continue
            # As in walk-forward folds, one observation is purged from the
            # most-recent labeled prefix before the live recipe is fitted.
            training = history.iloc[:-cfg.purge_rows]
            rows = _records(training, data.iloc[-1:], spec, prior, cfg)
            if not rows or rows[0]["low"] is None:
                models[letter] = {**common, "reason": "insufficient_live_calibration"}
                continue
            row = rows[0]
            if cache.get("pending", {}).get(letter, {}).get("date") != asof:
                cache.setdefault("pending", {})[letter] = row
            if letter == "C" and not run_c:
                models[letter] = {**common, "status": "not_triggered", "p_normal": None}
                continue
            close = common["close"]
            model = {**common, "status": "ready", "p_up": row["p"], "p_down": 1 - row["p"],
                     "expected_return": row["predicted"], "expected_index": close * (1 + row["predicted"]),
                     "interval": {"low": close * (1 + row["low"]), "high": close * (1 + row["high"]), "level": 0.8},
                     "train_n": row["train_n"], "calibration_n": len(prior),
                     "fit_version": _hash({"recipe": model_version, "model": spec, "last_training": training.index[-1].date().isoformat(),
                                           "data": live_data_hash})[:20],
                     "calibration_method": "prior_mature_oof_sigmoid",
                     "interval_method": "prior_mature_oof_volatility_scaled_residuals"}
            if letter == "C":
                down, normal, up = row["classes"]
                model.update(p_down=down, p_normal=normal, p_up=up, p_extreme=down + up,
                             direction_p_up=row["p"], p_up_given_extreme=up / (down + up) if down + up else None,
                             threshold=THRESHOLD, class_means_available=row["class_means_available"])
            models[letter] = model
    cache["last_scored_asof"] = history.index[-1].date().isoformat()
    cache = _safe(cache)
    cache["cache_hash"] = _hash({k: v for k, v in cache.items() if k != "cache_hash"})
    return _safe({"schema_version": SCHEMA_VERSION, "model_version": version,
        "status": "ready" if any(m["status"] == "ready" for m in models.values()) else "not_ready",
        "index_code": code, "asof_date": asof, "target_date": target_date, "cutoff": stamp.isoformat(),
        "cache_reused": reused, "models": models, "evaluation_cache": cache})
