#!/usr/bin/env python3
"""Local Model B/C train-and-predict pipeline for this Ubuntu GPU PC.

Never run by a GitHub workflow (tests/test_runner_model_boundary.py). It never
commits or pushes; publishing the regenerated docs/data files stays manual.

  --stage evening    after the KRX close: back up, collect inputs, retrain the
                     8-layer direction model (Model B), the Model B close head
                     and Model C on the RTX 3090, predict the next session and
                     write docs/data/model_b_forecast.json and model_c_forecast.json.
  --stage premarket  06:30-09:00 KST on a KRX session day: refresh the US closes
                     into a dated CSV, refit the pre-registered premarket
                     variants (CPU, seconds), append them to the forward ledger
                     and, for variants that passed the gate, add issue_stage=
                     premarket rows to docs/data/model_b_forecast.json.

Every run writes logs/local_bc_pipeline/<run_id>/manifest.json with data and
model hashes, seeds, features, code commit and timings. --dry-run prints the
plan and touches nothing.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from roboquant.models import bc_local_pipeline as lp  # noqa: E402
from roboquant.models import bc_premarket as pm  # noqa: E402

GPU_PYTHON = Path("/usr/bin/python3.12")
VENV_PYTHON = ROOT / ".venv-forecast" / "bin" / "python"
DEFAULT_SQLITE = ROOT / "data" / "processed" / "index_forecast.sqlite"
RUN_ROOT = ROOT / "logs" / "local_bc_pipeline"
BACKUP_ROOT = ROOT / "models" / "dnn" / "backups"
LEDGER = ROOT / "reports" / "model_bc_forward" / "ledger.jsonl"
GATE_FILE = ROOT / "configs" / "model_bc_premarket_gate.json"
B_JSON = ROOT / "docs" / "data" / "model_b_forecast.json"
C_JSON = ROOT / "docs" / "data" / "model_c_forecast.json"
MARKETS = ("KOSPI", "KOSDAQ")
INPUT_SERIES = (
    "YAHOO_DXY", "YAHOO_BTC", "YAHOO_ETH", "YAHOO_WTI", "YAHOO_USDKRW",
    *(f"US_TREASURY_{t}Y" for t in (1, 3, 5, 10)), *(f"JP_JGB_{t}Y" for t in (1, 3, 5, 10)),
    *(f"KR_KTB_{t}Y" for t in (1, 3, 5, 10)),
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evening_steps(sqlite: Path) -> list[dict]:
    """The existing training scripts, in dependency order, unchanged."""
    db = str(sqlite)
    return [
        {"name": "collect_inputs", "python": str(VENV_PYTHON),
         "argv": ["scripts/collect_applied_model_inputs.py", "--sqlite", db], "allow_rc": (0, 1), "timeout": 900},
        {"name": "train_direction", "python": str(GPU_PYTHON),
         "argv": ["scripts/run_incremental_index_learner.py", "--sqlite", db,
                  "--output", "models/dnn/incremental_learning_curve.json"], "timeout": 3600},
        {"name": "predict_direction", "python": str(GPU_PYTHON),
         "argv": ["scripts/run_incremental_index_learner.py", "--predict", "--sqlite", db,
                  "--forecast-json", "docs/data/model_b_forecast.json"], "timeout": 900},
        {"name": "model_b_close", "python": str(GPU_PYTHON),
         "argv": ["scripts/run_model_b_close_forecast.py", "--sqlite", db,
                  "--forecast-json", "docs/data/model_b_forecast.json"], "timeout": 3600},
        {"name": "model_c", "python": str(GPU_PYTHON), "argv": ["scripts/run_model_c_extreme.py"], "timeout": 3600},
    ]


def run_step(step: dict, log_dir: Path) -> dict:
    started = time.time()
    log_path = log_dir / f"{step['name']}.log"
    with log_path.open("w", encoding="utf-8") as handle:
        done = subprocess.run([step["python"], *step["argv"]], cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
                              timeout=step.get("timeout", 3600), check=False)
    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
    return {"name": step["name"], "rc": done.returncode, "seconds": round(time.time() - started, 1),
            "log": str(log_path.relative_to(ROOT)), "tail": tail,
            "ok": done.returncode in step.get("allow_rc", (0,))}


def git_info() -> dict:
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty_tracked_files": [line for line in git("status", "--porcelain", "--untracked-files=no").splitlines()]}


def xkrx_sessions(now) -> pd.DataFrame:
    sys.path.append(str(ROOT / ".venv-forecast/lib/python3.12/site-packages"))
    from roboquant.index_forecast.reliable_inputs import session_schedule

    day = pd.Timestamp(now).tz_convert(lp.KST).date()
    frame, _source = session_schedule(day - timedelta(days=40), day + timedelta(days=20))
    return frame


def database_state(sqlite: Path) -> dict:
    with sqlite3.connect(f"file:{sqlite.resolve()}?mode=ro", uri=True) as conn:
        index = {m: conn.execute("SELECT MAX(session_date), MAX(ingested_at) FROM index_eod WHERE index_code=?",
                                 (m,)).fetchone() for m in MARKETS}
        series = {s: conn.execute("SELECT MAX(observation_date), MAX(ingested_at) FROM macro_series WHERE series_code=?",
                                  (s,)).fetchone() for s in INPUT_SERIES}
    return {"index_latest": {m: v[0] for m, v in index.items()}, "index_ingested": {m: v[1] for m, v in index.items()},
            "series_latest": {s: v[0] for s, v in series.items()}, "series_ingested": {s: v[1] for s, v in series.items()}}


def model_hashes() -> dict:
    out = {}
    for relative in lp.EVENING_FILES:
        path = ROOT / relative
        if path.is_file():
            out[relative] = lp.file_sha256(path)
    return out


_FACTS = """
import json, sys, torch
out = {}
for path in sys.argv[1:]:
    ck = torch.load(path, map_location="cpu", weights_only=False)
    schedule = ck.get("schedule") or {}
    keep = ("features", "last_session", "trained_through", "variant", "production_weight", "learning_rate",
            "update_epochs", "seed", "kind", "weight_decay", "direction_sha256")
    row = {k: ck[k] for k in keep if k in ck}
    row["schedule"] = schedule
    row.setdefault("seed", schedule.get("seed"))
    out[path.rsplit("/", 1)[-1]] = row
