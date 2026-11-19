"""After-close research ablations for rates, cash flow and margin credit."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from roboquant.index_forecast.accuracy_research import (
    MARKETS,
    MIN_DEVELOPMENT,
    RECENT_SESSIONS,
    REFIT_STEP,
    TRAIN_SIZE,
    _archive_rows,
    _ci,
    _number,
    _prices_on_grid,
    _session_grid,
    _timestamp,
)
from roboquant.index_forecast.dataset import PRICE_FEATURES
from roboquant.index_forecast.postclose import PROTOCOL

TENORS = ("1Y", "3Y", "10Y")
YIELDS = {country: tuple(f"{country}_YIELD_{tenor}" for tenor in TENORS)
          for country in ("KR", "US")}
RATE_SOURCES = {
    "KR": frozenset({"bok_ecos_817Y002", "kofia_ktb_yields", "kofia_freesis_bond_yield"}),
    "US": frozenset({"us_treasury_daily_par_yield_curve"}),
}
FLOW = ("FOREIGN_NET", "INSTITUTION_NET", "RETAIL_NET")
CREDIT = "MARGIN_CREDIT_BALANCE"
FAMILIES = ("kr_rates", "us_rates", "kr_us_spreads", "prior_session_flow", "margin_credit")
MODEL = "price_plus_family"
BASELINES = ("price_only_matched", "linear:price", "flat", "expanding_mean")
COLUMNS = {"market", "series", "observation_date", "value", "unit", "source",
           "received_at", "published_at", "is_final"}
HISTORICAL_YIELD = "historical_dated_yield"
HISTORICAL_CREDIT = "historical_dated_credit"
FREESIS_CREDIT_SOURCE = "kofia_freesis_margin_credit"
OBSERVED_CREDIT_SOURCES = frozenset({FREESIS_CREDIT_SOURCE, "fsc_kofia_margin_credit"})
OBSERVED_FLOW_SOURCE = "krx_market_investor_value"
OBSERVED = "observed"


def _normalise(observations: pd.DataFrame, grid: pd.DataFrame, as_of: pd.Timestamp) -> tuple[list, dict]:
    if observations.empty:
        return [], {}
    if not COLUMNS.issubset(observations.columns):
        raise ValueError("Fundamental observations lack canonical columns")
    days = grid["date"].tolist()
    closes = dict(zip(days, grid["close_at"], strict=True))
    cutoffs = grid["cutoff"].tolist()
    rejected = Counter()
    output = []
    for row in observations.to_dict("records"):
        day = row["observation_date"]
        try:
            valid_day = isinstance(day, str) and datetime.strptime(day, "%Y-%m-%d").date().isoformat() == day
        except ValueError:
            valid_day = False
        if not valid_day:
            rejected["invalid_observation_date"] += 1
            continue
        # Ignore future rows regardless of their contents.
        if day > as_of.tz_convert("Asia/Seoul").date().isoformat():
            continue
        received = _timestamp(row["received_at"])
        if received is None:
            rejected["invalid_received_at"] += 1
            continue
        if received > as_of:
            continue
        published = _timestamp(row["published_at"])
        if pd.notna(row["published_at"]) and published is None:
            rejected["invalid_publication_timestamp"] += 1
            continue
        if published is not None and published > as_of:
            continue
        series, market, unit, source = (row[k] for k in ("series", "market", "unit", "source"))
        if not all(isinstance(item, str) and item for item in (series, market, unit, source)):
            rejected["invalid_identity"] += 1
            continue
        country = next((key for key, names in YIELDS.items() if series in names), None)
        value = _number(row["value"])
        valid_rate = country and market == "GLOBAL" and unit == "percent" and source in RATE_SOURCES[country]
        valid_krw = unit == "KRW" and (
            (market in MARKETS and series in (*FLOW, "MARKET_TURNOVER", CREDIT))
            or (market == "GLOBAL" and series == CREDIT)
        )
        if (not (valid_rate or valid_krw) or value is None
                or (series in ("MARKET_TURNOVER", CREDIT) and value <= 0)):
            rejected["invalid_unit_scope_source_or_value"] += 1
            continue
        policy = row.get("availability_policy")
        if pd.isna(policy):
            policy = OBSERVED
        historical_yield = policy == HISTORICAL_YIELD and valid_rate
        historical_credit = policy == HISTORICAL_CREDIT and series == CREDIT and source == FREESIS_CREDIT_SOURCE
        if not (policy == OBSERVED or historical_yield or historical_credit):
            rejected["unsupported_availability_policy"] += 1
            continue
        final = row["is_final"]
        marked_final = isinstance(final, (bool, np.bool_, int, np.integer)) and final == 1
        unverified_settlement = historical_credit and pd.isna(final)
        if not (marked_final or unverified_settlement):
            rejected["not_final"] += 1
            continue
        if valid_krw and policy == OBSERVED:
            official_source = (source in OBSERVED_CREDIT_SOURCES if series == CREDIT
                               else source == OBSERVED_FLOW_SOURCE)
            if not official_source:
                rejected["unverified_observed_krw_source"] += 1
                continue
        earliest = max(received, published) if published is not None else received
        if valid_krw and day in closes and earliest < closes[day]:
            rejected["final_observation_before_session_close"] += 1
            continue
        comparable = earliest
        if historical_yield or historical_credit:
            # Second later KRX session, including when the observation falls on a local holiday.
            pos = bisect_right(days, day) + 1
            if pos >= len(cutoffs):
                continue
            inferred = cutoffs[pos]
            comparable = max(inferred, published) if published is not None else inferred
            earliest = max(earliest, comparable)
        output.append({
            "market": market, "series": series, "day": day, "source": source, "value": value,
            "available_at": comparable, "prospective_available_at": earliest,
            "received_at": received, "policy": policy, "unverified_settlement": unverified_settlement,
        })
    return output, dict(rejected)


def _first_vintages(observations: list, *, prospective: bool, cutoff: pd.Timestamp) -> dict:
    availability = "prospective_available_at" if prospective else "available_at"
    groups = defaultdict(list)
    for row in observations:
        if row[availability] <= cutoff:
            groups[row["market"], row["series"], row["day"]].append(row)
    selected = {}
    for key, rows in groups.items():
        first = min(r[availability] for r in rows)
        at_first = [r for r in rows if r[availability] == first]
        if len({r["value"] for r in at_first}) == 1:
            row = min(at_first, key=lambda r: (r["policy"] != OBSERVED, r["source"]))
            selected[key] = {**row, "available_at": first}
    return selected


def _evidence(rows: list, features: dict) -> dict:
    return {
        "features": {key: float(value) for key, value in features.items()},
        "observation_dates": sorted({r["day"] for r in rows}),
        "sources": sorted({r["source"] for r in rows}),
        "observation_markets": sorted({r["market"] for r in rows}),
        "available_at": max(r["available_at"] for r in rows).isoformat(),
        "snapshot_received_at": max(r["received_at"] for r in rows).isoformat(),
        "availability_policy": next((r["policy"] for r in rows if r["policy"] != OBSERVED), OBSERVED),
        "source_finality": ("unverified_settlement_snapshot" if any(r["unverified_settlement"] for r in rows)
                            else "marked_final"),
    }


def _daily_features(family: str, market: str, day: str, cutoff: pd.Timestamp,
                    history: dict, lookback: list[str]) -> dict | None:
    def panel(names, date, scope="GLOBAL"):
        rows = [history.get((scope, name, date)) for name in names]
        if all(row is not None and row["available_at"] <= cutoff for row in rows):
            return rows
        return None

    if family == "prior_session_flow":
        rows = panel(FLOW, day, market)
        if rows is not None and len({r["source"] for r in rows}) == 1:
            return _evidence(rows, {f"{key.lower()}_trillion_krw": r["value"] / 1e12
                                    for key, r in zip(FLOW, rows, strict=True)})
        return None
    if family == "margin_credit":
        for scope in (market, "GLOBAL"):
            values = [rows[0] for date in lookback[:15] if (rows := panel((CREDIT,), date, scope))]
            if len(values) >= 2 and values[0]["source"] == values[1]["source"]:
                return _evidence(values[:2], {
                    "log_credit_krw": np.log(values[0]["value"]),
                    "log_credit_change": np.log(values[0]["value"]) - np.log(values[1]["value"]),
                })
        return None

    countries = {"kr_rates": ("KR",), "us_rates": ("US",), "kr_us_spreads": ("KR", "US")}[family]
    names = tuple(name for country in countries for name in YIELDS[country])
    panels = []
    for date in lookback[:11]:
        rows = panel(names, date)
        if rows and all(len({r["source"] for r in rows[i:i + 3]}) == 1
                        for i in range(0, len(rows), 3)):
            panels.append(rows)
            if len(panels) == (1 if len(countries) == 2 else 2):
                break
    if len(countries) == 2:
        if not panels:
            return None
        rows = panels[0]
        values = [(left["value"] - right["value"]) * 100
                  for left, right in zip(rows[:3], rows[3:], strict=True)]
        return _evidence(rows, {f"kr_minus_us_{tenor.lower()}_bps": val
                                for tenor, val in zip(TENORS, values, strict=True)})
    if len(panels) < 2 or panels[0][0]["source"] != panels[1][0]["source"]:
        return None
    current, previous = panels
    vals = [r["value"] for r in current]
    features = {f"change_{tenor.lower()}_bps": (r["value"] - prior["value"]) * 100
                for tenor, r, prior in zip(TENORS, current, previous, strict=True)}
    features.update(curve_3y_minus_1y_bps=(vals[1] - vals[0]) * 100,
                    curve_10y_minus_1y_bps=(vals[2] - vals[0]) * 100)
    return _evidence(current + previous, features)


def _fit_models(frame: pd.DataFrame, features: dict, anchor: int) -> tuple | None:
    if not frame.at[anchor, "features_ready"]:
        return None
    cutoff = frame.at[anchor, "cutoff"]
    earlier = frame.iloc[:max(0, anchor - 1)]
    train = earlier.loc[
        earlier.index.isin(features)
        & earlier["features_ready"]
        & np.isfinite(earlier["label_return"])
        & (earlier["label_available_at"] <= cutoff)
    ].tail(TRAIN_SIZE)
    if len(train) < TRAIN_SIZE:
        return None
    x = train[PRICE_FEATURES].to_numpy()
    extra = np.array([list(features[i]["features"].values()) for i in train.index])
    y = train["label_return"].to_numpy()
    baseline = make_pipeline(StandardScaler(), Ridge(alpha=10)).fit(x, y)
    challenger = make_pipeline(StandardScaler(), Ridge(alpha=10)).fit(np.column_stack((x, extra)), y)
    return baseline, challenger, {
        "fit_asof": frame.at[anchor, "date"], "fit_cutoff": cutoff.isoformat(),
        "fit_provenance": "retrospective_reconstruction",
        "model_training_clock": "simulated_historical_cutoffs",
        "train_first_asof": train.iloc[0]["date"], "train_last_asof": train.iloc[-1]["date"],
        "train_latest_label_available_at": train["label_available_at"].max().isoformat(),
        "train_n": len(train),
    }


def _predict(frame: pd.DataFrame, features: dict, i: int, fitted: tuple) -> dict:
    x = frame.loc[[i], PRICE_FEATURES].to_numpy()
    extra = list(features["features"].values())
    return {
        "price_only_matched": float(fitted[0].predict(x)[0]),
        MODEL: float(fitted[1].predict(np.column_stack((x, [extra])))[0]),
    }


def _metrics(rows: list, *, bootstrap: bool = True) -> dict:
    names = (MODEL, *BASELINES)
    if not rows:
        return {name: {"n": 0} for name in names}
    y = np.array([r["actual_return"] for r in rows])
    origin = np.array([r["origin_close"] for r in rows])
    returns = {name: np.array([r["predictions"][name] for r in rows]) for name in names}
    errors = {name: abs(values - y) for name, values in returns.items()}
    scores = {
        name: {"n": len(rows), "mae_points": float(np.mean(errors[name] * origin)),
               "mae_return_pp": float(np.mean(errors[name]) * 100),
               "direction_hit": float(np.mean((returns[name] > 0) == (y > 0)))}
        for name in names
    }
    if bootstrap:
        scores[MODEL]["paired_mae_points_difference_ci95"] = {
            name: _ci((errors[MODEL] - errors[name]) * origin) for name in BASELINES
        }
    return scores


def _window(indices: list, features: dict, scored: dict, frame: pd.DataFrame,
            freeze: pd.Timestamp | None = None) -> dict:
    selected = [scored[i] for i in indices if i in scored and
                (freeze is None or _timestamp(scored[i]["label_available_at"]) <= freeze)]
    slices = {}
    for key in ("volatility_regime", "trend_regime"):
        groups = defaultdict(list)
        for row in selected:
            groups[row[key]].append(row)
        slices[key] = {group: _metrics(rows, bootstrap=False) for group, rows in groups.items()}
    return {
        "expected_n": len(indices),
        "features_available_n": sum(i in features for i in indices),
        "scored_n": len(selected),
        "target_dates": [r["target_date"] for r in selected],
        "expected_target_dates": [frame.at[i + 1, "date"] for i in indices],
        "models": _metrics(selected),
        "regimes": slices,
    }


def _next_forecast(frame: pd.DataFrame, features: dict, i: int, fitted: tuple,
                   as_of: pd.Timestamp) -> dict:
    points = _predict(frame, features, i, fitted)
    close = float(frame.at[i, "close"])
    return {
        "status": "research_only", "production_weight": 0.,
        "asof_date": frame.at[i, "date"], "target_date": frame.at[i + 1, "date"],
        "prediction_cutoff": as_of.isoformat(), "target_close_at": frame.at[i + 1, "close_at"].isoformat(),
        "origin_close": close, "origin_close_provenance": frame.at[i, "price_status"],
        "origin_close_available_at": frame.at[i, "price_available_at"].isoformat(),
        "expected_index": close * (1 + points[MODEL]),
        "price_only_matched_expected_index": close * (1 + points["price_only_matched"]),
        "expected_return": points[MODEL],
        "feature_evidence": features, **fitted[2],
    }


def _family_result(family: str, market: str, frame: pd.DataFrame, matched: dict, windows: dict,
                   history: dict, real: dict, lookbacks: list, latest: int,
                   as_of: pd.Timestamp, comparator_selected: bool) -> dict:
    features = {}
    for i in range(latest + 1):
        day, cutoff = frame.at[i, "date"], frame.at[i, "cutoff"]
        if cutoff <= as_of:
            sample = _daily_features(family, market, day, cutoff, history, lookbacks[i])
            if sample is not None:
                features[i] = sample
    fits = {}

    def models(i):
        anchor = i // REFIT_STEP * REFIT_STEP
        if anchor not in fits:
            fits[anchor] = _fit_models(frame, features, anchor)
        return fits[anchor]

    scored = {}
    for i in sorted(set(j for indices, _ in windows.values() for j in indices)):
        if i not in features or i not in matched or not frame.at[i, "features_ready"]:
            continue
        record, actual, maturity, _ = matched[i]
        archived = _number(record.get("expected_return"))
        mean = _number(record.get("baselines", {}).get("mean", {}).get("expected_return"))
        fit = models(i)
        if fit is None or archived is None or mean is None:
            continue
        predictions = _predict(frame, features[i], i, fit)
        predictions.update({"linear:price": archived, "flat": 0., "expanding_mean": mean})
        scored[i] = {
            "asof_date": frame.at[i, "date"], "target_date": frame.at[i + 1, "date"],
            "actual_return": actual, "origin_close": float(frame.at[i, "close"]),
            "label_available_at": maturity.isoformat(),
            "volatility_regime": record.get("volatility_regime") or "unknown",
            "trend_regime": record.get("trend_regime") or "unknown",
            "feature_evidence": features[i], "predictions": predictions, **fit[2],
        }
    summary = {name: _window(indices, features, scored, frame, freeze)
               for name, (indices, freeze) in windows.items()}
    dev = summary["development"]
    reasons = []
    if not comparator_selected:
        reasons.append("archive_comparator_is_not_selected_linear_price")
    if dev["expected_n"] < MIN_DEVELOPMENT or dev["scored_n"] != dev["expected_n"]:
        reasons.append("insufficient_complete_common_development")
    else:
        candidate = dev["models"][MODEL]
        for key in BASELINES:
            if candidate["mae_points"] >= dev["models"][key]["mae_points"]:
                reasons.append(f"development_mae_did_not_beat_{key}")
        ci = candidate["paired_mae_points_difference_ci95"]["price_only_matched"]
        if ci is None or ci["high"] >= 0:
            reasons.append("incremental_price_mae_improvement_not_significant")
    next_forecast = None
    latest_features = features.get(latest)
    if (latest >= 0 and latest + 1 < len(frame) and
            frame.at[latest, "cutoff"] <= as_of < frame.at[latest + 1, "open_at"] and
            frame.at[latest, "features_ready"]):
        # Historical date availability may train the replay; a new forecast also
        # requires the actual receipt on this invocation's point-in-time cutoff.
        prospective = _daily_features(family, market, frame.at[latest, "date"],
                                      as_of, real, lookbacks[latest])
        if prospective is not None and (fit := models(latest)) is not None:
            next_forecast = _next_forecast(frame, prospective, latest, fit, as_of)
    return {
        "status": "evaluated" if scored else "not_ready",
        "latest_features": latest_features, "next_forecast": next_forecast,
        "windows": summary, "observations": list(scored.values()),
        "development_gate": {"passed": not reasons, "reasons": reasons},
    }


def _market_report(market: str, prices: pd.DataFrame, evaluation: dict, grid: pd.DataFrame,
                   history: dict, real: dict, lookbacks: list, latest: int,
                   as_of: pd.Timestamp) -> dict:
    frame = _prices_on_grid(prices, grid, as_of)
    cutoffs = dict(zip(frame["date"], frame["cutoff"], strict=True))
    start, frozen = evaluation.get("development_start"), evaluation.get("selection_frozen_before")
    selection = evaluation.get("selected") or {}
    terminal = {r.get("target_date") for r in evaluation.get("observations", [])}
    records = evaluation.get("candidate_observations", {}).get("linear:price", [])
    archive_ok = (
        start in cutoffs and frozen in cutoffs and start < frozen
        and evaluation.get("settings", {}).get("issuance_protocol") == PROTOCOL
    )
    matched, _ = _archive_rows(records, frame, as_of) if archive_ok else ({}, {})
    recent = set(range(max(0, latest - RECENT_SESSIONS), latest))
    development, older = [], []
    if archive_ok:
        development = [i for i in range(latest)
                       if i not in recent and start <= frame.at[i, "date"] < frozen]
        older = [i for i in range(latest)
                 if i not in recent and frame.at[i, "date"] >= frozen and frame.at[i + 1, "date"] in terminal]
    windows = {
        "development": (development, cutoffs.get(frozen)),
        "older_terminal_exploratory": (older, None),
        "last30_descriptive": (sorted(recent), None),
    }
    compare_selected = selection.get("family") == "linear" and selection.get("group") == "price"
    families = {family: _family_result(family, market, frame, matched, windows,
                                      history, real, lookbacks, latest, as_of, compare_selected)
                for family in FAMILIES}
    candidates = [name for name, result in families.items() if result["development_gate"]["passed"]]
    return {
        "families": families,
        "selected": min(candidates,
                        key=lambda name: families[name]["windows"]["development"]["models"][MODEL]["mae_points"])
                    if candidates else None,
        "forward_gate": {"status": "not_started", "new_matched_actuals": 0, "required_new_matched_actuals": 252},
        "production_weight": 0.,
    }


def evaluate_fundamental_research(prices: pd.DataFrame, archive: dict, observations: pd.DataFrame,
                                  *, sessions: pd.DataFrame, as_of: datetime) -> dict:
    """Measure incremental next-close performance with independent matched Ridge fits."""
    now = _timestamp(as_of)
    if now is None:
        raise ValueError("as_of must be timezone-aware")
    if not {"index_code", "session_date", "close"}.issubset(prices.columns):
        raise ValueError("prices must contain index_code, session_date and close")
    grid = _session_grid(sessions)
    prepared, rejected = _normalise(observations, grid, now)
    history = _first_vintages(prepared, prospective=False, cutoff=now)
    real = _first_vintages(prepared, prospective=True, cutoff=now)
    latest = int((grid["close_at"] <= now).sum()) - 1
    lookbacks = [[(day - timedelta(days=offset)).date().isoformat() for offset in range(15)]
                 for day in pd.to_datetime(grid["date"])]
    entries = defaultdict(list)
    for item in archive.get("markets", []):
        entries[item.get("market")].append(item.get("evaluation", {}))
    dates = pd.to_datetime(prices["session_date"], format="%Y-%m-%d", errors="coerce")
    normal_prices = prices.assign(session_date=dates.dt.strftime("%Y-%m-%d"))
    markets = {}
    for market in MARKETS:
        evaluation = entries[market][0] if len(entries[market]) == 1 else {}
        markets[market] = _market_report(
            market, normal_prices.loc[normal_prices["index_code"].eq(market)], evaluation, grid,
            history, real, lookbacks, latest, now,
        )
    return {
        "schema_version": "robo_stock_fundamental_research_v1", "as_of": now.isoformat(),
        "production_weight": 0.,
        "input_rejections": rejected,
        "admissible_observations_by_series": dict(Counter(series for _, series, _ in history)),
        "protocol": {
            "issuance": PROTOCOL, "target": "next scheduled KRX session close",
            "historical_dated_yield_policy": "approved_sources_second_scheduled_krx_session_after_observation",
            "historical_dated_credit_policy": "kofia_freesis_second_scheduled_krx_session_after_settlement_date",
            "historical_dated_credit_finality": "source_finality_may_be_unknown; expressly_pending_observations_excluded",
            "observed_krw_policy": "official_krx_or_kofia_fsc_source_and_explicit_finality_required",
            "observed_policy": "maximum_of_received_at_and_present_verified_published_at",
            "rate_lookback_calendar_days": 10, "credit_lookback_calendar_days": 14,
            "flow_alignment": "exact_prediction_origin_session; market net KRW amounts scaled to trillion",
            "models": "ridge10 with train-only StandardScaler; identical eligible dates for price-only and candidate",
            "anchor_stride_sessions": REFIT_STEP, "training_rows": TRAIN_SIZE, "purge_sessions": 1,
            "decision": "development_only; minimum 252 common rows; beat all baselines and paired incremental MAE CI",
        },
        "markets": markets,
    }
