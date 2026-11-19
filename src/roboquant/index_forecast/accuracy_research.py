"""Isolated accuracy research on the frozen after-close forecast archive."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.linear_model import HuberRegressor, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from roboquant.index_forecast.dataset import PRICE_FEATURES
from roboquant.index_forecast.postclose import PROTOCOL, historical_cutoff
from roboquant.index_forecast.reliable import ReliableSettings, _bootstrap
from roboquant.index_forecast.reliable_intervals import interval_score80

ORIGIN = "2019-01-02"
TRAIN_SIZE = 504
REFIT_STEP = 21
HISTORY_SIZE = 252
MIN_DEVELOPMENT = 252
RECENT_SESSIONS = 30
VOL_FLOOR = .005
MARKETS = ("KOSPI", "KOSDAQ")
POINT_BASELINES = ("linear:price", "flat", "expanding_mean")
POINT_CANDIDATES = ("ridge_rolling504", "huber_vol_rolling504")
PROBABILITY_BASELINES = ("linear:price", "expanding_up_rate")
PROBABILITY_MODELS = (*PROBABILITY_BASELINES, "laplace_prior252")
INTERVAL_MODELS = ("linear:price", "volatility_normalized")
BOOTSTRAP = ReliableSettings()


def _timestamp(value) -> pd.Timestamp | None:
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return stamp.tz_convert("UTC") if pd.notna(stamp) and stamp.tzinfo is not None else None


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _session_grid(sessions: pd.DataFrame) -> pd.DataFrame:
    grid = sessions[["date", "open_at", "close_at"]].copy()
    grid["date"] = grid["date"].astype(str)
    for column in ("open_at", "close_at"):
        grid[column] = grid[column].map(_timestamp)
    grid = grid.sort_values("date").reset_index(drop=True)
    if grid.empty or grid.at[0, "date"] != ORIGIN:
        raise ValueError(f"Schedule must start at the fixed refit origin {ORIGIN}")
    labels = pd.to_datetime(grid["date"], format="%Y-%m-%d", errors="coerce")
    if (grid["date"].duplicated().any() or not labels.dt.strftime("%Y-%m-%d").equals(grid["date"])
            or grid[["open_at", "close_at"]].isna().any().any()
            or (grid["open_at"] >= grid["close_at"]).any()
            or not grid["open_at"].dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d").equals(grid["date"])
            or not grid["close_at"].dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d").equals(grid["date"])):
        raise ValueError("Invalid or duplicate exchange sessions")
    grid["cutoff"] = grid["close_at"].map(historical_cutoff)
    return grid


def _prices_on_grid(prices: pd.DataFrame, grid: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    frame = grid.copy()
    frame["close"] = np.nan
    frame["price_available_at"] = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
    frame["price_status"] = "missing_price"
    grouped = {date: rows.to_dict("records") for date, rows in prices.groupby("session_date")}
    for i, session in enumerate(grid.itertuples()):
        if session.close_at > as_of:
            frame.at[i, "price_status"] = "session_not_closed"
            continue
        releases = []
        reasons = set()
        for row in grouped.get(session.date, ()):
            price = _number(row["close"])
            raw_release = row.get("published_at")
            assumed = pd.isna(raw_release)
            release = session.close_at if assumed else _timestamp(raw_release)
            if release is None or release < session.close_at:
                reasons.add("invalid_price_publication")
            elif release > as_of:
                reasons.add("price_publication_after_as_of")
            elif row.get("status") == "conflict":
                reasons.add("conflicting_price")
            elif price is None or price <= 0:
                reasons.add("invalid_price")
            else:
                releases.append((release, price, assumed))
        if "conflicting_price" in reasons:
            frame.at[i, "price_status"] = "conflicting_price"
        elif releases:
            # Hold the first final release fixed when a later timestamped revision appears.
            first = min(release for release, _, _ in releases)
            first_releases = [(price, assumed) for release, price, assumed in releases if release == first]
            if len({price for price, _ in first_releases}) != 1:
                frame.at[i, "price_status"] = "conflicting_price"
                continue
            frame.at[i, "close"] = first_releases[0][0]
            frame.at[i, "price_available_at"] = first
            frame.at[i, "price_status"] = ("historical_close_time_assumption"
                if all(assumed for _, assumed in first_releases) else "explicit_publication_timestamp")
        elif reasons:
            frame.at[i, "price_status"] = sorted(reasons)[0]
    close = frame["close"]
    log_return = np.log(close / close.shift(1))
    for days in (1, 5, 20):
        frame[f"return_{days}d"] = close.pct_change(days, fill_method=None)
    for days in (5, 20):
        frame[f"volatility_{days}d"] = log_return.rolling(days).std()
        frame[f"ma_gap_{days}d"] = close / close.rolling(days).mean() - 1
    available = frame["price_available_at"]
    frame["feature_available_at"] = pd.concat(
        [available.shift(i) for i in range(21)], axis=1,
    ).max(axis=1)
    frame["features_ready"] = (np.isfinite(frame[PRICE_FEATURES]).all(axis=1)
                               & (frame["feature_available_at"] <= frame["cutoff"]))
    frame["label_return"] = close.shift(-1) / close - 1
    frame["label_available_at"] = available.shift(-1)
    frame["volatility_scale"] = frame["volatility_20d"].clip(lower=VOL_FLOOR)
    return frame


def _fit(frame: pd.DataFrame, anchor: int) -> tuple[dict, dict]:
    if not frame.at[anchor, "features_ready"]:
        return {}, {"fit_reason": "anchor_price_features_unavailable"}
    cutoff = frame.at[anchor, "cutoff"]
    # A one-session purge removes as-of session anchor-1, even when its label
    # (the anchor session close) is available at the anchor forecast cutoff.
    before_purge = frame.iloc[:max(0, anchor - 1)]
    train = before_purge.loc[
        before_purge["features_ready"]
        & np.isfinite(before_purge["label_return"])
        & (before_purge["label_available_at"] <= cutoff)
    ].tail(TRAIN_SIZE)
    metadata = {
        "fit_asof": frame.at[anchor, "date"],
        "fit_cutoff": cutoff.isoformat(),
        "train_n": len(train),
    }
    if len(train) < TRAIN_SIZE:
        return {}, {**metadata, "fit_reason": "insufficient_mature_training_rows"}
    x = train[PRICE_FEATURES].to_numpy()
    y = train["label_return"].to_numpy()
    ridge = make_pipeline(StandardScaler(), Ridge(alpha=10))
    huber = make_pipeline(StandardScaler(), HuberRegressor(epsilon=1.35, alpha=.01, max_iter=500))
    ridge.fit(x, y)
    huber.fit(x, y / train["volatility_scale"].to_numpy())
    metadata.update(
        train_first_asof=train.iloc[0]["date"],
        train_last_asof=train.iloc[-1]["date"],
        train_latest_label_available_at=train["label_available_at"].max().isoformat(),
    )
    return dict(zip(POINT_CANDIDATES, (ridge, huber), strict=True)), metadata


def _interval(record: dict) -> list[float] | None:
    low, high = _number(record.get("range_low")), _number(record.get("range_high"))
    return [low, high] if low is not None and high is not None and low <= high else None


def _archive_rows(records: list, frame: pd.DataFrame, as_of: pd.Timestamp) -> tuple[dict, dict]:
    by_date = defaultdict(list)
    for row in records:
        if isinstance(row, dict) and isinstance(row.get("asof_date"), str):
            by_date[row["asof_date"]].append(row)
    matched, exclusions = {}, {}
    cutoffs = dict(zip(frame["date"], frame["cutoff"], strict=True))
    for i in range(len(frame) - 1):
        session, target = frame.iloc[i], frame.iloc[i + 1]
        day, target_day = session["date"], target["date"]
        if target["close_at"] > as_of:
            break
        found = by_date[day]
        if len(found) != 1:
            exclusions[target_day] = "duplicate_archive_forecast" if found else "missing_archive_forecast"
            continue
        record = found[0]
        cutoff = _timestamp(record.get("prediction_cutoff"))
        archived_maturity = _timestamp(record.get("label_available_at"))
        archived_close = _timestamp(record.get("target_close_at"))
        if (record.get("target_date") != target_day or cutoff is None or cutoff != session["cutoff"]
                or cutoff >= target["open_at"] or archived_close is None
                or archived_close.tz_convert("Asia/Seoul").date().isoformat() != target_day
                or archived_maturity is None or archived_maturity < archived_close):
            exclusions[target_day] = "archive_session_or_clock_mismatch"
            continue
        fit_day = record.get("fit_asof")
        fit_cutoff = _timestamp(record.get("fit_cutoff"))
        if (not isinstance(fit_day, str) or fit_day > day or fit_cutoff is None
                or fit_cutoff > cutoff or fit_cutoff != cutoffs.get(fit_day)):
            exclusions[target_day] = "invalid_archive_fit_clock"
            continue
        if pd.isna(session["close"]) or pd.isna(target["close"]):
            unavailable = target if pd.isna(target["close"]) else session
            exclusions[target_day] = unavailable["price_status"]
            continue
        if session["price_available_at"] > cutoff:
            exclusions[target_day] = "origin_price_publication_after_prediction"
            continue
        actual = _number(session["label_return"])
        historical_actual = _number(record.get("actual_return"))
        if (actual is None or historical_actual is None
                or not np.isclose(actual, historical_actual, atol=1e-6, rtol=1e-5)):
            exclusions[target_day] = "archive_actual_close_mismatch"
            continue
        maturity = max(archived_maturity, target["price_available_at"])
        if maturity > as_of:
            exclusions[target_day] = "archive_label_publication_after_as_of"
            continue
        flags = []
        if archived_close != target["close_at"]:
            flags.append("stored_target_close_mismatch")
        if archived_maturity < target["close_at"]:
            flags.append("stored_label_before_official_close")
        matched[i] = (record, actual, maturity, flags)
    return matched, exclusions


def _observations(frame: pd.DataFrame, matched: dict, start: str, exclusions: dict) -> list[dict]:
    rows = []
    fits = {}
    prior = []
    # Filter archived outcome records before using the trailing Laplace window.
    for i, (record, actual, maturity, flags) in matched.items():
        day = record["asof_date"]
        target_day = record["target_date"]
        cutoff = frame.at[i, "cutoff"]
        if day >= start:
            history = [r for r in prior if r[1] <= cutoff][-HISTORY_SIZE:]
            probability = _number(record.get("p_up"))
            current = _number(record.get("expected_return"))
            baselines = record.get("baselines", {})
            mean = _number(baselines.get("mean", {}).get("expected_return"))
            expanding_up_rate = _number(baselines.get("up_rate", {}).get("p_up"))
            point = {"linear:price": current, "flat": 0., "expanding_mean": mean}
            anchor = i // REFIT_STEP * REFIT_STEP
            if anchor not in fits:
                fits[anchor] = _fit(frame, anchor)
            models, metadata = fits[anchor]
            if not models:
                exclusions[target_day] = metadata["fit_reason"]
            elif not frame.at[i, "features_ready"]:
                exclusions[target_day] = "forecast_price_features_unavailable"
            else:
                x = frame.loc[[i], PRICE_FEATURES].to_numpy()
                ridge, huber = POINT_CANDIDATES
                point[ridge] = _number(models[ridge].predict(x)[0])
                point[huber] = _number(models[huber].predict(x)[0] * frame.at[i, "volatility_scale"])
                if any(value is None for value in point.values()):
                    exclusions[target_day] = "missing_finite_point_forecast"
            shadow = record.get("interval_shadows", {}).get("volatility_normalized", {})
            rows.append({
                "asof_date": day,
                "target_date": target_day,
                "prediction_cutoff": cutoff.isoformat(),
                "target_close_at": frame.at[i + 1, "close_at"].isoformat(),
                "label_available_at": maturity.isoformat(),
                "archive_target_clock_flags": flags,
                "actual_return": actual,
                "origin_close": float(frame.at[i, "close"]),
                "actual_close": float(frame.at[i + 1, "close"]),
                "target_price_provenance": frame.at[i + 1, "price_status"],
                "origin_price_provenance": frame.at[i, "price_status"],
                "prior_oof_n": len(history),
                "prior_oof_last_target": history[-1][0] if history else None,
                "prior_oof_latest_label_available_at": max(r[1] for r in history).isoformat() if history else None,
                "point": point,
                "probability": {
                    "linear:price": probability if probability is not None and 0 <= probability <= 1
                    and record.get("calibration_ready") is True else None,
                    "expanding_up_rate": expanding_up_rate if expanding_up_rate is not None
                    and 0 <= expanding_up_rate <= 1 else None,
                    "laplace_prior252": (sum(r[2] > 0 for r in history) + 1) / (HISTORY_SIZE + 2)
                    if len(history) == HISTORY_SIZE else None,
                },
                "interval": {
                    "linear:price": _interval(record),
                    "volatility_normalized": _interval(shadow) if shadow.get("status") == "ready" else None,
                },
                "volatility_regime": record.get("volatility_regime", "unknown"),
                "trend_regime": record.get("trend_regime", "unknown"),
                **metadata,
            })
        prior.append((target_day, maturity, actual))
    return rows


def _ci(differences: np.ndarray) -> dict | None:
    # Require enough matched observations for at least two full blocks.
    return _bootstrap(differences, BOOTSTRAP) if len(differences) >= 2 * BOOTSTRAP.bootstrap_block_days else None


def _point_metrics(rows: list[dict], *, bootstrap: bool) -> dict:
    methods = (*POINT_BASELINES, *POINT_CANDIDATES)
    if not rows:
        return {name: {"n": 0} for name in methods}
    y = np.array([r["actual_return"] for r in rows])
    origin = np.array([r["origin_close"] for r in rows])
    errors = {}
    reports = {}
    for name in methods:
        forecast = np.array([r["point"][name] for r in rows])
        errors[name] = abs(forecast - y)
        reports[name] = {
            "n": len(rows),
            "mae_points": float(np.mean(errors[name] * origin)),
            "mae_return_pp": float(np.mean(errors[name]) * 100),
            "return_direction_hit": float(np.mean((forecast > 0) == (y > 0))),
        }
    if bootstrap:
        for name in POINT_CANDIDATES:
            reports[name]["mae_difference_ci95"] = {
                baseline: {
                    "points": _ci((errors[name] - errors[baseline]) * origin),
                    "return_pp": _ci((errors[name] - errors[baseline]) * 100),
                } for baseline in POINT_BASELINES
            }
    return reports


def _probability_metrics(rows: list[dict], *, bootstrap: bool) -> dict:
    if not rows:
        return {name: {"n": 0} for name in PROBABILITY_MODELS}
    y = np.array([r["actual_return"] > 0 for r in rows])
    reports, brier, logloss = {}, {}, {}
    for name in PROBABILITY_MODELS:
        p = np.array([r["probability"][name] for r in rows])
        clipped = np.clip(p, 1e-12, 1 - 1e-12)
        brier[name] = (p - y) ** 2
        logloss[name] = -np.log(np.where(y, clipped, 1 - clipped))
        reports[name] = {
            "n": len(rows),
            "brier": float(brier[name].mean()),
            "log_loss": float(logloss[name].mean()),
            "probability_direction_hit": float(np.mean((p >= .5) == y)),
        }
        if bootstrap:
            buckets = np.minimum((p * 10).astype(int), 9)
            reports[name]["reliability_bins"] = [
                {"lower": i / 10, "upper": (i + 1) / 10, "n": int(mask.sum()),
                 "mean_p_up": float(p[mask].mean()) if mask.any() else None,
                 "observed_up_rate": float(y[mask].mean()) if mask.any() else None}
                for i in range(10) if (mask := buckets == i).any()
            ]
    if bootstrap:
        reports["laplace_prior252"].update(
            brier_difference_ci95={name: _ci(brier["laplace_prior252"] - brier[name])
                                   for name in PROBABILITY_BASELINES},
            log_loss_difference_ci95={name: _ci(logloss["laplace_prior252"] - logloss[name])
                                      for name in PROBABILITY_BASELINES},
        )
    return reports


def _interval_metrics(rows: list[dict], *, bootstrap: bool) -> dict:
    if not rows:
        return {name: {"n": 0} for name in INTERVAL_MODELS}
    y = np.array([r["actual_return"] for r in rows])
    reports, scores = {}, {}
    for name in INTERVAL_MODELS:
        low, high = np.array([r["interval"][name] for r in rows]).T
        scores[name] = interval_score80(y, low, high)
        reports[name] = {
            "n": len(rows),
            "coverage80": float(np.mean((y >= low) & (y <= high))),
            "average_width": float(np.mean(high - low)),
            "lower_breach": float(np.mean(y < low)),
            "upper_breach": float(np.mean(y > high)),
            "interval_score80": float(scores[name].mean()),
        }
    if bootstrap:
        reports["volatility_normalized"]["interval_score_difference_ci95"] = _ci(
            scores["volatility_normalized"] - scores["linear:price"],
        )
    return reports


def _pairs(rows: list[dict], dimension: str, names: tuple[str, ...]) -> list[dict]:
    return [row for row in rows if all(row[dimension].get(name) is not None for name in names)]


def _summarize(rows: list[dict], *, bootstrap: bool = True) -> dict:
    points = _pairs(rows, "point", (*POINT_BASELINES, *POINT_CANDIDATES))
    probabilities = _pairs(rows, "probability", PROBABILITY_MODELS)
    intervals = _pairs(rows, "interval", INTERVAL_MODELS)
    return {
        "n": len(points),
        "target_dates": [r["target_date"] for r in points],
        "probability_target_dates": [r["target_date"] for r in probabilities],
        "interval_target_dates": [r["target_date"] for r in intervals],
        "models": _point_metrics(points, bootstrap=bootstrap),
        "probabilities": _probability_metrics(probabilities, bootstrap=bootstrap),
        "intervals": _interval_metrics(intervals, bootstrap=bootstrap),
    }


def _window(rows: list[dict], dates: list[str], exclusions: dict, freeze: pd.Timestamp | None = None) -> dict:
    eligible = set(dates)
    relevant = [r for r in rows if r["target_date"] in eligible]
    immaturity = {r["target_date"]: "label_not_mature_at_development_freeze" for r in relevant
                  if freeze is not None and _timestamp(r["label_available_at"]) > freeze}
    relevant = [r for r in relevant if r["target_date"] not in immaturity]
    output = _summarize(relevant)
    scored = set(output["target_dates"])
    output["expected_n"] = len(dates)
    output["exclusions"] = {day: immaturity.get(day, exclusions.get(day, "no_matched_forecast"))
                            for day in dates if day not in scored}
    output["regimes"] = {}
    for name in ("year", "volatility_regime", "trend_regime"):
        bins = defaultdict(list)
        for row in relevant:
            key = row["target_date"][:4] if name == "year" else row[name]
            bins[key or "unknown"].append(row)
        output["regimes"][name] = {key: _summarize(values, bootstrap=False) for key, values in bins.items()}
    return output


def _development_gate(development: dict, *, comparator_selected: bool) -> dict:
    point = development["models"]
    probability = development["probabilities"]["laplace_prior252"]
    interval = development["intervals"]["volatility_normalized"]
    expected = development["expected_n"]
    point_reasons, probability_reasons, interval_reasons = [], [], []
    best = None
    if point[POINT_CANDIDATES[0]]["n"] != expected or expected < MIN_DEVELOPMENT:
        point_reasons.append("insufficient_complete_common_development")
    else:
        best = min(POINT_CANDIDATES, key=lambda name: point[name]["mae_points"])
        for baseline in POINT_BASELINES:
            if point[best]["mae_points"] >= point[baseline]["mae_points"]:
                point_reasons.append(f"point_mae_did_not_beat_{baseline}")

    if probability["n"] != expected or expected < MIN_DEVELOPMENT:
        probability_reasons.append("insufficient_complete_probability_development")
    else:
        for baseline in PROBABILITY_BASELINES:
            if probability["brier_difference_ci95"][baseline]["high"] >= 0:
                probability_reasons.append(f"brier_improvement_not_significant_vs_{baseline}")
            if probability["log_loss"] > development["probabilities"][baseline]["log_loss"]:
                probability_reasons.append(f"log_loss_worse_than_{baseline}")

    if interval["n"] != expected or expected < MIN_DEVELOPMENT:
        interval_reasons.append("insufficient_complete_interval_development")
    else:
        current = development["intervals"]["linear:price"]
        if not .75 <= interval["coverage80"] <= .85:
            interval_reasons.append("coverage80_outside_75_85")
        if interval["average_width"] > 1.10 * current["average_width"]:
            interval_reasons.append("width_more_than_110_percent_of_current")
        if interval["lower_breach"] > current["lower_breach"] + .02:
            interval_reasons.append("lower_breach_more_than_2pp_above_current")
        if interval["interval_score80"] >= current["interval_score80"]:
            interval_reasons.append("interval_score_did_not_beat_current")

    if not comparator_selected:
        for reasons in (point_reasons, probability_reasons, interval_reasons):
            reasons.append("fixed_archive_comparator_was_not_archive_selected")
    return {
        "best_point_candidate": best,
        "selected_point_candidate": best if not point_reasons else None,
        "selected_probability_candidate": "laplace_prior252" if not probability_reasons else None,
        "selected_interval_candidate": "volatility_normalized" if not interval_reasons else None,
        "point_reasons": point_reasons,
        "probability_reasons": probability_reasons,
        "interval_reasons": interval_reasons,
    }


def _market_result(prices: pd.DataFrame, grid: pd.DataFrame, evaluation: dict,
                   as_of: pd.Timestamp, recent: list[str]) -> dict:
    forward = {"status": "not_started", "new_matched_actuals": 0,
               "required_new_matched_actuals": 252, "production_weight": 0.}
    unavailable = {"status": "not_ready", "selected": None, "observations": [],
                   "windows": {}, "forward_gate": forward, "production_weight": 0.}
    start = evaluation.get("development_start")
    frozen = evaluation.get("selection_frozen_before")
    cutoffs = dict(zip(grid["date"], grid["cutoff"], strict=True))
    records = evaluation.get("candidate_observations", {}).get("linear:price")
    if (not isinstance(start, str) or not isinstance(frozen, str) or start not in cutoffs
            or frozen not in cutoffs or start >= frozen or not records
            or evaluation.get("settings", {}).get("issuance_protocol") != PROTOCOL):
        return {**unavailable, "reason": "missing_archive_development_or_after_close_history"}

    frame = _prices_on_grid(prices, grid, as_of)
    matched, exclusions = _archive_rows(records, frame, as_of)
    rows = _observations(frame, matched, start, exclusions)
    terminal = {r["target_date"] for r in evaluation.get("observations", [])
                if isinstance(r, dict) and isinstance(r.get("target_date"), str)}
    completed = grid.loc[grid["close_at"] <= as_of, "date"].tolist()
    recent_set = set(recent)
    consecutive = list(zip(completed[:-1], completed[1:], strict=True))
    development_dates = [b for a, b in consecutive
                         if start <= a < frozen and b not in recent_set]
    terminal_dates = [b for a, b in consecutive
                      if a >= frozen and b in terminal and b not in recent_set]
    development = _window(rows, development_dates, exclusions, cutoffs[frozen])
    selection = evaluation.get("selected")
    selection = selection if isinstance(selection, dict) else {}
    archive_selected = (f"{selection['family']}:{selection['group']}"
                        if "family" in selection and "group" in selection else None)
    comparator_selected = archive_selected == "linear:price"
    gate = _development_gate(development, comparator_selected=comparator_selected)
    point_rows = _pairs(rows, "point", (*POINT_BASELINES, *POINT_CANDIDATES))
    return {
        "status": "evaluated" if point_rows else "not_ready",
        "selected": gate["selected_point_candidate"],
        "archive_comparator": {"candidate": "linear:price", "archive_selected": archive_selected,
                               "matches_archive_selection": comparator_selected},
        "development_gate": gate,
        "development_start": start,
        "selection_frozen_before": frozen,
        "development_selection_cutoff": cutoffs[frozen].isoformat(),
        "windows": {
            "development": development,
            "older_terminal_exploratory": _window(rows, terminal_dates, exclusions),
            "last30_descriptive": _window(rows, recent, exclusions),
        },
        "observations": point_rows,
        "price_provenance_counts": {str(k): int(v) for k, v in
            frame.loc[frame["close_at"] <= as_of, "price_status"].value_counts().items()},
        "forward_gate": forward,
        "production_weight": 0.,
    }


def evaluate_accuracy_research(prices: pd.DataFrame, archive: dict, *, sessions: pd.DataFrame,
                               as_of: datetime) -> dict:
    """Replay independent KOSPI/KOSDAQ challengers with fixed historical cutoffs.

    First publication times are used when supplied. Date-only closes may be used
    under a marked historical exchange-close availability assumption.
    """
    now = _timestamp(as_of)
    if now is None:
        raise ValueError("as_of must be timezone-aware")
    if not {"index_code", "session_date", "close"}.issubset(prices.columns):
        raise ValueError("prices must include index_code, session_date and close")
    grid = _session_grid(sessions)
    recent = grid.loc[grid["close_at"] <= now, "date"].tail(RECENT_SESSIONS).tolist()
    by_market = defaultdict(list)
    for entry in archive.get("markets", []):
        if isinstance(entry, dict) and isinstance(entry.get("market"), str):
            by_market[entry["market"]].append(entry.get("evaluation", {}))
    session_dates = pd.to_datetime(prices["session_date"], format="%Y-%m-%d", errors="coerce")
    normalized = prices.assign(session_date=session_dates.dt.strftime("%Y-%m-%d"))
    markets = {}
    for market in MARKETS:
        entries = by_market[market]
        evaluation = entries[0] if len(entries) == 1 and isinstance(entries[0], dict) else {}
        markets[market] = _market_result(
            normalized.loc[normalized["index_code"].eq(market)], grid, evaluation, now, recent,
        )
    return {
        "schema_version": "robo_stock_accuracy_research_v1",
        "as_of": now.isoformat(),
        "evidence_status": "retrospective_reconstruction_and_previously_viewed_terminal",
        "production_weight": 0.,
        "protocol": {
            "issuance": PROTOCOL,
            "target": "next scheduled XKRX session close",
            "features": PRICE_FEATURES,
            "refit_origin": ORIGIN,
            "refit_step_sessions": REFIT_STEP,
            "rolling_training_rows": TRAIN_SIZE,
            "purge_sessions": 1,
            "regressors": {"ridge_rolling504": {"alpha": 10},
                           "huber_vol_rolling504": {"epsilon": 1.35, "alpha": .01,
                               "max_iter": 500, "volatility_floor": VOL_FLOOR}},
            "preprocessing": "StandardScaler fit on eligible training rows at each anchor",
            "publication_assumption": "exchange_close_time_for_missing_first_publication; ingestion_is_replay_provenance",
            "recent_window": "latest_30_completed_trading_sessions",
            "decision_objective": "minimum_common_development_mae_points",
            "point_gate": "full_development_and_lower_mae_points_than_all_baselines",
            "probability_gate": "full_development_and_lower_paired_brier_ci95_and_no_worse_log_loss_than_each_baseline",
            "up_definition": "return_strictly_positive; probability_up_at_p_gte_0.5",
            "bootstrap": {"method": "paired_moving_block", "block_days": BOOTSTRAP.bootstrap_block_days,
                          "samples": BOOTSTRAP.bootstrap_samples, "seed": BOOTSTRAP.random_state},
        },
        "markets": markets,
    }
