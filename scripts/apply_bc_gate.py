#!/usr/bin/env python3
"""Apply the pre-registered Model B/C gate to scores.json and print a table.

Gate text: reports/model_c_kospi_20261125/preregistration.json (Model C KOSPI)
and preregistration_bc_wide.json (Model B, Model C KOSDAQ). This script only
reads scores; it never fits or selects anything else.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.models.bc_chrono_eval import holm  # noqa: E402

C_CANDIDATES = {"K1": "evening", "K2": "evening", "K3": "evening", "K4": "premarket", "K5": "premarket"}
B_CANDIDATES = {"WB1": "evening", "WB2": "evening", "WB3": "premarket", "WB4": "premarket"}
CLOSE_CANDIDATES = {"WB5": "evening", "WB6": "evening", "WB7": "premarket"}
COIN_LOG_LOSS = 0.6931471805599453


def p_cand(entry: dict, comparators) -> float:
    return max(entry["vs"][name]["brier"]["p_one_sided"] for name in comparators)


def all_upper_below_zero(entry: dict, comparators) -> bool:
    return all(entry["vs"][name]["brier"]["high"] < 0 for name in comparators)


def gate_direction(block_sel: dict, block_conf: dict, candidates: dict, comparators, *, ll_limit, conf_ece_key, conf_ece_max):
    pvals = {key: p_cand(block_sel[key], comparators) for key in candidates}
    adjusted = holm(pvals)
    rows = {}
    for key, stage in candidates.items():
        sel = block_sel[key]
        reasons = []
        if not all_upper_below_zero(sel, comparators):
            reasons.append("selection_ci_upper_not_below_0")
        if adjusted[key] >= 0.05:
            reasons.append("holm_p_not_below_0.05")
        limit = ll_limit(block_sel) if callable(ll_limit) else ll_limit
        if sel["candidate"]["log_loss"] > limit:
            reasons.append("selection_log_loss_worse_than_limit")
        rows[key] = {"stage": stage, "p_cand": pvals[key], "p_holm": adjusted[key], "eligible": not reasons,
                     "reasons": reasons, "selection_brier": sel["candidate"]["brier"]}
    decision = {}
    for stage in ("evening", "premarket"):
        eligible = [k for k, v in rows.items() if v["stage"] == stage and v["eligible"]]
        if not eligible:
            decision[stage] = {"selected": None, "passed": False}
            continue
        best = min(eligible, key=lambda k: rows[k]["selection_brier"])
        conf = block_conf[best]
        reasons = []
        if not all_upper_below_zero(conf, comparators):
            reasons.append("confirmation_ci_upper_not_below_0")
        if conf["candidate"][conf_ece_key] > conf_ece_max:
            reasons.append(f"confirmation_{conf_ece_key}_above_{conf_ece_max}")
        decision[stage] = {"selected": best, "passed": not reasons, "reasons": reasons}
    return rows, decision


def gate_close(sel: dict, conf: dict) -> tuple[dict, dict]:
    comps = ("previous_close", "Bclose")
    pvals = {k: max(sel[k]["vs"][c]["diff"]["p_one_sided"] for c in comps) for k in CLOSE_CANDIDATES}
    adjusted = holm(pvals)
    rows = {}
    for key, stage in CLOSE_CANDIDATES.items():
        reasons = []
        if not all(sel[key]["vs"][c]["diff"]["high"] < 0 for c in comps):
            reasons.append("selection_ci_upper_not_below_0")
        if adjusted[key] >= 0.05:
            reasons.append("holm_p_not_below_0.05")
        rows[key] = {"stage": stage, "p_cand": pvals[key], "p_holm": adjusted[key], "eligible": not reasons,
                     "reasons": reasons, "selection_mae": sel[key]["mae_points"]}
    decision = {}
    for stage in ("evening", "premarket"):
        eligible = [k for k, v in rows.items() if v["stage"] == stage and v["eligible"]]
        if not eligible:
            decision[stage] = {"selected": None, "passed": False}
            continue
        best = min(eligible, key=lambda k: rows[k]["selection_mae"])
        ok = all(conf[best]["vs"][c]["diff"]["high"] < 0 for c in comps)
        decision[stage] = {"selected": best, "passed": ok,
                           "reasons": [] if ok else ["confirmation_ci_upper_not_below_0"]}
    return rows, decision


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scores")
    parser.add_argument("--out")
    args = parser.parse_args()
    scores = json.loads(Path(args.scores).read_text())
    report = {}
    for market, res in scores["markets"].items():
        c_rows, c_dec = gate_direction(res["C"]["selection_1.5"], res["C"]["y2026_3"], C_CANDIDATES,
                                       ("coin", "event_up_rate", "C0"), ll_limit=COIN_LOG_LOSS,
                                       conf_ece_key="ece5", conf_ece_max=0.10)
        b_rows, b_dec = gate_direction(res["B"]["selection"], res["B"]["y2026"], B_CANDIDATES,
                                       ("coin", "up_rate", "B0"),
                                       ll_limit=lambda block: block["baseline:up_rate_all"]["log_loss"],
                                       conf_ece_key="ece10", conf_ece_max=0.05)
        k_rows, k_dec = gate_close(res["B_close"]["selection"], res["B_close"]["y2026"])
        report[market] = {"C": {"rows": c_rows, "decision": c_dec}, "B": {"rows": b_rows, "decision": b_dec},
                          "B_close": {"rows": k_rows, "decision": k_dec}}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
