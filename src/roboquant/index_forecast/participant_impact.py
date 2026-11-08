"""Read-only participant observations and fixed PIT feature candidates.

Associations are descriptive, never causal effects, learned holdings, or a model
selection signal. Forecast uplift belongs to the caller's prior-only WFO gates.
The subproject retains its standard-library-only dependency contract.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from roboquant.index_forecast.reliable_inputs import eligible_stock_price_revisions

SCHEMA_VERSION = "smartmoney_bridge_v1"
ACTORS = ("foreign", "institution", "pension")
ACTOR_ALIASES = {"FOREIGN": "foreign", "외국인": "foreign", "INSTITUTION": "institution",
                 "기관": "institution", "기관합계": "institution", "PENSION": "pension",
                 "연기금": "pension", "연기금등": "pension"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def write_participant_bridge(path: str | Path, observations: list[dict] | pd.DataFrame,
                             metadata: dict | None = None) -> dict:
    """Write a versioned JSON snapshot; the manifest hashes its exact observations.

    Rewriting identical content is a no-op. An existing snapshot cannot be changed
    in place: select a new path for a new revision/export.
    """
    rows = observations.to_dict("records") if isinstance(observations, pd.DataFrame) else list(observations)
    # Round-trip also rejects NaN/Infinity, which cannot be missing observations.
    rows = json.loads(_json(rows))
    manifest = {"schema_version": SCHEMA_VERSION, "observation_sha256": _digest(rows),
                "row_count": len(rows), "metadata": dict(metadata or {})}
    payload = {"schema_version": SCHEMA_VERSION, "observations": rows, "manifest": manifest}
    destination = Path(path)
    encoded = _json(payload) + "\n"
    if destination.exists():
        if destination.read_text(encoding="utf-8") != encoded:
            raise ValueError("immutable participant bridge content conflict")
        return manifest
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(encoded, encoding="utf-8")
    return manifest


def _load_bridge(path: str | Path) -> tuple[list[dict], dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported participant bridge schema_version")
    rows, manifest = payload.get("observations"), payload.get("manifest", {})
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("participant observations must be records")
    if manifest.get("observation_sha256") != _digest(rows) or manifest.get("row_count") != len(rows):
        raise ValueError("participant bridge observation hash/count mismatch")
    return rows, {**manifest, "input_kind": "versioned_json", "input_path": str(Path(path))}


def _read_sqlite(path: str | Path) -> tuple[list[dict], dict]:
    """Read actual subproject observations without importing its write-enabled DB API."""
    source = Path(path)
    if not source.is_file():
        return [], {"schema_version": SCHEMA_VERSION, "input_kind": "missing_sqlite",
                    "input_path": str(source), "row_count": 0, "metadata_gaps": ["source_missing"]}
    rows: list[dict] = []
    tables = {"kr_investor_flows": "flow", "kr_foreign_ownership": "ownership", "prices": "price"}
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, kind in tables.items():
            if table not in existing:
                continue
            for original in conn.execute(f'SELECT * FROM "{table}"'):
                item = dict(original)
                if kind == "price" and item.get("market") != "KR":
                    continue
                row = {**item, "record_type": kind, "scope": "stock", "market": "KR",
                       "observation_date": item.get("trade_date"), "source": item.get("source_code"),
                       "investor_type": item.get("investor_type", "FOREIGN" if kind == "ownership" else None),
                       "unit": "percent" if kind == "ownership" else "KRW",
                       "quantity_unit": "shares", "turnover": item.get("traded_value"),
                       "is_final": None, "price_adjustment": "unknown",
                       "revision": _digest(item), "revision_basis": "current_row_hash_only"}
                rows.append(row)
    return rows, {"schema_version": SCHEMA_VERSION, "input_kind": "read_only_sub_sqlite",
                  "input_path": str(source), "row_count": len(rows), "observation_sha256": _digest(rows),
                  "metadata_gaps": ["source_finality_not_recorded", "source_revision_history_unavailable",
                                    "source_price_adjustment_not_recorded"]}


def _stamp(value: Any) -> pd.Timestamp:
    """No implicit KST/UTC conversion for a timestamp without an explicit offset."""
    try:
        result = pd.Timestamp(value)
        return result.tz_convert("UTC") if not pd.isna(result) and result.tzinfo is not None else pd.NaT
    except (TypeError, ValueError, OverflowError):
        return pd.NaT


def _day(value: Any) -> str | None:
    try:
        parsed = pd.Timestamp(value)
        return parsed.date().isoformat() if not pd.isna(parsed) else None
    except (ValueError, TypeError):
        return None


def _normalise(rows: list[dict]) -> tuple[pd.DataFrame, int]:
    normalised, fallbacks = [], 0
    for original in rows:
        row = dict(original)
        available, ingested = _stamp(row.get("available_at")), _stamp(row.get("ingested_at"))
        if pd.isna(available) and not pd.isna(ingested):
            available = ingested
            fallbacks += 1
            row["availability_basis"] = "actual_first_seen"
        row.update(_date=_day(row.get("observation_date")), _available=available, _ingested=ingested,
                   _actor=ACTOR_ALIASES.get(str(row.get("investor_type", "")).upper()),
                   _final=str(row.get("is_final", "")).lower() in {"true", "1", "yes"})
        for column in ("net_value", "net_qty", "turnover", "close", "ownership_pct"):
            try:
                value = float(row.get(column))
                row[column] = value if np.isfinite(value) else np.nan
            except (TypeError, ValueError):
                row[column] = np.nan
        normalised.append(row)
    frame = pd.DataFrame(normalised)
    if not frame.empty:
        for column in ("_available", "_ingested"):
            # An all-NaT series otherwise becomes timezone-naive in pandas.
            frame[column] = pd.to_datetime(frame[column], utc=True)
    return frame, fallbacks


def _eligible(data: pd.DataFrame, day: str, cutoff: pd.Timestamp, kind: str) -> pd.DataFrame:
    if data.empty or pd.isna(cutoff):
        return pd.DataFrame()
    required = {"record_type", "scope", "market", "symbol", "source", "source_url", "revision", "unit"}
    if not required.issubset(data):
        return pd.DataFrame()
    valid = ((data["record_type"] == kind) & (data["_date"] == day) &
             data["_final"] & (data["_available"] <= cutoff) & (data["_ingested"] <= cutoff) &
             data["source"].fillna("").astype(str).ne("") &
             data["source_url"].fillna("").astype(str).ne("") &
             data["revision"].fillna("").astype(str).ne(""))
    frame = data.loc[valid].copy()
    if frame.empty:
        return frame
    # Latest eligible revision within a source, then fixed primary-source priority.
    frame["_priority"] = frame["source"].map({"KRX_API": 3, "KRX_CSV": 2, "MANUAL": 1}).fillna(0)
    keys = ["scope", "market", "symbol", "_actor"]
    return frame.sort_values(["_priority", "_available", "revision"], kind="stable").drop_duplicates(keys, keep="last")


def _members(membership: pd.DataFrame, market: str, day: str, cutoff: pd.Timestamp, count: int) -> pd.DataFrame:
    required = {"snapshot_date", "market", "symbol", "market_cap", "security_type", "available_at"}
    if membership.empty or not required.issubset(membership):
        return pd.DataFrame()
    frame = membership.copy()
    frame["_available"] = pd.to_datetime(frame["available_at"].map(_stamp), utc=True)
    frame["_date"] = frame["snapshot_date"].map(_day)
    # Explicit dated cross-sections only. An old/current list is not a daily panel.
    frame = frame.loc[(frame["market"] == market) & (frame["_date"] == day) & (frame["_available"] <= cutoff)]
    frame = frame.sort_values("_available", kind="stable").drop_duplicates("symbol", keep="last")
    common = frame["security_type"].astype(str).str.upper().isin({"COMMON", "COMMON_STOCK", "보통주"})
    active = pd.Series(False, index=frame.index)
    for column in ("is_suspended", "suspended"):
        if column in frame:
            active |= frame[column].astype(str).str.lower().isin({"false", "0", "no"})
    if "trading_status" in frame:
        active |= frame["trading_status"].astype(str).str.upper().isin({"ACTIVE", "NORMAL", "정상"})
        common &= frame["trading_status"].astype(str).str.upper().isin({"ACTIVE", "NORMAL", "정상"})
    for column in ("is_suspended", "suspended", "is_etf", "is_etn", "is_spac", "is_preferred"):
        if column in frame:
            common &= ~frame[column].astype(str).str.lower().isin({"true", "1", "yes"})
    if "name" in frame:
        common &= ~frame["name"].astype(str).str.contains("SPAC|스팩|ETF|ETN", case=False, regex=True)
    frame["market_cap"] = pd.to_numeric(frame["market_cap"], errors="coerce")
    frame = frame.loc[common & active & frame["market_cap"].gt(0)]
    selected = frame.sort_values(["market_cap", "symbol"], ascending=[False, True]).head(count)
    return selected if len(selected) == count else pd.DataFrame()


def _prices(prices: pd.DataFrame, symbols: list[str], day: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    required = {"date", "symbol", "close", "available_at", "price_adjustment", "corporate_action_status"}
    if prices.empty or not required.issubset(prices):
        return pd.DataFrame()
    frame = eligible_stock_price_revisions(prices, cutoff, timestamp_parser=_stamp)
    if frame.empty:
        return pd.DataFrame()
    frame["_date"] = frame["date"].map(_day)
    frame["_available"] = pd.to_datetime(frame["available_at"].map(_stamp), utc=True)
    frame = frame.loc[(frame["_date"] == day) & (frame["_available"] <= cutoff) & frame["symbol"].isin(symbols)]
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    valid = (frame["close"].gt(0) & np.isfinite(frame["close"]) &
             frame["price_adjustment"].isin({"unadjusted", "split_adjusted", "total_return_adjusted"}) &
             frame["corporate_action_status"].isin({"verified_none", "verified_adjusted"}))
    frame = frame.loc[valid]
    if set(frame["symbol"]) != set(symbols) or frame["price_adjustment"].nunique() != 1:
        return pd.DataFrame()
    return frame


def _daily_stock(data: pd.DataFrame, membership: pd.DataFrame, prices: pd.DataFrame, market: str,
                 day: str, cutoff: pd.Timestamp, count: int, actor: str) -> dict | None:
    selected = _members(membership, market, day, cutoff, count)
    if selected.empty:
        return None
    symbols = selected["symbol"].tolist()
    panel = _prices(prices, symbols, day, cutoff)
    if panel.empty:
        return None
    flow = _eligible(data, day, cutoff, "flow")
    if flow.empty:
        return None
    flow = flow.loc[(flow["scope"] == "stock") & flow["market"].isin({market, "KR"}) &
                    (flow["_actor"] == actor) & flow["symbol"].isin(symbols) & (flow["unit"] == "KRW")]
    if set(flow["symbol"]) != set(symbols) or flow["net_value"].isna().any():
        return None
    # Turnover must describe the same actual stocks, never a whole-market denominator.
    if flow["turnover"].isna().any() or (flow["turnover"] <= 0).any():
        if "traded_value" not in panel:
            return None
        values = pd.to_numeric(panel["traded_value"], errors="coerce")
        if values.isna().any() or (values <= 0).any():
            return None
        turnover = float(values.sum())
    else:
        turnover = float(flow["turnover"].sum())
    return {"net": float(flow["net_value"].sum()), "turnover": turnover, "symbols": symbols}


def _ownership_delta(data: pd.DataFrame, symbols: list[str], day: str, old_day: str,
                     cutoff: pd.Timestamp, market: str) -> float | None:
    values = []
    for date in (old_day, day):
        own = _eligible(data, date, cutoff, "ownership")
        if own.empty:
            return None
        own = own.loc[(own["scope"] == "stock") & own["market"].isin({market, "KR"}) &
                      (own["_actor"] == "foreign") & own["symbol"].isin(symbols) & (own["unit"] == "percent")]
        if set(own["symbol"]) != set(symbols) or not own["ownership_pct"].between(0, 100).all():
            return None
        values.append(own.set_index("symbol")["ownership_pct"].reindex(symbols).to_numpy(dtype=float))
    return float(np.mean(values[1] - values[0]))


def _slope(x: np.ndarray, y: np.ndarray, controls: np.ndarray) -> float | None:
    nuisance = np.column_stack([np.ones(len(x)), controls])
    residual_x = x - nuisance @ np.linalg.lstsq(nuisance, x, rcond=None)[0]
    denominator = float(residual_x @ residual_x)
    if denominator <= 1e-18:
        return None
    residual_y = y - nuisance @ np.linalg.lstsq(nuisance, y, rcond=None)[0]
    return float(residual_x @ residual_y / denominator)


def _association(frame: pd.DataFrame, feature: str, outcome: str, controls: list[str]) -> dict:
    columns = [feature, outcome, *controls]
    sample = frame[columns].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    n = len(sample)
    result = {"status": "insufficient_data", "n": n, "correlation": None,
              "conditional_slope": None, "slope_ci95": None,
              "controls": controls, "uncertainty_method": "circular_20_session_block_bootstrap_200",
              "interpretation": "observational association; no causal or OOS forecast-uplift claim"}
    if n < 20:
        return result
    x, y = sample[feature].to_numpy(), sample[outcome].to_numpy()
    nuisance = sample[controls].to_numpy() if controls else np.empty((n, 0))
    slope = _slope(x, y, nuisance)
    if slope is None or np.std(y) <= 1e-15:
        return {**result, "status": "unidentifiable"}
    rng = np.random.default_rng(42)
    estimates = []
    for _ in range(200):
        starts = rng.integers(0, n, size=(n + 19) // 20)
        positions = ((starts[:, None] + np.arange(20)) % n).ravel()[:n]
        estimate = _slope(x[positions], y[positions], nuisance[positions])
        if estimate is not None and np.isfinite(estimate):
            estimates.append(estimate)
    return {**result, "status": "observational_only", "conditional_slope": slope,
            "correlation": float(np.corrcoef(x, y)[0, 1]),
            "slope_ci95": [float(v) for v in np.quantile(estimates, [.025, .975])] if estimates else None}


def _influence(features: pd.DataFrame, groups: dict[str, list[str]], history: pd.DataFrame,
               cutoffs: dict[str, pd.Timestamp]) -> dict:
    required = {"asof_date", "target_date", "label_return", "label_available_at", "return_1d"}
    if features.empty or history.empty or not required.issubset(history):
        return {}
    base = history.copy()
    base["asof_date"] = base["asof_date"].map(_day)
    base["target_date"] = base["target_date"].map(_day)
    base = base.sort_values("asof_date").drop_duplicates("asof_date", keep="last")
    base["_next_session"] = base["asof_date"].shift(-1)
    latest_cutoff = max((v for v in cutoffs.values() if not pd.isna(v)), default=pd.NaT)
    mature = pd.to_datetime(base["label_available_at"].map(_stamp), utc=True) <= latest_cutoff
    next_session = (base["target_date"] == base["_next_session"]) & (base["target_date"] > base["asof_date"])
    base.loc[~(mature & next_session), "label_return"] = np.nan
    joined = features.merge(base, on="asof_date", how="inner", validate="one_to_one")
    controls = [c for c in ("return_1d", "return_5d") if c in joined]
    output = {}
    for group, columns in groups.items():
        output[group] = [{"feature": column,
                          "same_session": _association(joined, column, "return_1d", []),
                          "next_session": _association(joined, column, "label_return", controls)}
                         for column in columns]
    return output


def _evidence_cards(coverage: dict, manifest: dict) -> list[dict]:
    values = [
        ("economic_hypothesis", "cash demand pressure and ownership-change agreement may explain incremental variation"),
        ("data_acquisition", "verified official CSV/JSON bridge or read-only subproject observations"),
        ("actual_publication_timestamp", "per-row available_at; unknown release uses actual first-seen; not invented"),
        ("cost", "no paid acquisition; current source rights and operational cost unverified"),
        ("missing_rate", "100% observed market inputs missing" if not sum(coverage.values()) else "see source_coverage and eligible feature rows"),
        ("history_length", "see source_coverage and eligible date ranges; source row count is not eligible PIT history"),
        ("leakage_risk", "release/revision delay, current universe, corporate actions and full-history selection forbidden"),
        ("baseline_gain", "unmeasured; associations are not forecast uplift"),
        ("regime_performance", "main prior-only WFO required; descriptive slices do not establish robustness"),
        ("ablation_result", "unmeasured until aligned price-only vs raw vs raw+derived WFO"),
    ]
    return [{"field": field, "value": value, "source_snapshot": manifest.get("observation_sha256")}
            for field, value in values]


def build_participant_impact_inputs(*, market: str, asof_cutoffs: dict,
                                   sqlite_path: str | Path | None = None,
                                   bridge_path: str | Path | None = None,
                                   membership: pd.DataFrame | None = None,
                                   stock_prices: pd.DataFrame | None = None,
                                   index_history: pd.DataFrame | None = None,
                                   target_count: int | None = None) -> dict:
    """Return fixed actor-independent features plus an honest descriptive report.

    The caller alone may run prior-only WFO, calibration and acceptance gates. No
    association coefficient here may select columns, actors, models or weights.
    """
    if market not in {"KOSPI", "KOSDAQ"}:
        raise ValueError("participant impact market must be KOSPI or KOSDAQ")
    if bridge_path is not None:
        rows, manifest = _load_bridge(bridge_path)
    elif sqlite_path is not None:
        rows, manifest = _read_sqlite(sqlite_path)
    else:
        rows, manifest = [], {"schema_version": SCHEMA_VERSION, "row_count": 0,
                              "input_kind": "missing", "metadata_gaps": ["source_missing"]}
    data, fallback_count = _normalise(rows)
    manifest["availability_fallback_rows"] = fallback_count
    coverage = {kind: sum(r.get("record_type") == kind for r in rows) for kind in ("flow", "ownership", "price")}
    members = membership if membership is not None else pd.DataFrame()
    prices = stock_prices if stock_prices is not None else pd.DataFrame()
    if stock_prices is None and not data.empty:
        prices = data.loc[(data["record_type"] == "price") & data["_final"]].copy()
        if not prices.empty:
            prices["date"] = prices["_date"]
    history = index_history if index_history is not None else pd.DataFrame()
    count = int(target_count if target_count is not None else (100 if market == "KOSPI" else 50))
    if count <= 0:
        raise ValueError("target_count must be positive")
    cuts = {str(_day(day)): _stamp(cutoff) for day, cutoff in asof_cutoffs.items() if _day(day)}
    days = sorted(cuts)
    output: dict[str, dict] = {}
    groups: dict[str, list[str]] = {}
    active_actors = set()
    for scope in ("market", "stock"):
        for actor in ACTORS:
            daily: dict[str, dict] = {}
            prefix = f"participant_{scope}_{actor}"
            raw_columns = [f"{prefix}_net_to_turnover_{window}d" for window in (1, 5, 20)]
            for day in days:
                if scope == "stock":
                    value = _daily_stock(data, members, prices, market, day, cuts[day], count, actor)
                else:
                    flow = _eligible(data, day, cuts[day], "flow")
                    if not flow.empty:
                        flow = flow.loc[(flow["scope"] == "market_total") & (flow["market"] == market) &
                                        flow["symbol"].isna() & (flow["_actor"] == actor) & (flow["unit"] == "KRW")]
                        flow = flow.loc[flow["net_value"].notna() & flow["turnover"].gt(0)]
                    value = ({"net": float(flow.iloc[-1]["net_value"]), "turnover": float(flow.iloc[-1]["turnover"])}
                             if not flow.empty else None)
                if value is not None:
                    daily[day] = value
            raw_found = position_found = False
            derived_columns = [f"{prefix}_ownership_change_pp_20d", f"{prefix}_positive_day_share_20d"]
            for i, day in enumerate(days):
                window = days[max(0, i - 19):i + 1]
                if len(window) != 20 or any(date not in daily for date in window):
                    continue
                row = output.setdefault(day, {"asof_date": day})
                for width, column in zip((1, 5, 20), raw_columns, strict=True):
                    selected = [daily[date] for date in window[-width:]]
                    row[column] = sum(v["net"] for v in selected) / sum(v["turnover"] for v in selected)
                raw_found = True
                if scope == "stock" and actor == "foreign" and i >= 20:
                    symbols = daily[day]["symbols"]
                    # Comparable same-stock endpoints, with verified prices/actions at both ends.
                    old_day = days[i - 20]
                    if _prices(prices, symbols, old_day, cuts[day]).empty:
                        continue
                    delta = _ownership_delta(data, symbols, day, old_day, cuts[day], market)
                    if delta is not None:
                        row[derived_columns[0]] = delta
                        row[derived_columns[1]] = sum(daily[date]["net"] > 0 for date in window) / 20
                        position_found = True
            if raw_found:
                groups[prefix + "_raw"] = raw_columns
                active_actors.add(actor)
            if position_found:
                groups[prefix + "_position"] = [*raw_columns, *derived_columns]
    features = pd.DataFrame(list(output.values()))
    if features.empty:
        features = pd.DataFrame(columns=["asof_date"])
    else:
        features = features.sort_values("asof_date").reset_index(drop=True)
    limitations = ["not_causal", "associations_never_select_features_or_weights", "forecast_uplift_unmeasured",
                   "public_aggregate_flows_do_not_identify_private_positions", "fixed_feature_candidates_only",
                   *manifest.get("metadata_gaps", [])]
    if members.empty or prices.empty:
        limitations.append("stock_universe_unverified")
    observed = {"source_coverage": coverage,
                "latest_eligible_features": (json.loads(features.iloc[-1].to_json()) if not features.empty else {})}
    position_evidence = {"observed": observed,
                         "inferred": "cash_pressure_evidence_only" if groups else "inconclusive",
                         "unverified": ["private_account_holdings", "average_entry_price", "same_account_hedges",
                                        "institution_or_pension_ownership_not_observed"],
                         "missing": [f"{actor}_eligible_observations" for actor in ACTORS if actor not in active_actors]}
    report = {"schema_version": "participant_impact_report_v1",
              "status": "eligible_research_features" if groups else "insufficient_data",
              "market": market, "source_coverage": coverage, "eligible_feature_rows": len(features),
              "active_feature_groups": groups, "missing_participants": [p for p in ACTORS if p not in active_actors],
              "forecast_uplift": "unmeasured", "causal_effect": "not_identified",
              "holdings": "not_inferred_from_net_trades", "limitations": limitations,
              "position_evidence": position_evidence,
              "influence": _influence(features, groups, history, cuts),
              "ablation_contract": "same eligible prior-only folds: price -> price+raw -> price+raw+position; unavailable groups excluded",
              "evidence_cards": _evidence_cards(coverage, manifest),
              "feature_metadata": {group: {"scope": "stock_subset" if "_stock_" in group else "market_total",
                                           "hypothesis": "observed cash pressure; derived ownership is corroborating evidence",
                                           "units": "dimensionless net/turnover, pp ownership change, fraction positive days",
                                           "timing": "only observations available at each asof cutoff; fixed columns"}
                                   for group in groups}}
    return {"features": features, "feature_groups": groups, "report": report, "bridge_manifest": manifest}
