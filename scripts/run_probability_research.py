#!/usr/bin/env python
"""Reproduce the pre-registered p_up candidate study, or log one premarket shadow forecast.

Research only. Never writes under docs/, never changes the incumbent forecast,
and keeps production_weight at 0. SQLite sources are opened read-only.

  research: development OOF only (as-of < --terminal-start); candidates C1..C6,
            ablations, Holm-corrected gate, slices and diagnostics.
  shadow:   one premarket (08:00 KST cutoff) KOSPI/KOSDAQ shadow p_up appended
            to a local JSONL ledger for forward confirmation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.index_forecast.dataset import PRICE_FEATURES  # noqa: E402
from roboquant.index_forecast.postclose import historical_cutoff  # noqa: E402
from roboquant.index_forecast.probability_research import (  # noqa: E402
    CREDIT_FEATURES,
    OHLC_FEATURES,
    RATE_FEATURES,
    REGIME_FEATURES,
    abstain_curve,
    add_ohlc_features,
    block_bootstrap,
    forecast_premarket_us_shadow,
    holm,
    lagged_series_features,
    probability_metrics,
    reliability,
    shadow_spec_hash,
    walk_candidate,
)
from roboquant.index_forecast.reliable import ReliableSettings, _prepare, _walk  # noqa: E402
from roboquant.index_forecast.reliable_inputs import (  # noqa: E402
    KST,
    resolve_forecast_context,
    session_schedule,
    utc_timestamp,
)
from roboquant.index_forecast.reliable_pipeline import build_reliable_price_frame  # noqa: E402
from roboquant.index_forecast.us_market_inputs import US_FEATURES, US_SYMBOLS, build_us_features  # noqa: E402

GATED = {
    "C1_post_price_l2": ("postclose", PRICE_FEATURES, "logistic", 2),
    "C2_post_price_ohlc_l2": ("postclose", [*PRICE_FEATURES, *OHLC_FEATURES], "logistic", 2),
    "C3_post_price_credit_l2": ("postclose", [*PRICE_FEATURES, *CREDIT_FEATURES], "logistic", 2),
    "C4_post_price_rates_l2": ("postclose", [*PRICE_FEATURES, *RATE_FEATURES], "logistic", 2),
    "C5_post_regime_cell_rate": ("postclose", REGIME_FEATURES, "regime_cells", 2),
    "C6_pre_price_us_l2": ("premarket_final", [*PRICE_FEATURES, *US_FEATURES], "logistic", 2),
}
ABLATIONS = {
    "A1_pre_us_only_l2": ("premarket_final", list(US_FEATURES), "logistic", 2),
    "A3_post_price_credit_l2_lag1": ("postclose", [*PRICE_FEATURES, *CREDIT_FEATURES], "logistic", 1),
    "A4_post_price_rates_l2_lag1": ("postclose", [*PRICE_FEATURES, *RATE_FEATURES], "logistic", 1),
}
PARENT = {"C2_post_price_ohlc_l2": "C1_post_price_l2", "C3_post_price_credit_l2": "C1_post_price_l2",
          "C4_post_price_rates_l2": "C1_post_price_l2", "C6_pre_price_us_l2": "C1_post_price_l2",
          "A3_post_price_credit_l2_lag1": "C1_post_price_l2", "A4_post_price_rates_l2_lag1": "C1_post_price_l2",
          "A1_pre_us_only_l2": "C6_pre_price_us_l2"}


def _refuse_docs(path: Path) -> Path:
    resolved = path.resolve()
    if (ROOT / "docs").resolve() in (resolved, *resolved.parents):
        raise SystemExit("refusing to write research output under docs/")
    return resolved


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _read_prices(sqlite_path: Path, market: str) -> pd.DataFrame:
    with sqlite3.connect(f"file:{sqlite_path.resolve()}?mode=ro", uri=True) as conn:
        return pd.read_sql_query("SELECT * FROM index_eod WHERE index_code=? ORDER BY session_date", conn, params=[market])


def research_frame(prices: pd.DataFrame, us: pd.DataFrame, *, stage: str, terminal_start: str,
                   knowledge_cutoff: str) -> pd.DataFrame:
    """Incumbent-identical frame truncated so no as-of >= terminal_start enters anything."""
    prices = prices.loc[prices["session_date"] <= terminal_start].drop(columns=["ingested_at"], errors="ignore")
    last = prices["session_date"].max()
    schedule, _ = session_schedule(pd.Timestamp(last) - pd.Timedelta(days=10), pd.Timestamp(last) + pd.Timedelta(days=14))
    close_at = dict(zip(schedule["date"], schedule["close_at"], strict=True))
    target = schedule.loc[schedule["date"] > last].iloc[0]["date"]
    cutoff = historical_cutoff(close_at[last]) if stage == "postclose" else utc_timestamp(target + "T08:00:00+09:00")
    frame = build_reliable_price_frame(prices, forecast_context={"asof_date": last, "stage": stage,
                                                                 "prediction_cutoff": cutoff.isoformat()})
    cutoffs = dict(zip(frame["asof_date"], frame["prediction_cutoff"], strict=True))
    features = build_us_features(us, asof_cutoffs=cutoffs, reporting_cutoff=knowledge_cutoff)["features"]
    frame = frame.merge(features, on="asof_date", how="left", validate="one_to_one")
    return add_ohlc_features(frame.loc[frame["asof_date"] < terminal_start]).reset_index(drop=True)


def _extra(frame: pd.DataFrame, robo: pd.DataFrame, market: str, lag: int) -> pd.DataFrame:
    days = frame["asof_date"].tolist()

    def series(scope, name):
        return robo.loc[(robo["market"] == scope) & (robo["series"] == name), ["observation_date", "value"]]

    parts = [lagged_series_features(days, series(market, "MARGIN_CREDIT_BALANCE"), lag_sessions=lag,
                                    changes={c: (int(c.rsplit("_", 1)[1]), "log") for c in CREDIT_FEATURES})]
    for column, name in (("kr3y_change_5_bp", "KR_YIELD_3Y"), ("us10y_change_5_bp", "US_YIELD_10Y"),
                         ("us1y_change_5_bp", "US_YIELD_1Y")):
        parts.append(lagged_series_features(days, series("GLOBAL", name), lag_sessions=lag, changes={column: (5, "bp")}))
    out = parts[0].drop(columns="_latest_observation_date")
    for part in parts[1:]:
        out = out.merge(part.drop(columns="_latest_observation_date"), on="asof_date", how="left", validate="one_to_one")
    return out


def _ll(p, y):
    c = np.clip(p, 1e-12, 1 - 1e-12)
    return -y * np.log(c) - (1 - y) * np.log(1 - c)


def _score(name, pool, incumbent, pools, drawdown, dev_start):
    dates = sorted(set(incumbent) & set(pool))
    if not dates:
        return {"n_dates": 0}
    y = np.array([incumbent[d]["actual_return"] > 0 for d in dates], dtype=float)
    series = {"candidate": np.array([pool[d]["p_up"] for d in dates]),
              "up_rate": np.array([incumbent[d]["baselines"]["up_rate"]["p_up"] for d in dates]),
              "incumbent": np.array([incumbent[d]["p_up"] for d in dates]),
              "legacy": np.array([incumbent[d]["baselines"]["legacy"]["p_up"] for d in dates]),
              "previous_direction": np.array([incumbent[d]["references"]["previous_direction"]["p_up"] for d in dates]),
              "simple_trend": np.array([incumbent[d]["references"]["simple_trend"]["p_up"] for d in dates])}
    windows = {}
    for label, mask in (("primary_extended", np.ones(len(dates), bool)),
                        ("development_252", np.array([d >= dev_start for d in dates]))):
        loss = {k: (v[mask] - y[mask]) ** 2 for k, v in series.items()}
        window = {"n": int(mask.sum()), "start": dates[int(np.argmax(mask))], "end": dates[-1],
                  "metrics": {k: probability_metrics(v[mask], y[mask]) for k, v in series.items()},
                  "brier_diff_vs_up_rate": block_bootstrap(loss["candidate"] - loss["up_rate"]),
                  "brier_diff_vs_incumbent": block_bootstrap(loss["candidate"] - loss["incumbent"]),
                  "brier_diff_vs_legacy": block_bootstrap(loss["candidate"] - loss["legacy"]),
                  "logloss_diff_vs_up_rate": block_bootstrap(_ll(series["candidate"][mask], y[mask]) - _ll(series["up_rate"][mask], y[mask])),
                  "reliability_candidate": reliability(series["candidate"][mask], y[mask]),
                  "abstain_curve_candidate": abstain_curve(series["candidate"][mask], y[mask]),
                  "abstain_curve_incumbent": abstain_curve(series["incumbent"][mask], y[mask])}
        parent = PARENT.get(name)
        if parent in pools:
            common = [d for d, m in zip(dates, mask, strict=True) if m and d in pools[parent]]
            yc = np.array([incumbent[d]["actual_return"] > 0 for d in common], dtype=float)
            pc = np.array([pool[d]["p_up"] for d in common])
            pp = np.array([pools[parent][d]["p_up"] for d in common])
            window[f"block_ablation_vs_{parent}"] = {"n": len(common), "candidate_brier": float(np.mean((pc - yc) ** 2)),
                                                     "parent_brier": float(np.mean((pp - yc) ** 2)),
                                                     "difference": block_bootstrap((pc - yc) ** 2 - (pp - yc) ** 2)}
        windows[label] = window
    slices = {}
    labels = {"volatility_regime": [incumbent[d]["volatility_regime"] for d in dates],
              "trend_regime": [incumbent[d]["trend_regime"] for d in dates],
              "drawdown_10pct": ["drawdown" if drawdown.get(d, 0) <= -.10 else "normal" for d in dates],
              "year": [d[:4] for d in dates]}
    for dimension, values in labels.items():
        values = np.array(values)
        slices[dimension] = {v: {"n": int((values == v).sum()), "observed_up_rate": float(y[values == v].mean()),
                                 **{f"brier_{k}": float(np.mean((series[k][values == v] - y[values == v]) ** 2))
                                    for k in ("candidate", "up_rate", "incumbent", "legacy")},
                                 **{f"mean_p_{k}": float(series[k][values == v].mean()) for k in ("candidate", "up_rate", "incumbent")}}
                             for v in sorted(set(values))}
    actual = np.array([incumbent[d]["actual_return"] for d in dates])
    for label, mask in (("big_down_le_-2pct", actual <= -.02), ("big_up_ge_2pct", actual >= .02)):
        slices[label] = {"n": int(mask.sum()), **{f"mean_p_{k}": float(series[k][mask].mean()) if mask.any() else None
                                                    for k in ("candidate", "up_rate", "incumbent")}}
    return {"n_dates": len(dates), "windows": windows, "slices": slices}


def run_research(args) -> dict:
    output = _refuse_docs(Path(args.output_dir))
    output.mkdir(parents=True, exist_ok=False)
    us = pd.read_csv(args.us_observations)
    robo = pd.DataFrame(columns=["market", "series", "observation_date", "value", "availability_policy"])
    if args.session_inputs:
        with sqlite3.connect(f"file:{Path(args.session_inputs).resolve()}?mode=ro", uri=True) as conn:
            robo = pd.read_sql_query("SELECT * FROM session_input_observations ORDER BY version_id", conn)
        robo = robo.loc[robo["availability_policy"].isin(["historical_dated_yield", "historical_dated_credit"])
                        & (robo["observation_date"] < args.terminal_start)]
    settings = ReliableSettings(model_families=("linear",))
    report = {"schema_version": "probability_research_v1", "production_weight": 0.0,
              "preregistration_sha256": args.preregistration_sha256, "terminal_start_excluded": args.terminal_start,
              "development_start": args.development_start, "knowledge_cutoff": args.knowledge_cutoff,
              "inputs": {"sqlite_sha256": _sha256(Path(args.sqlite)), "us_observations_sha256": _sha256(Path(args.us_observations)),
                         "session_inputs_sha256": _sha256(Path(args.session_inputs)) if args.session_inputs else None},
              "shadow_spec_hash": shadow_spec_hash(), "markets": {}, "diagnostics": {}}
    p_values = {}
    for market in ("KOSPI", "KOSDAQ"):
        prices = _read_prices(Path(args.sqlite), market)
        frames, histories, incumbents = {}, {}, {}
        for stage in ("postclose", "premarket_final"):
            frame = research_frame(prices, us, stage=stage, terminal_start=args.terminal_start,
                                   knowledge_cutoff=args.knowledge_cutoff)
            for lag in (1, 2):
                extra = _extra(frame, robo, market, lag).set_index("asof_date").add_suffix(f"__lag{lag}").reset_index()
                frame = frame.merge(extra, on="asof_date", how="left", validate="one_to_one")
            history = frame.dropna(subset=[*PRICE_FEATURES, *US_FEATURES, "label_return", "target_date"]).reset_index(drop=True)
            config = replace(settings, issuance_protocol="after_close_daily_v1" if stage == "postclose" else "premarket_v1")
            incumbents[stage] = _walk(_prepare(history), {"price": list(PRICE_FEATURES), "price_us": [*PRICE_FEATURES, *US_FEATURES]},
                                      config, {}, _prepare(frame, keep_unlabeled=True))
            frames[stage], histories[stage] = frame, history
        incumbent = {r["asof_date"]: r for r in incumbents["postclose"]["linear:price"]
                     if r["calibration_ready"] and r["asof_date"] < args.terminal_start}
        pools, fits = {}, {}
        for name, (stage, features, kind, lag) in {**GATED, **ABLATIONS}.items():
            history, grid = histories[stage].copy(), frames[stage].copy()
            for column in (*CREDIT_FEATURES, *RATE_FEATURES):
                history[column], grid[column] = history[f"{column}__lag{lag}"], grid[f"{column}__lag{lag}"]
            config = replace(settings, issuance_protocol="after_close_daily_v1" if stage == "postclose" else "premarket_v1")
            records, fits[name] = walk_candidate(_prepare(history), _prepare(grid, keep_unlabeled=True), list(features), kind, config)
            pools[name] = {r["asof_date"]: r for r in records}
        pools["A2_pre_incumbent_price_us_sigmoid"] = {r["asof_date"]: r for r in incumbents["premarket_final"]["linear:price_us"] if r["calibration_ready"]}
        pools["A5_post_incumbent_price_us_sigmoid"] = {r["asof_date"]: r for r in incumbents["postclose"]["linear:price_us"] if r["calibration_ready"]}
        close = pd.to_numeric(frames["postclose"].set_index("asof_date")["close"], errors="coerce")
        drawdown = (close / close.rolling(252, min_periods=20).max() - 1).to_dict()
        report["markets"][market] = {name: _score(name, pool, incumbent, pools, drawdown, args.development_start)
                                     for name, pool in pools.items()}
        report["markets"][market]["_fits"] = {name: values for name, values in fits.items()}
        for name in GATED:
            p_values[f"{name}|{market}"] = report["markets"][market][name]["windows"]["primary_extended"]["brier_diff_vs_up_rate"]["p_one_sided_not_better"]
        post, pre = frames["postclose"].set_index("asof_date"), frames["premarket_final"].set_index("asof_date")
        label = post["label_return"]
        gap = pd.to_numeric(post["open"], errors="coerce").shift(-1) / pd.to_numeric(post["close"], errors="coerce") - 1
        ok = label.notna() & gap.notna()
        timing = {"n": int(ok.sum()), "var_share_of_next_return_in_open_gap": float(gap[ok].var() / label[ok].var())}
        for column in ("us_spx_return_1d", "us_nasdaq_return_1d", "us_sox_return_1d", "us_vix_return_1d"):
            for clock, source in (("premarket_us_session_T", pre), ("postclose_us_session_T_minus_1", post)):
                x = source[column].reindex(label.index)
                m = ok & x.notna()
                timing[f"corr_next_return__{column}__{clock}"] = float(np.corrcoef(x[m], label[m])[0, 1])
        report["diagnostics"][market] = {"timing_information": timing,
                                         "us_feature_missing_share_postclose_frame": float(post[US_FEATURES].isna().any(axis=1).mean()),
                                         "us_feature_missing_share_premarket_frame": float(pre[US_FEATURES].isna().any(axis=1).mean())}
    adjusted = holm(p_values)
    gate = {}
    for key, values in adjusted.items():
        name, market = key.split("|")
        windows = report["markets"][market][name]["windows"]
        primary, development = windows["primary_extended"]["metrics"], windows["development_252"]["metrics"]
        reasons = []
        if not values["reject_at_alpha"]:
            reasons.append("holm_adjusted_p_not_below_0.05")
        if not development["candidate"]["brier"] < development["up_rate"]["brier"]:
            reasons.append("dev252_brier_not_below_up_rate")
        if (primary["candidate"]["log_loss"] > primary["up_rate"]["log_loss"]
                or development["candidate"]["log_loss"] > development["up_rate"]["log_loss"]):
            reasons.append("log_loss_worse_than_up_rate")
        if primary["candidate"]["ece10"] > .05:
            reasons.append("ece10_above_0.05")
        if any(v["brier_candidate"] - v["brier_up_rate"] > .01 for v in report["markets"][market][name]["slices"]["year"].values()):
            reasons.append("a_year_worse_than_up_rate_by_more_than_0.01")
        gate[key] = {**values, "passed": not reasons, "reasons": reasons}
    report["holm_family"] = "6 candidates x 2 markets, one-sided bootstrap p vs up_rate on the primary window"
    report["development_gate"] = gate
    (output / "probability_research.json").write_text(json.dumps(report, indent=1, allow_nan=False, default=float) + "\n")
    return report


def shadow_config(path: Path) -> dict:
    """The shadow runs only when explicitly enabled and the frozen spec hash matches."""
    import yaml

    config = yaml.safe_load(Path(path).read_text()) or {}
    shadow = config.get("shadow", {})
    if shadow.get("spec_hash") != shadow_spec_hash():
        raise SystemExit("shadow spec hash mismatch; the frozen pre-registered spec was changed")
    if float(shadow.get("production_weight", 0.0)) != 0.0:
        raise SystemExit("shadow production_weight must remain 0")
    return config


def read_ledger(path: Path) -> list[dict]:
    if not Path(path).is_file():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def is_scoreable(row: dict) -> bool:
    """A forward-cohort row: an actual-clock, in-window, frozen-spec forecast with pre-cutoff receipts."""
    return (row.get("status") == "shadow_research" and not row.get("rehearsal") and not row.get("replay")
            and row.get("spec_hash") == shadow_spec_hash() and row.get("p_up") is not None
            and (row.get("context") or {}).get("stage") == "premarket_final"
            and (row.get("input_receipts") or {}).get("received_before_cutoff") is True)


def already_issued(rows: list[dict], market: str, target_date: str | None) -> bool:
    return any(is_scoreable(r) and r.get("market") == market
               and (r.get("context") or {}).get("target_date") == target_date for r in rows)


def incumbent_card(path: Path | None, market: str, target_date: str | None) -> dict:
    """The published 18:00 after-close p_up for the same target, read-only, for the paired comparison."""
    if path is None or not Path(path).is_file():
        return {"available": False, "reason": "incumbent_forecast_file_missing"}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "reason": "incumbent_forecast_unreadable"}
    for entry in payload.get("markets") or []:
        if entry.get("market") != market:
            continue
        forecast = entry.get("forecast") or {}
        if forecast.get("target_date") != target_date or forecast.get("p_up") is None:
            return {"available": False, "reason": "incumbent_target_mismatch",
                    "incumbent_target_date": forecast.get("target_date")}
        return {"available": True, "p_up": float(forecast["p_up"]), "status": forecast.get("status"),
                "asof_date": forecast.get("asof_date"), "forecast_id": entry.get("forecast_id"),
                "issuance_protocol": payload.get("issuance_protocol"),
                "artifact_written_at": payload.get("artifact_written_at")}
    return {"available": False, "reason": "incumbent_market_missing"}


def up_rate_at_cutoff(history: pd.DataFrame, asof: str, cutoff) -> dict:
    """Same rule as the incumbent live baseline: all labels matured by the cutoff, before the as-of."""
    known = _prepare(history)
    limit = utc_timestamp(cutoff)
    if known.empty or pd.isna(limit):
        return {"p_up": None, "known_label_n": 0}
    known = known.loc[(known["_asof_label"] < asof) & (known["_available"] <= limit) & (known["_target"] <= limit)]
    return {"p_up": float((known["label_return"] > 0).mean()) if not known.empty else None,
            "known_label_n": int(len(known))}


def input_receipts(prices: pd.DataFrame, asof: str, us_snapshot: dict, cutoff) -> dict:
    limit = utc_timestamp(cutoff)
    row = prices.loc[prices["session_date"].astype(str).str.slice(0, 10) == asof]
    kr_seen = utc_timestamp(row["ingested_at"].iloc[-1]) if not row.empty and "ingested_at" in row else pd.NaT
    us_seen = [utc_timestamp(v.get("first_seen_at")) for v in (us_snapshot or {}).values() if v.get("first_seen_at")]
    us_latest = max(us_seen) if us_seen and not any(pd.isna(s) for s in us_seen) else pd.NaT
    ok = (not pd.isna(limit) and not pd.isna(kr_seen) and not pd.isna(us_latest)
          and kr_seen <= limit and us_latest <= limit and len(us_seen) == len(US_SYMBOLS))
    return {"kr_close_received_at": None if pd.isna(kr_seen) else kr_seen.isoformat(),
            "us_latest_first_seen_at": None if pd.isna(us_latest) else us_latest.isoformat(),
            "us_symbols_received": len(us_seen), "received_before_cutoff": bool(ok)}


def run_shadow(args) -> dict:
    config = shadow_config(Path(args.config))
    if not config["shadow"].get("enabled", False):
        raise SystemExit("shadow logging disabled in config (shadow.enabled: false)")
    official = (ROOT / config["shadow"]["ledger"]).resolve()
    ledger = _refuse_docs(Path(args.ledger) if args.ledger else official)
    if args.rehearsal and (not args.ledger or ledger == official):
        raise SystemExit("a rehearsal must write to an explicit --ledger outside the forward-cohort ledger")
    if args.rehearsal and args.now:
        raise SystemExit("a rehearsal uses the actual clock; --now is replay only")
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(KST)
    if now.tzinfo is None:
        raise SystemExit("--now must include a timezone")
    existing = read_ledger(ledger)
    us = pd.read_csv(args.us_observations)
    incumbent_path = Path(args.incumbent_forecast) if args.incumbent_forecast else None
    rows, skipped = [], []
    for market in [m.strip() for m in args.markets.split(",") if m.strip()]:
        prices = _read_prices(Path(args.sqlite), market)
        context = resolve_forecast_context(prices["session_date"].max(), now=now, issuance_protocol="premarket_v1")
        record = {"schema_version": "premarket_us_shadow_ledger_v2", "cohort": config.get("forward_confirmation", {}).get("cohort"),
                  "market": market, "run_at": utc_timestamp(now).isoformat(), "replay": args.now is not None,
                  "rehearsal": bool(args.rehearsal), "spec_hash": shadow_spec_hash(), "production_weight": 0.0,
                  "run_provenance": {k: v for k, v in (("github_run_id", args.run_id), ("git_sha", args.git_sha)) if v},
                  "context": {k: context.get(k) for k in ("status", "stage", "asof_date", "target_date", "prediction_cutoff",
                                                           "scheduled_cutoff", "issuance_deadline", "target_close_at")}}
        if not args.rehearsal and already_issued(existing, market, context.get("target_date")):
            skipped.append({"market": market, "target_date": context.get("target_date"), "reason": "already_issued"})
            continue
        in_window = context["status"] == "ready" and context["stage"] == "premarket_final"
        if context["status"] != "ready" or not (in_window or args.rehearsal):
            rows.append({**record, "status": "blocked", "reasons": [*context.get("reasons", []), "not_premarket_final_window"]})
            continue
        frame = build_reliable_price_frame(prices, forecast_context=context)
        cutoffs = dict(zip(frame["asof_date"], frame["prediction_cutoff"], strict=True))
        features = build_us_features(us, asof_cutoffs=cutoffs, live_asof_date=context["asof_date"],
                                     reporting_cutoff=context["prediction_cutoff"])
        frame = frame.merge(features["features"], on="asof_date", how="left", validate="one_to_one")
        frame = frame.loc[frame["asof_date"] <= context["asof_date"]]
        history = frame.dropna(subset=[*PRICE_FEATURES, *US_FEATURES, "label_return", "target_date"])
        latest = frame.loc[frame["asof_date"] == context["asof_date"]]
        forecast = forecast_premarket_us_shadow(history, latest, prediction_grid=frame,
                                                settings=ReliableSettings(issuance_protocol="premarket_v1"))
        snapshot = features["report"].get("latest_snapshot") or {}
        rows.append({**record, **forecast, "us_snapshot": snapshot,
                     "input_receipts": input_receipts(prices, context["asof_date"], snapshot, context["prediction_cutoff"]),
                     "baselines": {"up_rate": up_rate_at_cutoff(history, context["asof_date"], context["prediction_cutoff"])},
                     "incumbent_postclose_card": incumbent_card(incumbent_path, market, context.get("target_date"))})
    if rows:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True, default=str, allow_nan=False) + "\n")
    return {"rows": rows, "skipped": skipped, "ledger": str(ledger)}


def _closes(sqlite_path: Path) -> dict[tuple[str, str], float]:
    with sqlite3.connect(f"file:{sqlite_path.resolve()}?mode=ro", uri=True) as conn:
        data = pd.read_sql_query("SELECT index_code, session_date, close FROM index_eod", conn)
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    data = data.loc[data["close"].gt(0)]
    return {(str(m), str(d)[:10]): float(c) for m, d, c in zip(data["index_code"], data["session_date"], data["close"], strict=True)}


def score_ledger(rows: list[dict], closes: dict[tuple[str, str], float], config: dict, *, now) -> dict:
    """Score matured forward-cohort rows against the up-rate and the paired 18:00 card. Descriptive until the decision n."""
    rule = config.get("forward_confirmation", {})
    boot = rule.get("bootstrap", {})
    limit = utc_timestamp(now)
    first: dict[tuple[str, str], dict] = {}
    for row in rows:
        if is_scoreable(row):
            first.setdefault((row["market"], row["context"]["target_date"]), row)
    markets = {}
    for market in sorted({m for m, _ in first} | set(config.get("shadow", {}).get("markets", {}))):
        issued = [r for (m, _), r in sorted(first.items()) if m == market]
        matured = []
        for r in issued:
            ctx = r["context"]
            base, target = closes.get((market, ctx["asof_date"])), closes.get((market, ctx["target_date"]))
            up = (r.get("baselines") or {}).get("up_rate", {}).get("p_up")
            close_at = r.get("target_close_at") or ctx.get("target_close_at")
            if base is None or target is None or up is None or (close_at and utc_timestamp(close_at) > limit):
                continue
            card = r.get("incumbent_postclose_card") or {}
            matured.append({"target_date": ctx["target_date"], "p_up": float(r["p_up"]), "up_rate": float(up),
                            "incumbent": float(card["p_up"]) if card.get("available") else None,
                            "actual_return": target / base - 1})
        summary = {"role": rule.get("primary_market") == market and "primary" or "monitor_only",
                   "issued": len(issued), "matured": len(matured), "status": "accumulating"}
        if matured:
            y = np.array([m["actual_return"] > 0 for m in matured], dtype=float)
            p, u = np.array([m["p_up"] for m in matured]), np.array([m["up_rate"] for m in matured])
            summary.update(first_target=matured[0]["target_date"], last_target=matured[-1]["target_date"],
                           shadow=probability_metrics(p, y), up_rate=probability_metrics(u, y),
                           brier_minus_up_rate=float(np.mean((p - y) ** 2 - (u - y) ** 2)))
            paired = [m for m in matured if m["incumbent"] is not None]
            if paired:
                yi = np.array([m["actual_return"] > 0 for m in paired], dtype=float)
                pi, ii = np.array([m["p_up"] for m in paired]), np.array([m["incumbent"] for m in paired])
                summary["vs_incumbent_postclose"] = {"n": len(paired), "shadow": probability_metrics(pi, yi), "incumbent": probability_metrics(ii, yi),
                                                     "brier_minus_incumbent": float(np.mean((pi - yi) ** 2 - (ii - yi) ** 2))}
            n = len(matured)
            if n >= int(rule.get("decision_n", 252)):
                kwargs = {"samples": int(boot.get("samples", 2000)), "block": int(boot.get("block_days", 20)),
                          "seed": int(boot.get("random_state", 42))}
                vs_up = block_bootstrap((p - y) ** 2 - (u - y) ** 2, **kwargs)
                checks = {"brier_vs_up_rate_upper_below_0": vs_up["high"] < 0,
                          "log_loss_not_worse_than_up_rate": summary["shadow"]["log_loss"] <= summary["up_rate"]["log_loss"],
                          "ece10_at_most_0.05": summary["shadow"]["ece10"] <= .05}
                if len(paired) == n:
                    vs_inc = block_bootstrap((pi - yi) ** 2 - (ii - yi) ** 2, **kwargs)
                    checks["brier_vs_incumbent_upper_below_0"] = vs_inc["high"] < 0
                    summary["decision_bootstrap_vs_incumbent"] = vs_inc
                else:
                    checks["brier_vs_incumbent_upper_below_0"] = False
                    summary["decision_incumbent_pairs_missing"] = n - len(paired)
                summary.update(decision_bootstrap_vs_up_rate=vs_up, decision_checks=checks,
                               status=("passed_pending_independent_review" if all(checks.values()) else "failed")
                               if summary["role"] == "primary" else "monitor_only_decision_n_reached")
            elif n >= int(rule.get("interim_safety_look_n", 60)):
                harm = summary["brier_minus_up_rate"] > float(rule.get("interim_stop_if_brier_minus_up_rate_above", .02))
                summary["status"] = "stopped_for_harm" if harm else "interim_safety_passed_accumulating"
        markets[market] = summary
    return {"schema_version": "premarket_us_shadow_summary_v1", "cohort": rule.get("cohort"),
            "spec_hash": shadow_spec_hash(), "production_weight": 0.0, "generated_at": limit.isoformat(),
            "rule": {k: rule.get(k) for k in ("interim_safety_look_n", "interim_stop_if_brier_minus_up_rate_above",
                                               "decision_n", "pass_requires", "after_pass")},
            "note": "Descriptive until decision_n matured forecasts; no efficacy claim before then.",
            "markets": markets}


def run_evaluate(args) -> dict:
    config = shadow_config(Path(args.config))
    ledger = _refuse_docs(Path(args.ledger) if args.ledger else ROOT / config["shadow"]["ledger"])
    output = _refuse_docs(Path(args.output) if args.output else ROOT / config["shadow"]["summary"])
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(KST)
    summary = score_ledger(read_ledger(ledger), _closes(Path(args.sqlite)), config, now=now)
    summary["ledger_sha256"] = _sha256(ledger)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=1, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    research = sub.add_parser("research")
    research.add_argument("--sqlite", default="data/processed/index_forecast.sqlite")
    research.add_argument("--us-observations", default="data/processed/us_market_observations_v2.csv")
    research.add_argument("--session-inputs", default=None, help="read-only robo_session_inputs.sqlite (yields, credit)")
    research.add_argument("--terminal-start", default="2025-11-10")
    research.add_argument("--development-start", default="2024-10-25")
    research.add_argument("--knowledge-cutoff", default="2026-11-21T00:00:00+00:00")
    research.add_argument("--preregistration-sha256", default="42f0d118cf6afa078a3b3150d5bb8f6d3e1d8a0eef50b5220856cfbc2ff89daf")
    research.add_argument("--output-dir", required=True)
    shadow = sub.add_parser("shadow")
    shadow.add_argument("--sqlite", default="data/processed/index_forecast.sqlite")
    shadow.add_argument("--us-observations", default="data/processed/us_market_observations_v2.csv")
    shadow.add_argument("--now", default=None, help="replay clock (labelled replay=true); default actual clock")
    shadow.add_argument("--config", default="configs/probability_shadow.yaml")
    shadow.add_argument("--ledger", default=None, help="append-only JSONL outside docs/ (default from config)")
    shadow.add_argument("--markets", default="KOSPI,KOSDAQ")
    shadow.add_argument("--rehearsal", action="store_true",
                        help="actual clock, any ready stage; labelled rehearsal and never written to the cohort ledger")
    shadow.add_argument("--incumbent-forecast", default="docs/data/reliable_index_forecast.json",
                        help="read-only published 18:00 card for the paired comparison")
    shadow.add_argument("--run-id", default=None)
    shadow.add_argument("--git-sha", default=None)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--sqlite", default="data/processed/index_forecast.sqlite")
    evaluate.add_argument("--config", default="configs/probability_shadow.yaml")
    evaluate.add_argument("--ledger", default=None)
    evaluate.add_argument("--output", default=None)
    evaluate.add_argument("--now", default=None)
    args = parser.parse_args(argv)
    if args.mode == "research":
        result = run_research(args)
        for key, value in sorted(result["development_gate"].items()):
            print(key, "PASS" if value["passed"] else "fail", f"p_holm={value['p_holm']:.4f}", ",".join(value["reasons"]))
    elif args.mode == "shadow":
        result = run_shadow(args)
        for row in result["rows"]:
            print(json.dumps({"market": row["market"], "status": row.get("status"), "p_up": row.get("p_up"),
                              "target_date": row["context"].get("target_date"), "rehearsal": row["rehearsal"],
                              "reasons": row.get("reasons") or row.get("gate_reasons"),
                              "received_before_cutoff": (row.get("input_receipts") or {}).get("received_before_cutoff")}))
        for item in result["skipped"]:
            print(json.dumps({**item, "status": "skipped"}))
    else:
        result = run_evaluate(args)
        for market, value in result["markets"].items():
            print(json.dumps({"market": market, **{k: value.get(k) for k in ("role", "issued", "matured", "status")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
