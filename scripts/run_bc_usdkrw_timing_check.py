#!/usr/bin/env python3
"""Unregistered leakage diagnostic: how much do B/C lean on the same-day USDKRW bar?

The evening feature usdkrw_return_1d uses the Yahoo KRW=X daily bar dated on
the as-of session itself. Historical bars are full-day closes (London day, so
they end hours after the 15:30 KST KRX close and after the US open), while a
live evening run stores the in-progress quote. This check reruns the current
method (B0, C0, seed 44) and the evening logistic (WB2) with that one feature
lagged by one day, on the same chronological folds, and reports the Brier
change. It is a diagnostic, not a candidate; nothing is selected from it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bc_eval", ROOT / "scripts" / "run_bc_chronological_eval.py")
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)
ev = E.ev
learner = E.learner


def lagged_market_frame(original):
    def market_frame(conn, market):
        labeled, latest, columns = original(conn, market)
        usd = learner.load_series(conn, "YAHOO_USDKRW")
        for frame in (labeled, latest):
            dates = frame["session_date"].dt.date
            lag = learner.align(dates, usd, "usdkrw_lag1", 1, False)
            merged = frame[["_asof"]].merge(lag, on="_asof", how="left")
            frame["usdkrw_return_1d"] = merged["usdkrw_lag1"].to_numpy()
        keep = labeled["usdkrw_return_1d"].notna()
        return labeled.loc[keep].reset_index(drop=True), latest.dropna(subset=columns).reset_index(drop=True), columns
    return market_frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--us-csv", required=True)
    parser.add_argument("--us-receipt", required=True)
    parser.add_argument("--reference", required=True, help="eval dir with the lag-0 predictions")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    import torch

    device, _gpu = E.model_c.require_cuda(torch)
    learner.market_frame = lagged_market_frame(learner.market_frame)
    conn = sqlite3.connect(f"file:{Path(args.sqlite).resolve()}?mode=ro", uri=True)
    report = {"note": "unregistered diagnostic; usdkrw_return_1d lagged one day; seed 44; same folds", "markets": {}}
    for market in ("KOSPI", "KOSDAQ"):
        data = E.prepare(conn, market, Path(args.us_csv), args.us_receipt)
        ref = np.load(Path(args.reference) / f"{market.lower()}_predictions.npz")
        if list(ref["dates"]) != data["dates"]:
            raise SystemExit(f"{market} lagged frame dates differ from the reference")
        n = len(data["y"])
        c_p, c_ret, close_ret = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
        checked = {"seen": True}

        def on_fold(train_end, block_end, model, mean, std, data=data, c_p=c_p, c_ret=c_ret, close_ret=close_ret,
                    checked=checked):
            state = E.model_c.clone_state(model)
            E.close_head_fold(torch, device, data, model, mean, std, train_end, block_end, close_ret)
            E.model_c_fold(torch, device, data, 44, state, mean, std, train_end, block_end, c_p, c_ret, checked)

        b0 = E.run_backbone(torch, device, data, x=data["x"], valid=np.ones(n, bool), schedule=E.schedule_for(market),
                            seed=44, weighted=False, first=E.FIRST, on_fold=on_fold)
        linear = E.fit_linear(data)
        dates = np.array(data["dates"])
        y, r = data["y"], data["r"]
        sel = (np.arange(n) >= E.FIRST) & (dates >= E.SELECTION_START) & (dates < E.SELECTION_END)
        y26 = (np.arange(n) >= E.FIRST) & (dates >= E.SELECTION_END)
        out = {}
        for name, lag0, lag1, masks in (
            ("B0", ref["B0_s44"], b0, {"selection": sel, "y2026": y26}),
            ("WB2", ref["WB2"], linear["WB2"], {"selection": sel, "y2026": y26}),
            ("C0", ref["C0_s44"], c_p, {"selection_1.5": sel & (np.abs(r) >= 0.015), "y2026_3": y26 & (np.abs(r) >= 0.03)}),
        ):
            out[name] = {}
            for label, mask in masks.items():
                ok = mask & np.isfinite(lag0) & np.isfinite(lag1)
                diff = ev.brier(lag1[ok], y[ok]) - ev.brier(lag0[ok], y[ok])
                out[name][label] = {"n": int(ok.sum()), "brier_lag0": float(ev.brier(lag0[ok], y[ok]).mean()),
                                    "brier_lag1": float(ev.brier(lag1[ok], y[ok]).mean()),
                                    "lag1_minus_lag0": ev.block_bootstrap(diff, block=10)}
        report["markets"][market] = out
        print(market, json.dumps({k: {s: (v["n"], round(v["brier_lag0"], 4), round(v["brier_lag1"], 4)) for s, v in d.items()}
                                  for k, d in out.items()}), flush=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
