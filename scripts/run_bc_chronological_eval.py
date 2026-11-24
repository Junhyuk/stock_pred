#!/usr/bin/env python3
"""Chronological from-scratch evaluation of Model B and Model C candidates.

Research only; production_weight stays 0 and nothing published is written.
Every score comes from weights, scalers and heads fit on windows that end
before the fold start. The candidate list, metrics and gate are fixed in
reports/model_c_kospi_20261125/preregistration*.json before this runs.

Stage 1 (``--stage fit``) runs the GPU models and stores per-window
predictions. Stage 2 (``--stage score``) only reads those predictions.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Append only: system CUDA torch/numpy stay first; the venv supplies exchange_calendars.
sys.path.append(str(ROOT / ".venv-forecast/lib/python3.12/site-packages"))

from roboquant.models import bc_chrono_eval as ev  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


learner = _load("incremental_index_learner", ROOT / "scripts" / "run_incremental_index_learner.py")
model_c = _load("run_model_c_extreme", ROOT / "scripts" / "run_model_c_extreme.py")
model_b = _load("run_model_b_close_forecast", ROOT / "scripts" / "run_model_b_close_forecast.py")

FIRST = 252
BLOCK = 60
SEEDS = (44, 1, 2, 3, 4)
EXTREME = 0.03
SELECTION_END = "2026-01-01"
SELECTION_START = "2022-06-02"
DEFAULT_SCHEDULE = {"initial_epochs": 6, "initial_lr": 2e-4, "update_epochs": 2, "update_lr": 2e-4}
C_FINETUNE_LR = 1e-5
C_FINETUNE_EPOCHS = 3
CLOSE_HEAD = {"kind": "linear", "weight_decay": 10.0, "learning_rate": 1e-3, "epochs": 200, "seed": 44}
US6 = ["us_spx_return_1d", "us_nasdaq_return_1d", "us_sox_return_1d", "us_vix_level", "us_vix_return_1d", "sox_nasdaq_spread_1d"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def schedule_for(market: str) -> dict:
    return dict(learner.KOSPI_FINETUNE) if market == "KOSPI" else dict(DEFAULT_SCHEDULE)


def premarket_cutoffs(conn, market: str, dates: list[str], latest: str | None = None) -> dict:
    """08:00 KST on the next stored XKRX session of each as-of date."""
    sessions = pd.read_sql_query(
        "SELECT session_date FROM index_eod WHERE index_code = ? ORDER BY session_date", conn, params=[market]
    )["session_date"].tolist()
    nxt = dict(zip(sessions[:-1], sessions[1:]))
    if latest is not None and latest not in nxt:
        nxt[latest] = learner.next_session(latest)
    out = {}
    for day in dates:
        target = nxt.get(day)
        if target is None:
            continue
        out[day] = pd.Timestamp(f"{target}T08:00:00+09:00").tz_convert("UTC")
    return out


def us_frame(conn, market: str, dates: list[str], us_csv: Path, reporting_cutoff: str, latest: str | None = None):
    from roboquant.index_forecast.us_market_inputs import build_us_features

    observations = pd.read_csv(us_csv)
    cutoffs = premarket_cutoffs(conn, market, dates, latest)
    built = build_us_features(observations, asof_cutoffs=cutoffs, reporting_cutoff=reporting_cutoff)
    features = built["features"].set_index("asof_date")
    return features.reindex(dates)[US6], cutoffs, built["report"]


def prepare(conn, market: str, us_csv: Path, reporting_cutoff: str) -> dict:
    labeled, latest, features = learner.market_frame(conn, market)
    x, y, dates, features = learner.make_windows(labeled, features)
    targets = model_c.load_session_targets(conn, market)
    r = np.array([targets[d]["actual_return"] for d in dates], dtype=np.float64)
    close = np.array([targets[d]["close"] for d in dates], dtype=np.float64)
    if not np.isfinite(r).all() or not np.array_equal((r > 0).astype(np.float32), y):
        raise SystemExit(f"{market} labels and returns disagree")
    row_dates = labeled["session_date"].dt.strftime("%Y-%m-%d").tolist()
    us_rows, cutoffs, report = us_frame(conn, market, row_dates, us_csv, reporting_cutoff)
    us_values = us_rows.to_numpy(dtype=np.float32)
    windows_us = []
    valid_us = []
    for end in range(learner.LOOKBACK - 1, len(row_dates)):
        block = us_values[end - learner.LOOKBACK + 1:end + 1]
        ok = bool(np.isfinite(block).all())
        valid_us.append(ok)
        windows_us.append(block if ok else np.zeros_like(block))
    valid_us = np.asarray(valid_us)
    x_pre = np.concatenate([x, np.stack(windows_us)], axis=2)
    return {
        "market": market, "x": x, "x_pre": x_pre, "valid_us": valid_us, "y": y.astype(np.float64), "r": r,
        "close": close, "dates": dates, "features": list(features), "us_report": report,
        "cutoff_first": str(min(cutoffs.values())), "cutoff_last": str(max(cutoffs.values())),
    }


# ---------------------------------------------------------------- transformers

def train_epochs(torch, model, x, y, device, epochs, learning_rate, weights=None):
    """learner.train_epochs, with optional per-row loss weights."""
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=0.01)
    features = torch.from_numpy(x.astype(np.float32))
    labels = torch.from_numpy(y.astype(np.float32))
    w = None if weights is None else torch.from_numpy(np.asarray(weights, dtype=np.float32))
    loss_fn = torch.nn.BCEWithLogitsLoss() if w is None else torch.nn.BCEWithLogitsLoss(reduction="none")
    model.train()
    for _epoch in range(epochs):
        order = torch.randperm(len(features))
        for start in range(0, len(features), 64):
            batch = order[start:start + 64]
            optimizer.zero_grad(set_to_none=True)
            logits = model(features[batch].to(device))
            loss = loss_fn(logits, labels[batch].to(device))
            if w is not None:
                loss = (loss * w[batch].to(device)).mean()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()


def run_backbone(torch, device, data, *, x, valid, schedule, seed, weighted, first, on_fold=None):
    """Incremental schedule started at ``first``; p for every scored window."""
    n = len(x)
    usable = np.flatnonzero(valid)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    model = learner.create_large_index_model(x.shape[-1], learner.MODEL_CONFIG).to(device)
    p = np.full(n, np.nan)
    y = data["y"]
    r = data["r"]

    def fit_rows(end, epochs, lr, mean, std):
        rows = usable[usable < end]
        w = ev.abs_return_weights(r, rows) if weighted else None
        train_epochs(torch, model, learner.apply_scale(x[rows], mean, std), y[rows], device, epochs, lr, w)

    rows0 = usable[usable < first]
    mean, std = learner.scale_fit(x[rows0])
    fit_rows(first, schedule["initial_epochs"], schedule["initial_lr"], mean, std)
    for train_end, block_end in learner.iter_learning_blocks(n, first, BLOCK):
        mean, std = learner.scale_fit(x[usable[usable < train_end]])
        block_rows = np.arange(train_end, block_end)
        block_rows = block_rows[valid[block_rows]]
        if len(block_rows):
            p[block_rows] = learner.predict(torch, model, learner.apply_scale(x[block_rows], mean, std), device)
        if on_fold is not None:
            saved = model_c.capture_rng(torch)
            on_fold(train_end, block_end, model, mean, std)
            model_c.restore_rng(torch, saved)
        fit_rows(block_end, schedule["update_epochs"], schedule["update_lr"], mean, std)
    return p


def model_c_fold(torch, device, data, seed, state, mean, std, train_end, block_end, out_p, out_ret, checked):
    """Model C walk on the fold-start backbone snapshot (run_model_c_extreme logic)."""
    model_c.reseed(torch, seed)
    backbone = learner.create_large_index_model(data["x"].shape[-1], learner.MODEL_CONFIG)
    backbone.load_state_dict(state, strict=True)
    model = model_c.build_extreme_model(torch, backbone).to(device)
    r = data["r"]
    events = np.flatnonzero(np.abs(r[:block_end]) >= EXTREME)
    scaled = learner.apply_scale(data["x"][:block_end], mean, std).astype(np.float32)
    ex = torch.from_numpy(np.ascontiguousarray(scaled[events])).to(device)
    ups = torch.tensor((r[events] > 0).astype(np.float32), device=device)
    rets = torch.tensor(r[events].astype(np.float32), device=device)
    trained = 0
    pending = [k for k, idx in enumerate(events) if idx < train_end]
    for k in pending:
        model_c.train_prefix(torch, model, ex, ups, rets, k + 1, C_FINETUNE_EPOCHS, C_FINETUNE_LR, checked)
        trained = k + 1
    for w in range(train_end, block_end):
        while trained < len(events) and events[trained] < w:
            model_c.train_prefix(torch, model, ex, ups, rets, trained + 1, C_FINETUNE_EPOCHS, C_FINETUNE_LR, checked)
            trained += 1
        row = torch.from_numpy(np.ascontiguousarray(scaled[w:w + 1])).to(device)
        prob, expected = model_c.predict_row(torch, model, row)
        out_p[w] = prob
        out_ret[w] = expected
    del model, backbone, ex
    torch.cuda.empty_cache()


def close_head_fold(torch, device, data, model, mean, std, train_end, block_end, out_ret):
    scaled = learner.apply_scale(data["x"][:block_end], mean, std).astype(np.float32)
    emb = model_b.embed_windows(torch, model, scaled, device)
    targets = torch.tensor(data["r"][:train_end], dtype=torch.float32, device=device)
    head, xm, xs, ym, ys, _ = model_b.fit_return_head(torch, emb[:train_end], targets, device, CLOSE_HEAD, announce=False)
    out_ret[train_end:block_end] = model_b.predict_returns(torch, head, emb[train_end:block_end], xm, xs, ym, ys).float().cpu().numpy()
    model.train()  # embed_windows switched to eval; the backbone schedule continues in train mode


def fit_transformers(torch, device, data, log) -> dict:
    market = data["market"]
    n = len(data["y"])
    sched = schedule_for(market)
    all_valid = np.ones(n, dtype=bool)
    out = {}
    checked = {"seen": True}
    for seed in SEEDS:
        c_p = np.full(n, np.nan)
        c_ret = np.full(n, np.nan)
        close_ret = np.full(n, np.nan)

        def on_fold(train_end, block_end, model, mean, std, seed=seed, c_p=c_p, c_ret=c_ret, close_ret=close_ret):
            state = model_c.clone_state(model)
            close_head_fold(torch, device, data, model, mean, std, train_end, block_end, close_ret)
            model_c_fold(torch, device, data, seed, state, mean, std, train_end, block_end, c_p, c_ret, checked)

        t = time.time()
        out[f"B0_s{seed}"] = run_backbone(torch, device, data, x=data["x"], valid=all_valid, schedule=sched,
                                          seed=seed, weighted=False, first=FIRST, on_fold=on_fold)
        out[f"C0_s{seed}"] = c_p
        out[f"C0ret_s{seed}"] = c_ret
        out[f"Bclose_s{seed}"] = close_ret
        log(f"{market} seed {seed} B0/C0/close {time.time() - t:.1f}s")
        t = time.time()
        out[f"K2_s{seed}"] = run_backbone(torch, device, data, x=data["x"], valid=all_valid, schedule=sched,
                                          seed=seed, weighted=True, first=FIRST)
        out[f"WB4_s{seed}"] = run_backbone(torch, device, data, x=data["x_pre"], valid=data["valid_us"],
                                           schedule=sched, seed=seed, weighted=False, first=FIRST)
        out[f"K5_s{seed}"] = run_backbone(torch, device, data, x=data["x_pre"], valid=data["valid_us"],
                                          schedule=sched, seed=seed, weighted=True, first=FIRST)
        log(f"{market} seed {seed} K2/WB4/K5 {time.time() - t:.1f}s")
    # The exact published protocol (first test 2024-01-02) checks this reimplementation
    # against logs/retrain_20261124. KOSDAQ's published schedule sets no seed, so it
    # cannot be reproduced bit for bit and is not rerun here.
    out["B0pub_s44"] = np.full(n, np.nan)
    if market == "KOSPI":
        first_2024 = next(i for i, d in enumerate(data["dates"]) if d >= "2024-01-01")
        out["B0pub_s44"] = run_backbone(torch, device, data, x=data["x"], valid=all_valid, schedule=sched,
                                        seed=44, weighted=False, first=first_2024)
    return out


# ---------------------------------------------------------------- linear models

def fit_linear(data) -> dict:
    from sklearn.linear_model import LogisticRegression, RidgeCV
    from sklearn.preprocessing import StandardScaler

    n = len(data["y"])
    last = data["x"][:, -1, :].astype(np.float64)
    last_pre = data["x_pre"][:, -1, :].astype(np.float64)
    valid = data["valid_us"]
    y = data["y"].astype(int)
    r = data["r"]
    out = {key: np.full(n, np.nan) for key in ("K3", "K4", "WB2", "WB3", "WB6", "WB7")}
    alphas = {"WB6": [], "WB7": []}
    for train_end, block_end in learner.iter_learning_blocks(n, FIRST, BLOCK):
        test = np.arange(train_end, block_end)
        for key, X, rows_ok, weighted, kind in (
            ("K3", last, np.ones(n, bool), True, "logit"),
            ("K4", last_pre, valid, True, "logit"),
            ("WB2", last, np.ones(n, bool), False, "logit"),
            ("WB3", last_pre, valid, False, "logit"),
            ("WB6", last, np.ones(n, bool), False, "ridge"),
            ("WB7", last_pre, valid, False, "ridge"),
        ):
            train = np.arange(train_end)
            train = train[rows_ok[train]]
            tst = test[rows_ok[test]]
            if len(tst) == 0:
                continue
            scaler = StandardScaler().fit(X[train])
            xt = scaler.transform(X[train])
            xs = scaler.transform(X[tst])
            if kind == "logit":
                w = ev.abs_return_weights(r, train) if weighted else None
                clf = LogisticRegression(C=0.01, solver="lbfgs", max_iter=2000, random_state=42)
                clf.fit(xt, y[train], sample_weight=w)
                out[key][tst] = clf.predict_proba(xs)[:, 1]
            else:
                reg = RidgeCV(alphas=np.logspace(-1, 4, 11)).fit(xt, r[train])
                alphas[key].append(float(reg.alpha_))
                out[key][tst] = reg.predict(xs)
    out["_ridge_alphas"] = alphas
    return out


# ---------------------------------------------------------------- scoring

def baselines(data) -> dict:
    y = data["y"]
    r = data["r"]
    last = data["x_last"]
    names = data["features"]
    ret1 = last[:, names.index("return_1d")]
    ret5 = last[:, names.index("return_5d")]
    extreme = np.abs(r) >= EXTREME
    out = {
        "coin": np.full(len(y), 0.5),
        "up_rate_all": ev.expanding_event_up_rate(y, np.ones(len(y), bool)),
        "prev_extreme_dir": ev.previous_event_direction(y, extreme),
        "momentum_1d": (ret1 > 0).astype(float),
        "reversal_1d": (ret1 <= 0).astype(float),
        "momentum_5d": (ret5 > 0).astype(float),
        "reversal_5d": (ret5 <= 0).astype(float),
    }
    sox = last[:, len(names) + US6.index("us_sox_return_1d")]
    out["us_sox_sign"] = np.where(data["valid_us"], (sox > 0).astype(float), np.nan)
    for thr in (0.015, 0.02, 0.03):
        out[f"event_up_rate_{thr}"] = ev.expanding_event_up_rate(y, np.abs(r) >= thr)
    return out


def ensemble(preds: dict, key: str) -> np.ndarray:
    return np.mean(np.stack([preds[f"{key}_s{s}"] for s in SEEDS]), axis=0)


def compare(cand, comps: dict, y, mask, block=10, bins=5, extra_blocks=(5, 20)) -> dict:
    ok = mask & np.isfinite(cand)
    for value in comps.values():
        ok &= np.isfinite(value)
    res = {"n": int(ok.sum()), "candidate": ev.summarize(cand[ok], y[ok], bins), "vs": {}}
    if ok.sum() == 0:
        return res
    lc = ev.brier(cand[ok], y[ok])
    for name, comp in comps.items():
        d = lc - ev.brier(comp[ok], y[ok])
        res["vs"][name] = {"brier": ev.block_bootstrap(d, block=block), "comparator": ev.summarize(comp[ok], y[ok], bins)}
        res["vs"][name]["sensitivity_high"] = {b: ev.block_bootstrap(d, block=b)["high"] for b in extra_blocks}
        dl = ev.log_loss(cand[ok], y[ok]) - ev.log_loss(comp[ok], y[ok])
        res["vs"][name]["log_loss_diff"] = float(dl.mean())
    return res


def compare_mae(pred_ret, comps: dict, r, close, mask, block=10) -> dict:
    ok = mask & np.isfinite(pred_ret)
    for value in comps.values():
        ok &= np.isfinite(value)
    err = np.abs(close * (pred_ret - r))
    res = {"n": int(ok.sum()), "mae_points": float(err[ok].mean()) if ok.any() else None,
           "mae_return": float(np.abs(pred_ret - r)[ok].mean()) if ok.any() else None, "vs": {}}
    for name, comp in comps.items():
        e2 = np.abs(close * (comp - r))
        res["vs"][name] = {"mae_points": float(e2[ok].mean()), "diff": ev.block_bootstrap(err[ok] - e2[ok], block=block)}
    return res


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("fit", "score"), required=True)
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--us-csv", required=True)
    parser.add_argument("--us-receipt", required=True, help="UTC receipt time of the US CSV (reporting cutoff)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--markets", nargs="+", default=["KOSPI", "KOSDAQ"])
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_lines = []

    def log(message):
        line = f"{datetime.now(UTC).strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        log_lines.append(line)

    conn = sqlite3.connect(f"file:{Path(args.sqlite).resolve()}?mode=ro", uri=True)
    if args.stage == "fit":
        import torch

        device, gpu = model_c.require_cuda(torch)
        manifest = {"started_at": datetime.now(UTC).isoformat(), "gpu": gpu, "torch": torch.__version__,
                    "sqlite_sha256": sha256(Path(args.sqlite)), "us_csv_sha256": sha256(Path(args.us_csv)),
                    "code_sha256": {p: sha256(ROOT / p) for p in (
                        "scripts/run_bc_chronological_eval.py", "src/roboquant/models/bc_chrono_eval.py",
                        "scripts/run_incremental_index_learner.py", "scripts/run_model_c_extreme.py",
                        "scripts/run_model_b_close_forecast.py", "src/roboquant/models/large_index_transformer.py",
                        "src/roboquant/index_forecast/us_market_inputs.py")},
                    "seeds": list(SEEDS), "first_window": FIRST, "block": BLOCK, "production_weight": 0}
        for market in args.markets:
            data = prepare(conn, market, Path(args.us_csv), args.us_receipt)
            log(f"{market} windows {len(data['dates'])} {data['dates'][0]}..{data['dates'][-1]} "
                f"premarket-valid {int(data['valid_us'].sum())} features {len(data['features'])}+{len(US6)}")
            preds = fit_transformers(torch, device, data, log)
            linear = fit_linear(data)
            manifest[f"{market}_ridge_alphas"] = linear.pop("_ridge_alphas")
            manifest[f"{market}_features"] = data["features"] + US6
            manifest[f"{market}_us_report"] = {k: data["us_report"].get(k) for k in ("status", "eligible_rows", "reporting_cutoff", "limitations")}
            manifest[f"{market}_premarket_invalid_windows"] = [d for d, ok in zip(data["dates"], data["valid_us"]) if not ok]
            arrays = {**preds, **linear}
            np.savez_compressed(out_dir / f"{market.lower()}_predictions.npz", dates=np.array(data["dates"]),
                                y=data["y"], r=data["r"], close=data["close"], valid_us=data["valid_us"],
                                x_last=data["x_pre"][:, -1, :], features=np.array(data["features"]), **arrays)
            log(f"{market} saved {len(arrays)} prediction arrays")
            manifest["log"] = log_lines
            (out_dir / "fit_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n")
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        manifest["log"] = log_lines
        (out_dir / "fit_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n")
        return 0

    results = {"generated_at": datetime.now(UTC).isoformat(), "markets": {}}
    for market in args.markets:
        z = np.load(out_dir / f"{market.lower()}_predictions.npz")
        preds = {key: z[key] for key in z.files}
        data = {"y": z["y"], "r": z["r"], "close": z["close"], "dates": [str(d) for d in z["dates"]],
                "x_last": z["x_last"], "valid_us": z["valid_us"], "features": [str(f) for f in z["features"]]}
        base = baselines(data)
        y, r, close = data["y"], data["r"], data["close"]
        dates = np.array(data["dates"])
        scored = np.zeros(len(y), bool)
        scored[FIRST:] = True
        sel = scored & (dates >= SELECTION_START) & (dates < SELECTION_END)
        y26 = scored & (dates >= SELECTION_END)
        cand = {
            "C0": preds["C0_s44"], "K1": ensemble(preds, "C0"), "K2": ensemble(preds, "K2"), "K3": preds["K3"],
            "K4": preds["K4"], "K5": ensemble(preds, "K5"),
            "B0": preds["B0_s44"], "WB1": ensemble(preds, "B0"), "WB2": preds["WB2"], "WB3": preds["WB3"],
            "WB4": ensemble(preds, "WB4"),
        }
        market_res = {"counts": {}, "C": {}, "B": {}, "B_close": {}, "seed_spread": {}}
        # ---- Model C
        for slice_name, base_mask, thr in (("selection_1.5", sel, 0.015), ("selection_2", sel, 0.02),
                                           ("selection_3", sel, 0.03), ("y2026_3", y26, 0.03),
                                           ("y2026_1.5", y26, 0.015)):
            mask = base_mask & (np.abs(r) >= thr)
            market_res["counts"][slice_name] = int(mask.sum())
            comps = {"coin": base["coin"], "event_up_rate": base[f"event_up_rate_{thr}"], "C0": cand["C0"]}
            block_res = {}
            for key in ("K1", "K2", "K3", "K4", "K5", "B0", "C0"):
                block_res[key] = compare(cand[key], comps, y, mask, bins=5)
            for key in ("prev_extreme_dir", "momentum_1d", "reversal_1d", "momentum_5d", "reversal_5d",
                        "us_sox_sign", "coin", f"event_up_rate_{thr}"):
                ok = mask & np.isfinite(base[key])
                block_res["baseline:" + key] = ev.summarize(base[key][ok], y[ok], 5)
            market_res["C"][slice_name] = block_res
        # |r|-weighted all-session Brier on the selection slice
        wmask = sel
        wres = {}
        for key in ("K1", "K2", "K3", "K4", "K5", "B0", "C0"):
            ok = wmask & np.isfinite(cand[key])
            w = np.abs(r[ok])
            wres[key] = {"n": int(ok.sum()), "abs_weighted_brier": float(np.sum(w * ev.brier(cand[key][ok], y[ok])) / w.sum())}
        ok = wmask
        w = np.abs(r[ok])
        wres["coin"] = {"n": int(ok.sum()), "abs_weighted_brier": float(np.sum(w * 0.25) / w.sum())}
        market_res["C"]["selection_abs_weighted"] = wres
        # Model C close MAE on extreme days (secondary)
        cmae = {}
        for slice_name, base_mask in (("selection_3", sel), ("y2026_3", y26)):
            mask = base_mask & (np.abs(r) >= EXTREME)
            ext = np.abs(r) >= EXTREME
            rets = {"C0": preds["C0ret_s44"], "K1": np.mean(np.stack([preds[f"C0ret_s{s}"] for s in SEEDS]), axis=0)}
            for key in ("K2", "K3", "K4", "K5"):
                rets[key] = ev.conditional_expected_return(cand[key], r, ext)
            cmae[slice_name] = {key: compare_mae(val, {"previous_close": np.zeros(len(r))}, r, close, mask) for key, val in rets.items()}
        market_res["C"]["close_mae_secondary"] = cmae
        # ---- Model B direction
        for slice_name, mask in (("selection", sel), ("y2026", y26)):
            comps = {"coin": base["coin"], "up_rate": base["up_rate_all"], "B0": cand["B0"]}
            block_res = {}
            for key in ("WB1", "WB2", "WB3", "WB4", "B0"):
                block_res[key] = compare(cand[key], comps, y, mask, bins=10)
            for key in ("momentum_1d", "reversal_1d", "momentum_5d", "reversal_5d", "us_sox_sign", "up_rate_all", "coin"):
                ok = mask & np.isfinite(base[key])
                block_res["baseline:" + key] = ev.summarize(base[key][ok], y[ok], 10)
            if np.isfinite(preds["B0pub_s44"]).any():
                ok = mask & np.isfinite(preds["B0pub_s44"])
                block_res["B0_published_protocol_2024on"] = ev.summarize(preds["B0pub_s44"][ok], y[ok], 10)
                ok2 = ok & np.isfinite(cand["B0"])
                block_res["B0_reconstruction_same_dates"] = ev.summarize(cand["B0"][ok2], y[ok2], 10)
            market_res["B"][slice_name] = block_res
        # ---- Model B close
        close_cand = {"Bclose": preds["Bclose_s44"],
                      "WB5": np.mean(np.stack([preds[f"Bclose_s{s}"] for s in SEEDS]), axis=0),
                      "WB6": preds["WB6"], "WB7": preds["WB7"]}
        for slice_name, mask in (("selection", sel), ("y2026", y26)):
            comps = {"previous_close": np.zeros(len(r)), "Bclose": close_cand["Bclose"]}
            market_res["B_close"][slice_name] = {
                key: compare_mae(close_cand[key], comps, r, close, mask) for key in ("WB5", "WB6", "WB7", "Bclose")
            }
        # ---- seed spread
        for key in ("C0", "B0", "K2", "K5", "WB4"):
            per = {}
            for slice_name, mask in (("selection_1.5", sel & (np.abs(r) >= 0.015)), ("y2026_3", y26 & (np.abs(r) >= 0.03)),
                                     ("selection_all", sel), ("y2026_all", y26)):
                vals = []
                for s in SEEDS:
                    pr = preds[f"{key}_s{s}"]
                    ok = mask & np.isfinite(pr)
                    vals.append(float(ev.brier(pr[ok], y[ok]).mean()))
                per[slice_name] = {"per_seed_brier": dict(zip(map(str, SEEDS), vals)), "mean_single": float(np.mean(vals))}
            market_res["seed_spread"][key] = per
        results["markets"][market] = market_res
    (out_dir / "scores.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({m: v["counts"] for m, v in results["markets"].items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
