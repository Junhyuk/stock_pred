#!/usr/bin/env python3
"""Refresh exact Korean cash-index observations and publish isolated next-session research."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from roboquant.index_forecast.automation_state import atomic_json
from roboquant.index_forecast.bc_publication import read_payload, valid_pair
from roboquant.index_forecast.benchmark_context import (
    FEATURE_COLUMNS,
    PARENTS,
    add_context,
    context_features,
    issue_window,
    trigger_value,
)
from roboquant.index_forecast.benchmark_ledger import (
    acknowledge,
    forward_scores,
    init_ledger,
    issued_models,
    save_issue,
)
from roboquant.index_forecast.benchmark_models import evaluate_and_predict
from roboquant.index_forecast.benchmark_publication import valid_benchmark_payload
from roboquant.index_forecast.benchmark_sources import (
    SourceRights,
    collect_benchmark_history,
    import_benchmark_csv,
    load_benchmark_history,
)
from roboquant.index_forecast.market_closes import effective_index_eod_frame
from roboquant.index_forecast.postclose import verify_market_closes
from roboquant.index_forecast.reliable_inputs import KST, session_schedule, utc_timestamp
from scripts.run_bc_cpu_afterclose import daily_inputs


def live_trigger(public_dir, window, cutoff, broad_db=None):
    # Prior-day moves are independently usable even when broad Model B has missing US inputs.
    close_file = public_dir / "market_closes.json"
    if broad_db and Path(broad_db).is_file() and close_file.is_file():
        try:
            quotes = verify_market_closes(close_file)["markets"]
            frame = effective_index_eod_frame(broad_db, cutoff=cutoff)
            anchor = date.fromisoformat(window["asof_date"])
            sessions, _ = session_schedule(anchor - timedelta(days=40), anchor)
            moves, _ = daily_inputs(frame, quotes, sessions, cutoff)
            if any(row.get("session_date") == str(anchor) for row in quotes.values()):
                return moves
        except (KeyError, ValueError, OSError, sqlite3.DatabaseError, pd.errors.DatabaseError):
            pass
    # A verified B/C pair for the same target carries the original receipt-based broad moves.
    b, c = (read_payload(public_dir / f"model_{m}_forecast.json") for m in ("b", "c"))
    if (valid_pair(b, c) and c.get("asof_date") == window["asof_date"]
            and c.get("target_date") == window["target_date"]
            and utc_timestamp(c.get("prediction_cutoff")) <= cutoff):
        return c["trigger_returns"]
    return {}


def date_frame(df):
    df = df.copy()
    df.index = pd.to_datetime(df.index).normalize()
    return df.sort_index()


def history_info(frame):
    if frame.empty:
        return {"start": None, "end": None, "count": 0}
    return {"start": frame.index.min().date().isoformat(), "end": frame.index.max().date().isoformat(),
            "count": len(frame)}


def attribution(row):
    return json.loads(row.rights_json)["attribution"]


def chart_history(frame, anchor):
    if frame.empty:
        return {}
    period = frame.loc[(frame.index >= pd.Timestamp(anchor) - pd.DateOffset(years=1))
                       & (frame.index <= pd.Timestamp(anchor))]
    if period.empty:
        return {}
    return {"points": [{"date": day.date().isoformat(), "close": float(row.close)} for day, row in period.iterrows()],
            "asof_date": str(period.index[-1].date()), "source": attribution(period.iloc[-1]), "raw_display_permitted": True}


def read_json(path):
    return json.loads(path.read_text()) if path.is_file() else {}


def collect(private, end, *, start=None):
    private.mkdir(parents=True, exist_ok=True)
    path = private / "observations.sqlite"
    status = {}
    for market in PARENTS:
        # Scan the rolling window locally. Only missing sessions and the correction overlap cost requests.
        first = start or (date.fromisoformat(end) - timedelta(days=5 * 366)).isoformat()
        attempt = collect_benchmark_history(path, index_code=market, start_date=first, end_date=end,
            max_requests=850 if start else 12, budget_seconds=170 if start else 18, timeout_seconds=6)
        status[market] = attempt.to_dict()
    atomic_json(private / "collection.json", status, indent=2)
    return status


def market_forecast(market, *, private, ledger, context_paths, window, cutoff, moves, research_only=False):
    path = private / "observations.sqlite"
    anchor, target = window["asof_date"], window["target_date"]
    history = date_frame(load_benchmark_history(path, market, as_of=cutoff))
    history = history.loc[:anchor]
    def load_scope(**kwargs):
        return date_frame(load_benchmark_history(path, market, as_of=cutoff, **kwargs)).loc[:anchor]
    authorized = history if research_only else load_scope(derived_only=True)
    chart = chart_history(load_scope(public_raw_only=True), anchor)
    close_allowed = chart.get("asof_date") == anchor
    market_info = {
        "status": "not_ready", "asof_date": anchor, "target_date": target, "cutoff": cutoff.isoformat(),
        "source": attribution(authorized.iloc[-1]) if not authorized.empty else (attribution(history.iloc[-1]) if not history.empty else None),
        "history": history_info(history), "eligible_history": history_info(authorized), "chart": chart,
        "close": chart["points"][-1]["close"] if close_allowed else None,
    }
    day = str(cutoff.tz_convert(KST).date())
    previous = issued_models(ledger, target, market, window["issue_kind"], day)
    attempts = read_json(private / "collection.json").get(market, {})
    reason = None
    if history.empty:
        reasons = attempts.get("reason_codes") or []
        reason = next(iter(reasons), None) or attempts.get("reason") or "no_exact_index_history"
    elif authorized.empty:
        reason = "derived_publication_unavailable"
    elif not window["allowed"]:
        reason = "outside_premarket_window"
    elif pd.Timestamp(anchor) not in authorized.index:
        reason = "latest_close_unavailable"
    else:
        observed = load_scope(strict_live=True, **({} if research_only else {"derived_only": True}))
        if pd.Timestamp(anchor) not in observed.index:
            reason = "latest_close_unavailable"
    gate = trigger_value(moves)
    for name in ("A", "B", "C"):
        if name != "C" or gate == 1:
            ledger.execute("INSERT OR IGNORE INTO benchmark_expected VALUES (?,?,?)", (target, market, name))
    pool = {}
    base = None
    if not reason and not all(k in previous for k in (("A", "B", "C") if gate == 1 else ("A", "B"))):
        sessions, _ = session_schedule(authorized.index[0].date(), date.fromisoformat(anchor))
        dates = pd.DatetimeIndex(sessions.date)
        context = context_features(dates, target_date=target, cutoff=cutoff, **context_paths)
        first_versions = load_scope(first_observed=True, **({} if research_only else {"derived_only": True}))
        frame = add_context(first_versions, context, market, moves)
        # The latest observed revision can differ from revision zero used to reconstruct older training rows.
        base = float(observed.loc[pd.Timestamp(anchor), "close"])
        frame.loc[pd.Timestamp(anchor), "close"] = base
        path = private / (market + ("-internal" if research_only else "") + "-evaluation.json")
        result = evaluate_and_predict(market, frame, run_c=gate == 1, target_date=target, cutoff=cutoff.isoformat(),
                                      evaluation_cache=read_json(path) or None, feature_columns=FEATURE_COLUMNS)
        if result.get("evaluation_cache"):
            atomic_json(path, result["evaluation_cache"])
        for name, row in result["models"].items():
            pool[name] = {k: v for k, v in row.items() if k != "close"}
            if "evaluation" in row:
                # Detailed fold, regime and ablation evidence stays in the private evaluation report.
                pool[name]["evaluation"] = {k: v for k, v in row["evaluation"].items()
                    if k not in {"development_candidates", "regimes", "all_sessions"}}

    models = {}
    for name in ("A", "B", "C"):
        if name == "C" and gate != 1:
            models[name] = {"status": "not_triggered" if gate == 0 else "not_ready",
                            "reason": "broad_trigger_not_met" if gate == 0 else "broad_trigger_unknown"}
        else:
            models[name] = previous.get(name) or pool.get(name) or {"status": "not_ready", "reason": reason}
    ready = any(x.get("status") in {"ready", "research_candidate"} for x in models.values())
    market_info.update(status="ready" if ready else "not_ready", reason=None if ready else
                       (reason or models["A"].get("reason") or "insufficient_history"), models=models)
    market_info["forward_validation"] = forward_scores(ledger, market, authorized, anchor,
        {name: row.get("model_version") for name, row in models.items()})
    issued_cuts = {row["cutoff"] for row in models.values()
                   if row.get("status") in {"ready", "research_candidate"} and row.get("cutoff")}
    if base is None and len(issued_cuts) == 1:
        market_info["cutoff"] = next(iter(issued_cuts))
    return market_info, base


def publish(private, output, *, broad_db=None, inputs_db=None, us_csv=None, now=None, research_only=False, confirmed=None):
    private.mkdir(parents=True, exist_ok=True)
    cutoff = utc_timestamp(now or datetime.now(UTC))
    window = issue_window(cutoff)
    try:
        prior = read_json(output) if output.is_file() and output.stat().st_size <= 2_000_000 else {}
    except (OSError, ValueError):
        prior = {}
    if not valid_benchmark_payload(prior):
        prior = {}
    if not window["allowed"] and prior:
        return {"status": "outside_premarket_window", "target_date": window["target_date"]}
    moves = live_trigger(output.parent, window, cutoff, broad_db)
    trigger = trigger_value(moves)
    status = "triggered" if trigger == 1 else "not_triggered" if trigger == 0 else "unknown"
    payload = {"schema_version": 1, "cutoff": cutoff.isoformat(), "asof_date": window["asof_date"],
               "target_date": window["target_date"], "issue_kind": window["issue_kind"],
               "c_trigger": {"status": status, "moves": moves}, "markets": {}}
    paths = dict(broad_db=broad_db, inputs_db=inputs_db, us_csv=us_csv)
    identity = dict(anchor=window["asof_date"], target=window["target_date"], stage=window["issue_kind"],
                    run_date=str(cutoff.tz_convert(KST).date()), cutoff=cutoff.isoformat())
    ledger_name = "internal-issues.sqlite" if research_only else "issues.sqlite"
    with sqlite3.connect(private / ledger_name) as ledger:
        init_ledger(ledger)
        if confirmed and valid_benchmark_payload(confirmed):
            acknowledge(ledger, confirmed)
        bases = {}
        for market in PARENTS:
            try:
                info, bases[market] = market_forecast(market, private=private, ledger=ledger, context_paths=paths,
                    window=window, cutoff=cutoff, moves=moves, research_only=research_only)
            except (ValueError, OSError, sqlite3.DatabaseError, pd.errors.DatabaseError) as exc:
                # Diagnostic details stay on the runner. Preserve previously issued immutable forecasts on retry.
                print(f"{market}: {type(exc).__name__}: {exc}", file=sys.stderr)
                info = {"status": "not_ready", "reason": "benchmark_processing_error",
                        "models": issued_models(ledger, window["target_date"], market, window["issue_kind"], identity["run_date"])}
                if info["models"]:
                    info["status"] = "ready"
            # A cold/missing private cache on a retry cannot erase an issued forecast for the same day.
            old = prior.get("markets", {}).get(market, {})
            same_target = (prior.get("target_date") == window["target_date"]
                           and prior.get("issue_kind") == window["issue_kind"]
                           and utc_timestamp(prior.get("issued_at")).tz_convert(KST).date() == cutoff.tz_convert(KST).date())
            if same_target:
                for name, saved in old.get("models", {}).items():
                    if (saved.get("status") in {"ready", "research_candidate"} and (name != "C" or status == "triggered")
                            and info["models"].get(name, {}).get("status") not in {"ready", "research_candidate"}):
                        info["models"][name] = saved
                        info.update(status="ready", reason=None)
            if old.get("chart", {}).get("raw_display_permitted") and not info.get("chart"):
                if info.get("history", {}).get("count", 0) == 0:
                    info["chart"] = old["chart"]
            payload["markets"][market] = info
        completed = utc_timestamp(now or datetime.now(UTC))
        if completed >= window["open_at"]:
            return {"status": "outside_premarket_window", "target_date": window["target_date"]}
        payload["issued_at"] = completed.isoformat()
        for market, info in payload["markets"].items():
            for name, row in info["models"].items():
                if bases.get(market) is not None and row.get("status") in {"ready", "research_candidate"}:
                    save_issue(ledger, market=market, model=name, issued=completed.isoformat(),
                               close=bases[market], payload=row, **identity)
            for name, row in issued_models(ledger, identity["target"], market, identity["stage"], identity["run_date"]).items():
                if name != "C" or status == "triggered":
                    info["models"][name] = row
        if not valid_benchmark_payload(payload):
            raise ValueError("invalid public benchmark forecast")
        atomic_json(output, payload, indent=2)
    return {"status": "completed", "target_date": payload["target_date"],
            "markets": {market: row["status"] for market, row in payload["markets"].items()}}


def research(private, *, broad_db, inputs_db, us_csv):
    """A private retrospective evaluation is still meaningful when the latest live anchor is delayed."""
    now = utc_timestamp(datetime.now(UTC))
    report = {"kind": "retrospective_research", "observed_at": now.isoformat(), "markets": {}}
    db = private / "observations.sqlite"
    for market in PARENTS:
        hist = date_frame(load_benchmark_history(db, market, as_of=now, first_observed=True))
        stats = history_info(hist)
        row = {"history": stats, "status": "not_ready", "reason": "no_exact_index_history"}
        if not hist.empty:
            anchor = hist.index[-1].date()
            sessions, _ = session_schedule(hist.index[0].date(), anchor + timedelta(days=21))
            dates = pd.DatetimeIndex(sessions.loc[sessions.date <= str(anchor), "date"])
            target = sessions.loc[sessions.date > str(anchor), "date"].iloc[0]
            last_replay_cut = min(utc_timestamp(target + "T08:00:00+09:00"), now)
            context = context_features(dates, target_date=target, cutoff=last_replay_cut, broad_db=broad_db,
                                       inputs_db=inputs_db, us_csv=us_csv)
            frame = add_context(hist, context, market, {})
            cache_path = private / (market + "-retrospective-evaluation.json")
            result = evaluate_and_predict(market, frame, run_c=True, cutoff=now.isoformat(), target_date=target,
                                          feature_columns=FEATURE_COLUMNS, evaluation_cache=read_json(cache_path) or None)
            if result.get("evaluation_cache"):
                atomic_json(cache_path, result["evaluation_cache"])
            row.update(status=result["status"], reason=result.get("reason"), evaluation={
                name: value.get("evaluation") for name, value in result["models"].items()})
        report["markets"][market] = row
    atomic_json(private / "retrospective-research.json", report, indent=2)
    return {m: v["status"] for m, v in report["markets"].items()}


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("phase", choices=["collect", "predict", "backfill", "research", "import-csv"])
    args.add_argument("--private", type=Path, default=ROOT / "data/processed/benchmarks")
    args.add_argument("--output", type=Path, default=ROOT / "docs/data/benchmark_forecasts.json")
    args.add_argument("--broad-db", type=Path, default=ROOT / "data/processed/index_forecast.sqlite")
    args.add_argument("--inputs-db", type=Path, default=ROOT / "data/processed/robo_session_inputs.sqlite")
    args.add_argument("--us-csv", type=Path)
    args.add_argument("--start", type=date.fromisoformat)
    args.add_argument("--end", type=date.fromisoformat)
    args.add_argument("--csv", type=Path, help="Licensed historical CSV to import into the private store")
    args.add_argument("--index", choices=PARENTS)
    args.add_argument("--source")
    args.add_argument("--source-url")
    args.add_argument("--rights", type=Path, help="JSON containing scope, attribution, evidence_url and the three allow flags")
    args.add_argument("--confirm-public", action="store_true")
    args.add_argument("--internal-research", action="store_true", help="Use all research-eligible data and save only inside the private directory")
    opts = args.parse_args()
    window = issue_window(datetime.now(UTC))
    if opts.phase == "import-csv":
        if not all((opts.csv, opts.index, opts.source, opts.source_url, opts.rights)):
            args.error("import requires --csv, --index, --source, --source-url and --rights")
        result = import_benchmark_csv(opts.private / "observations.sqlite", opts.csv, index_code=opts.index,
                                      source=opts.source, source_url=opts.source_url,
                                      rights=SourceRights(**read_json(opts.rights))).to_dict()
    elif opts.phase == "research":
        result = research(opts.private, broad_db=opts.broad_db, inputs_db=opts.inputs_db, us_csv=opts.us_csv)
    elif opts.phase == "predict":
        dest = opts.private / "internal-research.json" if opts.internal_research else opts.output
        confirmed = None
        if opts.confirm_public and not opts.internal_research:
            try:
                request = Request("https://junhyuk.github.io/stock_pred/data/benchmark_forecasts.json",
                                  headers={"Cache-Control": "no-cache"})
                with urlopen(request, timeout=4) as response:
                    confirmed = json.loads(response.read(2_000_000))
            except (OSError, ValueError):
                pass
        result = publish(opts.private, dest, confirmed=confirmed, broad_db=opts.broad_db, inputs_db=opts.inputs_db,
                         us_csv=opts.us_csv, research_only=opts.internal_research)
    else:
        end = opts.end or date.fromisoformat(window["asof_date"])
        start = opts.start or (date(2010, 1, 4) if opts.phase == "backfill" else None)
        if end > date.fromisoformat(window["asof_date"]) or (start and start > end):
            args.error("expected a completed date range")
        attempts = collect(opts.private, end.isoformat(), start=start.isoformat() if start else None)
        result = {market: {k: v for k, v in row.items() if k in ("status", "reason_codes", "rows_received", "rows_inserted", "requests")}
                  for market, row in attempts.items()}
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