print(json.dumps(out, default=str))
"""


def checkpoint_facts() -> dict:
    """Seeds, features and trained-through dates stored in the checkpoints (read with the GPU interpreter)."""
    names = ("incremental_kospi.pt", "incremental_kosdaq.pt", "model_b_close_kospi.pt", "model_b_close_kosdaq.pt",
             "model_c_kospi.pt", "model_c_kosdaq.pt")
    paths = [str(ROOT / "models" / "dnn" / n) for n in names if (ROOT / "models" / "dnn" / n).is_file()]
    done = subprocess.run([str(GPU_PYTHON), "-c", _FACTS, *paths], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        return {"error": done.stderr[-500:]}
    return json.loads(done.stdout.strip().splitlines()[-1])


def write_manifest(run_dir: Path, manifest: dict) -> None:
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n")


def lock_or_exit(run_root: Path):
    run_root.mkdir(parents=True, exist_ok=True)
    handle = (run_root / ".lock").open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another local B/C pipeline run holds logs/local_bc_pipeline/.lock") from None
    return handle


def stamp_stage(path: Path, run_id: str) -> None:
    payload = json.loads(path.read_text())
    payload["issue_stage"] = "evening"
    payload["pipeline_run_id"] = run_id
    payload.pop("premarket_markets", None)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


# ------------------------------------------------------------------ evening

def evening(args, now, run_id: str) -> int:
    sessions = xkrx_sessions(now)
    plan = lp.evening_window(now, sessions)
    steps = evening_steps(Path(args.sqlite))
    if args.skip_refresh:
        steps = steps[1:]
    if args.dry_run:
        print(json.dumps({"stage": "evening", "clock": now.isoformat(), "plan": plan,
                          "backup": [p for p in lp.EVENING_FILES if (ROOT / p).is_file()],
                          "steps": [[s["python"], *s["argv"]] for s in steps],
                          "writes": ["models/dnn/incremental_*.pt", "models/dnn/model_b_close_*.pt",
                                     "models/dnn/model_c_*.pt", str(B_JSON.relative_to(ROOT)), str(C_JSON.relative_to(ROOT))]},
                         ensure_ascii=False, indent=2))
        return 0
    run_dir = RUN_ROOT / run_id
    run_dir.mkdir(parents=True)
    manifest = {"run_id": run_id, "stage": "evening", "issue_stage": "evening", "started_at": datetime.now(UTC).isoformat(),
                "plan": plan, "git": git_info(), "production_weight": 0, "steps": []}
    if plan["status"] != "ready":
        manifest["status"] = plan["status"]
        write_manifest(run_dir, manifest)
        print(json.dumps({"status": plan["status"], "reason": plan.get("reason")}))
        return 0
    hashes = lp.backup_files(ROOT, BACKUP_ROOT / run_id, lp.EVENING_FILES)
    manifest["backup"] = {"dir": str((BACKUP_ROOT / run_id).relative_to(ROOT)), "sha256": hashes}
    manifest["models_before"] = model_hashes()

    def fail(reason: str) -> int:
        manifest["status"] = "failed"
        manifest["reason"] = reason
        manifest["restored"] = lp.restore_files(ROOT, BACKUP_ROOT / run_id, hashes)
        write_manifest(run_dir, manifest)
        print(json.dumps({"status": "failed", "reason": reason, "manifest": str(run_dir / "manifest.json")}))
        return 1

    for index, step in enumerate(steps):
        if index == (0 if args.skip_refresh else 1):
            state = database_state(Path(args.sqlite))
            manifest["inputs"] = state
            manifest["sqlite_sha256_before_training"] = lp.file_sha256(Path(args.sqlite))
            behind = {m: d for m, d in state["index_latest"].items() if d != plan["asof_date"]}
            if behind:
                return fail(f"index closes not at {plan['asof_date']}: {behind}")
            manifest["input_staleness_sessions"] = lp.staleness(
                state["series_latest"], plan["asof_date"], sessions["date"].tolist())
        result = run_step(step, run_dir)
        manifest["steps"].append(result)
        if not result["ok"]:
            return fail(f"step {step['name']} exited {result['rc']}")
    for path, schema, expected in ((B_JSON, "model_b_forecast_v1", True), (C_JSON, "model_c_forecast_v1", True)):
        payload = json.loads(path.read_text())
        problems = lp.check_forecast_payload(payload, schema, require_expected=expected)
        wrong = [row["market"] for row in payload.get("markets", []) if row.get("asof_date") != plan["asof_date"]]
        if wrong:
            problems.append(f"asof_date is not {plan['asof_date']} for {wrong}")
        if problems:
            return fail(f"{path.name}: {problems}")
        stamp_stage(path, run_id)
    manifest["models_after"] = model_hashes()
    manifest["checkpoints"] = checkpoint_facts()
    manifest["outputs"] = {str(p.relative_to(ROOT)): lp.file_sha256(p) for p in (B_JSON, C_JSON)}
    manifest["pruned_backups"] = lp.prune_backups(BACKUP_ROOT, args.keep_backups)
    manifest["status"] = "ok"
    write_manifest(run_dir, manifest)
    print(json.dumps({"status": "ok", "asof": plan["asof_date"], "target": plan["target_date"],
                      "manifest": str(run_dir / "manifest.json")}, ensure_ascii=False))
    return 0


# ------------------------------------------------------------------ premarket

def premarket_design(conn, market: str, us_csv: Path, asof: str, target: str, cutoff_utc: pd.Timestamp,
                     reporting_cutoff: pd.Timestamp) -> dict:
    """Matured training rows and the live row for one market (no future label)."""
    from roboquant.index_forecast.us_market_inputs import build_us_features

    learner = _load("incremental_index_learner", ROOT / "scripts" / "run_incremental_index_learner.py")
    labeled, latest, features = learner.market_frame(conn, market)
    live_day = latest["session_date"].iloc[-1].strftime("%Y-%m-%d")
    if live_day != asof:
        raise RuntimeError(f"{market} latest complete evening row is {live_day}, not {asof}")
    x, _y, dates, _ = learner.make_windows(labeled, features)
    late = lp.labels_before(dates, asof)
    if late:
        raise RuntimeError(f"{market} training rows dated after the as-of session: {late[:3]}")
    closes = pd.read_sql_query("SELECT session_date, close FROM index_eod WHERE index_code=? ORDER BY session_date",
                               conn, params=[market])
    nxt = dict(zip(closes["session_date"][:-1], closes["session_date"][1:], strict=True))
    ret = dict(zip(closes["session_date"], closes["close"].shift(-1) / closes["close"] - 1.0, strict=True))
    label_dates = [nxt[d] for d in dates]
    if max(label_dates) > asof:
        raise RuntimeError(f"{market} a training label is dated after {asof}")
    cutoffs = {d: pd.Timestamp(f"{nxt[d]}T08:00:00+09:00").tz_convert("UTC") for d in dates}
    cutoffs[asof] = cutoff_utc
    observations = pd.read_csv(us_csv)
    built = build_us_features(observations, asof_cutoffs=cutoffs, live_asof_date=asof, reporting_cutoff=reporting_cutoff)
    us = built["features"].set_index("asof_date")
    if asof not in us.index:
        raise RuntimeError(f"{market} US features unavailable at the cutoff: {built['report'].get('latest_snapshot')}")
    rows = [i for i, d in enumerate(dates) if d in us.index]
    x_train = np.concatenate([x[rows, -1, :], us.loc[[dates[i] for i in rows], pm.US6].to_numpy()], axis=1)
    r_train = np.array([ret[dates[i]] for i in rows], dtype=np.float64)
    live_f24 = latest[features].iloc[-1].to_numpy(dtype=np.float64)
    x_live = np.concatenate([live_f24, us.loc[asof, pm.US6].to_numpy(dtype=np.float64)])[None, :]
    return {"x_train": x_train, "r_train": r_train, "x_live": x_live, "close": float(latest["close"].iloc[-1]),
            "features": list(features) + pm.US6, "n_train": len(rows), "train_first": dates[rows[0]],
            "train_last_label": label_dates[rows[-1]], "us_snapshot": built["report"].get("latest_snapshot"),
            "dropped_rows_without_us": len(dates) - len(rows)}


def premarket(args, now, run_id: str) -> int:
    sessions = xkrx_sessions(now)
    plan = lp.premarket_window(now, sessions)
    gate = json.loads(GATE_FILE.read_text()) if GATE_FILE.is_file() else {"published": {}}
    if args.dry_run:
        print(json.dumps({"stage": "premarket", "clock": now.isoformat(), "plan": plan, "markets": args.markets,
                          "variants": list(pm.VARIANTS), "published_by_gate": gate.get("published"),
                          "refresh": [str(VENV_PYTHON), "scripts/refresh_premarket_prices.py", "--sqlite",
                                      "<scratch copy of the live sqlite>", "--output-dir", "data/processed"],
                          "writes": [str(LEDGER.relative_to(ROOT)), str(B_JSON.relative_to(ROOT)) + " (premarket_markets)"]},
                         ensure_ascii=False, indent=2))
        return 0
    run_dir = RUN_ROOT / run_id
    run_dir.mkdir(parents=True)
    manifest = {"run_id": run_id, "stage": "premarket", "issue_stage": "premarket", "plan": plan,
                "started_at": datetime.now(UTC).isoformat(), "git": git_info(), "production_weight": 0}
    if plan["status"] != "ready":
        manifest["status"] = plan["status"]
        write_manifest(run_dir, manifest)
        print(json.dumps({"status": plan["status"], "reason": plan.get("reason")}))
        return 0
    us_csv = Path(args.us_csv) if args.us_csv else None
    if not args.skip_refresh:
        scratch = run_dir / "kr_scratch.sqlite"
        with sqlite3.connect(f"file:{Path(args.sqlite).resolve()}?mode=ro", uri=True) as src, sqlite3.connect(scratch) as dst:
            src.backup(dst)
        step = {"name": "refresh_us", "python": str(VENV_PYTHON),
                "argv": ["scripts/refresh_premarket_prices.py", "--sqlite", str(scratch), "--output-dir", "data/processed"],
                "timeout": 600}
        result = run_step(step, run_dir)
        manifest["steps"] = [result]
        scratch.unlink(missing_ok=True)
        if not result["ok"]:
            manifest["status"] = "failed"
            write_manifest(run_dir, manifest)
            return 1
        for line in reversed(result["tail"]):
            if line.startswith("{"):
                us_csv = Path(json.loads(line)["us_csv"])
                break
    if us_csv is None:
        raise SystemExit("no US observation CSV (pass --us-csv with --skip-refresh)")
    issued = pd.Timestamp(datetime.now(UTC))
    if issued.tz_convert(lp.KST) >= pd.Timestamp(plan["open_at"]):
        manifest["status"] = "missed"
        write_manifest(run_dir, manifest)
        return 0
    manifest["us_csv"] = {"path": str(us_csv), "sha256": lp.file_sha256(us_csv)}
    manifest["sqlite_sha256"] = lp.file_sha256(Path(args.sqlite))
    payload = json.loads(B_JSON.read_text())
    published_rows = []
    ledger_rows = []
    with sqlite3.connect(f"file:{Path(args.sqlite).resolve()}?mode=ro", uri=True) as conn:
        for market in args.markets:
            design = premarket_design(conn, market, us_csv, plan["asof_date"], plan["target_date"], issued, issued)
            manifest[f"{market}_design"] = {k: v for k, v in design.items() if k not in ("x_train", "r_train", "x_live")}
            for variant, spec in pm.VARIANTS.items():
                fitted = pm.fit_predict(variant, design["x_train"], design["r_train"], design["x_live"])
                value = float(fitted["value"][0])
                is_published = bool(gate.get("published", {}).get(market, {}).get(variant))
                row = {"schema_version": "model_bc_premarket_ledger_v1", "variant": variant, "model": spec["model"],
                       "output": spec["output"], "market": market, "issue_stage": "premarket",
                       "asof_date": plan["asof_date"], "target_date": plan["target_date"],
                       "issued_at": issued.isoformat(), "prediction_cutoff": issued.isoformat(),
                       "nominal_cutoff": plan["nominal_cutoff"], "close": design["close"], "value": value,
                       "n_train": fitted["n_train"], "alpha": fitted.get("alpha"),
                       "train_last_label": design["train_last_label"], "features": design["features"],
                       "us_sessions": {s: (info or {}).get("selected_session") for s, info in (design["us_snapshot"] or {}).items()},
                       "us_first_seen": {s: (info or {}).get("first_seen_at") for s, info in (design["us_snapshot"] or {}).items()},
                       "us_csv_sha256": manifest["us_csv"]["sha256"], "code_commit": manifest["git"]["commit"],
                       "published": is_published, "production_weight": 0, "run_id": run_id}
                ledger_rows.append(row)
                if is_published and spec["output"] == "expected_return":
                    published_rows.append({"market": market, "issue_stage": "premarket", "variant": variant,
                                           "asof_date": plan["asof_date"], "target_date": plan["target_date"],
                                           "prediction_cutoff": issued.isoformat(), "close": round(design["close"], 2),
                                           "expected_return": value,
                                           "expected_index": design["close"] * (1.0 + value),
                                           "production_weight": 0,
                                           "validation": gate.get("validation", {}).get(market, {}).get(variant)})
    manifest["ledger"] = [lp.append_ledger(LEDGER, row)["row_sha256"] for row in ledger_rows]
    if published_rows:
        payload["premarket_markets"] = published_rows
        problems = lp.check_forecast_payload(payload, "model_b_forecast_v1", require_expected=False)
        if problems:
            manifest["status"] = "failed"
            manifest["reason"] = problems
            write_manifest(run_dir, manifest)
            return 1
        B_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        manifest["outputs"] = {str(B_JSON.relative_to(ROOT)): lp.file_sha256(B_JSON)}
    manifest["status"] = "ok"
    write_manifest(run_dir, manifest)
    print(json.dumps({"status": "ok", "target": plan["target_date"], "published_rows": len(published_rows),
                      "ledger_rows": len(ledger_rows), "manifest": str(run_dir / "manifest.json")}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=lp.STAGES, required=True)
    parser.add_argument("--markets", nargs="+", choices=MARKETS, default=list(MARKETS),
                        help="premarket only; the evening scripts always train both markets")
    parser.add_argument("--sqlite", default=str(DEFAULT_SQLITE))
    parser.add_argument("--us-csv", help="premarket: use this US CSV (with --skip-refresh)")
    parser.add_argument("--skip-refresh", action="store_true", help="use the inputs already collected")
    parser.add_argument("--keep-backups", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true", help="print the plan; write, train and fetch nothing")
    parser.add_argument("--now", help="clock override (ISO with offset); only with --dry-run")
    args = parser.parse_args(argv)
    if args.now and not args.dry_run:
        raise SystemExit("--now is only allowed with --dry-run")
    if args.stage == "evening" and set(args.markets) != set(MARKETS):
        raise SystemExit("the evening stage trains both markets together")
    now = pd.Timestamp(args.now) if args.now else pd.Timestamp(datetime.now(UTC))
    if now.tzinfo is None:
        raise SystemExit("--now needs a UTC offset")
    run_id = now.tz_convert(lp.KST).strftime("%Y%m%dT%H%M%S") + f"_{args.stage}"
    if args.dry_run:
        return evening(args, now, run_id) if args.stage == "evening" else premarket(args, now, run_id)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    handle = lock_or_exit(RUN_ROOT)
    try:
        return evening(args, now, run_id) if args.stage == "evening" else premarket(args, now, run_id)
    finally:
        handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
