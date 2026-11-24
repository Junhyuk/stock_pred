#!/usr/bin/env python3
"""Score the local Model B/C forward ledger against realised closes.

Reads reports/model_bc_forward/ledger.jsonl (hash-chained, append-only) and
index_eod. A row matures when the target session's close is stored. Scoring
follows configs/model_bc_forward_rule.json; nothing here changes the ledger.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.models import bc_chrono_eval as ev  # noqa: E402
from roboquant.models import bc_local_pipeline as lp  # noqa: E402

RULE = ROOT / "configs" / "model_bc_forward_rule.json"
LEVEL = {"value": 0.95}


def boot(difference, block=10):
    return ev.block_bootstrap(difference, block=block, level=LEVEL["value"])


def realised(conn, market: str) -> dict:
    rows = conn.execute("SELECT session_date, close FROM index_eod WHERE index_code=? ORDER BY session_date",
                        (market,)).fetchall()
    return {day: close for day, close in rows if close is not None}


def first_rows(rows: list[dict]) -> dict:
    """First issued row per (variant, market, target); later duplicates cannot exist by construction."""
    out = {}
    for row in rows:
        out.setdefault((row["variant"], row["market"], row["target_date"]), row)
    return out


def expanding_up_rate(closes: dict, asof: str, threshold: float | None) -> float:
    days = sorted(d for d in closes if d <= asof)
    ret = [closes[b] / closes[a] - 1.0 for a, b in zip(days[:-1], days[1:], strict=True)]
    if threshold is not None:
        ret = [r for r in ret if abs(r) >= threshold]
    ups = sum(r > 0 for r in ret)
    return (ups + 1.0) / (len(ret) + 2.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", default=str(ROOT / "reports" / "model_bc_forward" / "ledger.jsonl"))
    parser.add_argument("--sqlite", default=str(ROOT / "data" / "processed" / "index_forecast.sqlite"))
    parser.add_argument("--out")
    args = parser.parse_args()
    rule = json.loads(RULE.read_text())
    LEVEL["value"] = float(rule.get("ci_level", 0.95))
    problems = lp.verify_ledger(Path(args.ledger))
    rows = [json.loads(line) for line in Path(args.ledger).read_text().splitlines() if line.strip()]
    issued = first_rows(rows)
    report = {"ledger_rows": len(rows), "ledger_problems": problems, "rule": rule.get("id"), "tests": {}}
    with sqlite3.connect(f"file:{Path(args.sqlite).resolve()}?mode=ro", uri=True) as conn:
        closes = {m: realised(conn, m) for m in ("KOSPI", "KOSDAQ")}
    for test in rule["tests"]:
        market, variant = test["market"], test["variant"]
        matured = []
        for (v, m, target), row in sorted(issued.items(), key=lambda kv: kv[0][2]):
            if v != variant or m != market or target not in closes[m] or row["asof_date"] not in closes[m]:
                continue
            opened = datetime.fromisoformat(f"{target}T09:00:00+09:00")
            if datetime.fromisoformat(row["issued_at"]) >= opened:
                # Issued at or after the XKRX open: not a valid forecast for that session.
                continue
            actual = closes[m][target] / closes[m][row["asof_date"]] - 1.0
            if test.get("event_threshold") and abs(actual) < test["event_threshold"]:
                continue
            reference = issued.get((test.get("paired_reference"), m, target)) if test.get("paired_reference") else None
            matured.append({"row": row, "actual": actual, "reference": reference})
        entry = {"n_matured": len(matured), "decision_n": test["decision_n"], "status": "accumulating"}
        if matured:
            if test["metric"] == "mae_points":
                close = np.array([x["row"]["close"] for x in matured])
                pred = np.array([x["row"]["value"] for x in matured])
                act = np.array([x["actual"] for x in matured])
                err = np.abs(close * (pred - act))
                entry["mae_points"] = float(err.mean())
                entry["vs_previous_close"] = boot(err - np.abs(close * act), block=10)
                paired = [x for x in matured if x["reference"] and x["reference"].get("expected_return") is not None]
                if paired:
                    e1 = np.array([abs(x["row"]["close"] * (x["row"]["value"] - x["actual"])) for x in paired])
                    e2 = np.array([abs(x["row"]["close"] * (x["reference"]["expected_return"] - x["actual"])) for x in paired])
                    entry["vs_reference"] = {"n": len(paired), **boot(e1 - e2, block=10)}
            else:
                p = np.array([x["row"]["value"] for x in matured])
                y = np.array([1.0 if x["actual"] > 0 else 0.0 for x in matured])
                base = np.array([expanding_up_rate(closes[market], x["row"]["asof_date"], test.get("event_threshold"))
                                 for x in matured])
                entry["candidate"] = ev.summarize(p, y, test.get("ece_bins", 10))
                entry["vs_coin"] = boot(ev.brier(p, y) - 0.25, block=10)
                entry["vs_up_rate"] = boot(ev.brier(p, y) - ev.brier(base, y), block=10)
                paired = [(x, x["reference"]) for x in matured if x["reference"]]
                if paired:
                    pp = np.array([x["row"]["value"] for x, _ in paired])
                    pr = np.array([ref["value"] for _, ref in paired])
                    yy = np.array([1.0 if x["actual"] > 0 else 0.0 for x, _ in paired])
                    entry["vs_reference"] = {"n": len(paired), **boot(ev.brier(pp, yy) - ev.brier(pr, yy), block=10)}
        first = entry.get("vs_previous_close") or entry.get("vs_coin")
        if first and len(matured) >= 40:
            limit = 5.0 if test["metric"] == "mae_points" else 0.02
            entry["interim_safety_stop"] = bool(first["mean"] > limit)
        if len(matured) >= test["decision_n"]:
            keys = [k for k in ("vs_previous_close", "vs_coin", "vs_up_rate", "vs_reference") if k in entry]
            ok = all(entry[k]["high"] < 0 for k in keys)
            if "candidate" in entry and test.get("ece_max") is not None:
                ok = ok and entry["candidate"][f"ece{test.get('ece_bins', 10)}"] <= test["ece_max"]
            entry["status"] = "pass_needs_independent_review" if ok else "fail"
        report["tests"][f"{variant}|{market}"] = entry
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
