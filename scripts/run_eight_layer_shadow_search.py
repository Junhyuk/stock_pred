#!/usr/bin/env python3
"""Shadow search for the 8-layer transformer.

Settings are chosen only on walk-forward blocks that end before 2026.
The 2026 blocks are reported as an already examined reference and do not
change the chosen settings. Published model B weights are not replaced.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/processed/index_forecast.sqlite"
SELECTION_CUTOFF = "2026-01-01"
OUTPUT = ROOT / "models/dnn/eight_layer_shadow_search.json"
ARCHITECTURE = {"patch_len": 8, "stride": 4, "d_model": 384, "n_heads": 8, "num_layers": 8, "dropout": 0.2}


def _learner():
    path = ROOT / "scripts" / "run_incremental_index_learner.py"
    spec = importlib.util.spec_from_file_location("incremental_index_learner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selection_blocks(blocks: list[dict], cutoff: str = SELECTION_CUTOFF) -> list[dict]:
    return [block for block in blocks if block["end"] < cutoff]


def pooled(blocks: list[dict]) -> dict:
    if not blocks:
        return {"n": 0, "brier": None, "direction_hit": None, "log_loss": None, "probability_mae": None, "mean_p_up": None}
    total = sum(block["n"] for block in blocks)
    def average(key):
        return round(sum(block[key] * block["n"] for block in blocks) / total, 4)
    return {
        "n": total,
        "blocks": len(blocks),
        "brier": average("brier"),
        "direction_hit": average("direction_hit"),
        "log_loss": average("log_loss"),
        "probability_mae": average("probability_mae"),
        "mean_p_up": average("mean_p_up"),
    }


def history_start(dates: list[str], end: int, years: int | None) -> int:
    if not years:
        return 0
    cutoff = (pd.Timestamp(dates[end - 1]) - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
    start = 0
    while start < end and dates[start] < cutoff:
        start += 1
    return start


def score_predictions(actual, probability) -> dict:
    actual = np.asarray(actual, dtype=np.float64)
    probability = np.clip(np.asarray(probability, dtype=np.float64), 1e-6, 1 - 1e-6)
    n = len(actual)
    brier = float(np.mean((probability - actual) ** 2))
    hit = float(np.mean((probability >= 0.5) == (actual >= 0.5)))
    log_loss = float(-np.mean(actual * np.log(probability) + (1 - actual) * np.log(1 - probability)))
    mae = float(np.mean(np.abs(probability - actual)))
    return {
        "n": int(n),
        "brier": round(brier, 4),
        "direction_hit": round(hit, 4),
        "log_loss": round(log_loss, 4),
        "probability_mae": round(mae, 4),
        "mean_p_up": round(float(np.mean(probability)), 4),
    }


def walk_forward(torch, learner, model, x, y, dates, first_test, device, *, update_epochs, update_lr, block_size, history_years):
    from roboquant.models.incremental_timeseries import iter_learning_blocks
    blocks = []
    oof_y, oof_p, oof_dates = [], [], []
    for train_end, block_end in iter_learning_blocks(len(x), first_test, block_size):
        start = history_start(dates, train_end, history_years)
        mean, std = learner.scale_fit(x[start:train_end])
        probability = learner.predict(torch, model, learner.apply_scale(x[train_end:block_end], mean, std), device)
        scored = score_predictions(y[train_end:block_end], probability)
        scored.update(start=dates[train_end], end=dates[block_end - 1], train_end=dates[train_end - 1])
        blocks.append(scored)
        oof_y.append(y[train_end:block_end])
        oof_p.append(probability)
        oof_dates.extend(dates[train_end:block_end])
        learner.train_epochs(
            torch, model, learner.apply_scale(x[start:block_end], mean, std), y[start:block_end],
            device, update_epochs, update_lr)
    return blocks, np.concatenate(oof_y), np.concatenate(oof_p), oof_dates


def fit_initial(torch, learner, x, y, dates, first_test, device, config, seed, history_years):
    from roboquant.models.large_index_transformer import create_large_index_model
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    model = create_large_index_model(x.shape[-1], config).to(device)
    start = history_start(dates, first_test, history_years)
    mean, std = learner.scale_fit(x[start:first_test])
    learner.train_epochs(torch, model, learner.apply_scale(x[start:first_test], mean, std), y[start:first_test], device, 6, 2e-4)
    return model


def prepare(learner, conn, market: str):
    frame, _latest, features = learner.market_frame(conn, market)
    x, y, dates, features = learner.make_windows(frame, features)
    finite = np.isfinite(y)
    if not finite.all():
        keep = finite
        x, y, dates = x[keep], y[keep], [day for day, ok in zip(dates, keep) if ok]
    first_test = next(index for index, day in enumerate(dates) if day >= "2024-01-01")
    return x, y, dates, features, first_test


def summarize(blocks):
    chosen = selection_blocks(blocks)
    later = [block for block in blocks if block["end"] >= SELECTION_CUTOFF]
    return {"selection_pre_2026": pooled(chosen), "already_examined_2026": pooled(later), "blocks": blocks}


def best_by_selection(records):
    ranked = [record for record in records if record["summary"]["selection_pre_2026"]["n"]]
    return min(ranked, key=lambda record: record["summary"]["selection_pre_2026"]["brier"])


def save(payload):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    import torch
    if not torch.cuda.is_available():
        raise SystemExit("CUDA GPU is not available")
    learner = _learner()
    device = torch.device("cuda")
    conn = sqlite3.connect(DB)
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "architecture": ARCHITECTURE,
        "selection_rule": "walk-forward blocks ending before 2026-01-01",
        "year_2026": "already examined in earlier comparisons; not used to choose settings",
        "published_model_b_changed": False,
        "production_weight": 0.0,
        "gpu": torch.cuda.get_device_name(0),
        "candidates": [],
    }
    if OUTPUT.exists():
        saved = json.loads(OUTPUT.read_text())
        if any(item.get("name") == "block-21" for item in saved.get("candidates", [])) and "kosdaq" not in saved:
            payload = saved
            chosen = best_by_selection(payload["candidates"])
            return finish(payload, chosen, torch, learner, conn, device)
    x, y, dates, features, first_test = prepare(learner, conn, "KOSPI")
    runs = [("reference", 2e-4, 2)] + [(f"ft-{lr:g}-{epochs}ep", lr, epochs) for lr in (1e-5, 2e-5, 5e-5) for epochs in (1, 2, 3)]
    base = fit_initial(torch, learner, x, y, dates, first_test, device, ARCHITECTURE, 42, None)
    initial_state = copy.deepcopy(base.state_dict())

    def fresh():
        from roboquant.models.large_index_transformer import create_large_index_model
        model = create_large_index_model(x.shape[-1], ARCHITECTURE).to(device)
        model.load_state_dict(initial_state)
        return model

    def record_run(name, model, lr, epochs, dropout, history_years, seed, block_size, arrays):
        blocks, oof_y, oof_p, oof_dates = walk_forward(
            torch, learner, model, arrays[0], arrays[1], arrays[2], first_test, device,
            update_epochs=epochs, update_lr=lr, block_size=block_size, history_years=history_years)
        record = {"name": name, "market": "KOSPI", "lr": lr, "epochs": epochs, "dropout": dropout,
                  "history_years": history_years, "seed": seed, "block_size": block_size,
                  "summary": summarize(blocks), "oof": {"y": oof_y.tolist(), "p": np.asarray(oof_p).tolist(), "dates": oof_dates}}
        payload["candidates"].append(record)
        save(payload)
        print(name, record["summary"]["selection_pre_2026"], flush=True)
        return record

    arrays = (x, y, dates)
    record_run("reference", base, 2e-4, 2, 0.2, None, 42, 60, arrays)
    for name, lr, epochs in runs[1:]:
        record_run(name, fresh(), lr, epochs, 0.2, None, 42, 60, arrays)
    chosen = best_by_selection([item for item in payload["candidates"] if item["block_size"] == 60 and item["dropout"] == 0.2 and item["seed"] == 42 and item["history_years"] is None])
    for dropout in (0.1, 0.3):
        config = {**ARCHITECTURE, "dropout": dropout}
        model = fit_initial(torch, learner, x, y, dates, first_test, device, config, 42, None)
        record_run(f"dropout-{dropout}", model, chosen["lr"], chosen["epochs"], dropout, None, 42, 60, arrays)
    chosen = best_by_selection(payload["candidates"])
    for years in (3, 5):
        config = {**ARCHITECTURE, "dropout": chosen["dropout"]}
        model = fit_initial(torch, learner, x, y, dates, first_test, device, config, 42, years)
        record_run(f"history-{years}y", model, chosen["lr"], chosen["epochs"], chosen["dropout"], years, 42, 60, arrays)
    chosen = best_by_selection(payload["candidates"])
    for seed in (43, 44):
        config = {**ARCHITECTURE, "dropout": chosen["dropout"]}
        model = fit_initial(torch, learner, x, y, dates, first_test, device, config, seed, chosen["history_years"])
        record_run(f"seed-{seed}", model, chosen["lr"], chosen["epochs"], chosen["dropout"], chosen["history_years"], seed, 60, arrays)
    chosen = best_by_selection(payload["candidates"])
    config = {**ARCHITECTURE, "dropout": chosen["dropout"]}
    model = fit_initial(torch, learner, x, y, dates, first_test, device, config, chosen["seed"], chosen["history_years"])
    record_run("block-21", model, chosen["lr"], chosen["epochs"], chosen["dropout"], chosen["history_years"], chosen["seed"], 21, arrays)
    chosen = best_by_selection(payload["candidates"])
    return finish(payload, chosen, torch, learner, conn, device)


def finish(payload, chosen, torch, learner, conn, device) -> int:
    payload["selected_pre_2026"] = {key: chosen[key] for key in ("name", "lr", "epochs", "dropout", "history_years", "seed", "block_size")}
    payload["selected_pre_2026"]["metrics"] = chosen["summary"]["selection_pre_2026"]
    payload["selected_pre_2026"]["already_examined_2026"] = chosen["summary"]["already_examined_2026"]
    reference = next(record for record in payload["candidates"] if record["name"] == "reference")
    gain = reference["summary"]["selection_pre_2026"]["brier"] - chosen["summary"]["selection_pre_2026"]["brier"]
    payload["moe"] = "skipped_no_pre2026_gain" if gain < 0.005 else "skipped_until_a_later_shadow_confirms_the_gain"
    payload["kosdaq150"] = "unavailable_no_substitute"
    pre = [index for index, day in enumerate(chosen["oof"]["dates"]) if day < SELECTION_CUTOFF]
    y_pre = np.asarray(chosen["oof"]["y"])[pre]
    p_pre = np.asarray(chosen["oof"]["p"])[pre]
    payload["calibration"] = "sigmoid_on_pre_2026_oof_only"
    payload["calibration_pre_2026"] = _calibrated(y_pre, p_pre)
    payload["kosdaq"] = _kosdaq_shadow(torch, learner, conn, device, chosen)
    payload["kosdaq_decision"] = payload["kosdaq"]["decision"]
    save(payload)
    print(json.dumps(payload["selected_pre_2026"], ensure_ascii=False), flush=True)
    return 0


def _aligned_return(dates: pd.Series, frame: pd.DataFrame, lag_days: int) -> np.ndarray:
    right = frame.sort_values("date").copy()
    right["_asof"] = (right["date"] + pd.Timedelta(days=lag_days)).astype("datetime64[ns]")
    left = pd.DataFrame({"_asof": pd.to_datetime(dates).astype("datetime64[ns]")})
    merged = pd.merge_asof(left, right[["_asof", "value"]].sort_values("_asof"), on="_asof", direction="backward")
    return merged["value"].to_numpy()


def kosdaq_feature_frame(conn, learner):
    labeled, _latest, columns = learner.market_frame(conn, "KOSDAQ")
    kospi = pd.read_sql_query("SELECT session_date, close FROM index_eod WHERE index_code = 'KOSPI' ORDER BY session_date", conn)
    kospi["session_date"] = pd.to_datetime(kospi["session_date"])
    kospi["kospi_ret"] = pd.to_numeric(kospi["close"], errors="coerce").pct_change()
    labeled = labeled.merge(kospi[["session_date", "kospi_ret"]], on="session_date", how="left")
    labeled["rel_kospi"] = labeled["return_1d"] - labeled["kospi_ret"]
    flow = pd.read_sql_query(
        "SELECT session_date, investor_type, net_value FROM investor_flow WHERE market = 'KOSDAQ'", conn)
    flow["session_date"] = pd.to_datetime(flow["session_date"])
    wide = flow.pivot_table(index="session_date", columns="investor_type", values="net_value", aggfunc="last")
    for investor, column in (("외국인", "flow_foreign"), ("기관", "flow_institution")):
        values = wide[investor] / 1e8 if investor in wide.columns else pd.Series(index=wide.index, dtype=float)
        labeled = labeled.merge(values.rename(column), left_on="session_date", right_index=True, how="left")
        labeled[column] = labeled[column].shift(1)
    for code, column in (("US_NASDAQ", "nasdaq_ret"), ("US_SOX", "sox_ret")):
        macro = pd.read_sql_query("SELECT observation_date, value FROM macro_series WHERE series_code = ?", conn, params=[code])
        macro["date"] = pd.to_datetime(macro["observation_date"])
        macro["value"] = pd.to_numeric(macro["value"], errors="coerce")
        labeled[column] = _aligned_return(labeled["session_date"], macro.dropna(), 1)
    usd = pd.read_sql_query("SELECT observation_date, value FROM macro_series WHERE series_code = 'YAHOO_USDKRW'", conn)
    usd["date"] = pd.to_datetime(usd["observation_date"])
    usd["value"] = pd.to_numeric(usd["value"], errors="coerce").pct_change()
    labeled["usdkrw_ret"] = _aligned_return(labeled["session_date"], usd.dropna(), 0)
    extra = ["rel_kospi", "flow_foreign", "flow_institution", "nasdaq_ret", "sox_ret", "usdkrw_ret"]
    labeled = labeled.dropna(subset=extra).reset_index(drop=True)
    return labeled, columns + extra


def _constant_and_tree(learner, x, y, dates, first_test, block_size, history_years):
    from roboquant.models.incremental_timeseries import iter_learning_blocks
    import lightgbm as lgb
    constant_blocks, tree_blocks = [], []
    cy, cp, ty, tp = [], [], [], []
    for train_end, block_end in iter_learning_blocks(len(x), first_test, block_size):
        start = history_start(dates, train_end, history_years)
        mean, std = learner.scale_fit(x[start:train_end])
        train_last = learner.apply_scale(x[start:train_end], mean, std)[:, -1, :]
        test_last = learner.apply_scale(x[train_end:block_end], mean, std)[:, -1, :]
        rate = float(y[start:train_end].mean())
        constant = np.full(block_end - train_end, rate)
        tree = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=15, min_child_samples=20, random_state=42, verbosity=-1)
        tree.fit(train_last, y[start:train_end].astype(int))
        tree_p = tree.predict_proba(test_last)[:, 1]
        for store, probability in ((constant_blocks, constant), (tree_blocks, tree_p)):
            scored = score_predictions(y[train_end:block_end], probability)
            scored.update(start=dates[train_end], end=dates[block_end - 1])
            store.append(scored)
        cy.append(y[train_end:block_end]); cp.append(constant); ty.append(y[train_end:block_end]); tp.append(tree_p)
    return constant_blocks, tree_blocks


def _kosdaq_shadow(torch, learner, conn, device, chosen):
    frame, features = kosdaq_feature_frame(conn, learner)
    x, y, dates, _features = learner.make_windows(frame, features)
    finite = np.isfinite(y)
    x, y, dates = x[finite], y[finite], [day for day, ok in zip(dates, finite) if ok]
    selectable = [index for index, day in enumerate(dates) if day < SELECTION_CUTOFF]
    if len(selectable) < 80:
        return {"decision": "constant_until_a_pre2026_walkforward_beats_the_training_up_rate", "reason": "not_enough_pre2026_rows"}
    first_test = max(40, len(selectable) // 2)
    config = {**ARCHITECTURE, "dropout": chosen["dropout"]}
    model = fit_initial(torch, learner, x, y, dates, first_test, device, config, chosen["seed"], chosen["history_years"])
    blocks, *_rest = walk_forward(
        torch, learner, model, x, y, dates, first_test, device,
        update_epochs=chosen["epochs"], update_lr=chosen["lr"], block_size=chosen["block_size"], history_years=chosen["history_years"])
    constant_blocks, tree_blocks = _constant_and_tree(learner, x, y, dates, first_test, chosen["block_size"], chosen["history_years"])
    compared = {
        "constant": pooled(selection_blocks(constant_blocks)),
        "lightgbm": pooled(selection_blocks(tree_blocks)),
        "eight_layer_with_kosdaq_features": pooled(selection_blocks(blocks)),
    }
    winner = min(compared, key=lambda name: compared[name]["brier"] if compared[name]["brier"] is not None else 9)
    decision = "constant" if winner == "constant" or compared[winner]["brier"] >= compared["constant"]["brier"] else winner
    return {"decision": decision, "pre_2026": compared, "features_added": ["nasdaq_ret", "sox_ret", "usdkrw_ret", "flow_foreign", "flow_institution", "rel_kospi"], "kosdaq150": "unavailable_no_substitute"}


def _calibrated(actual, probability) -> dict:
    from sklearn.linear_model import LogisticRegression
    logit = np.log(probability / (1 - probability)).reshape(-1, 1)
    model = LogisticRegression(C=1.0, max_iter=200)
    model.fit(logit, actual.astype(int))
    calibrated = model.predict_proba(logit)[:, 1]
    return {"pre_2026_before": score_predictions(actual, probability), "pre_2026_after": score_predictions(actual, calibrated)}


if __name__ == "__main__":
    raise SystemExit(main())
