#!/usr/bin/env python3
"""Score a pre-2026 finetune of the existing 8-layer patch transformer.

The architecture is create_large_index_model with the incremental learner
config: patch 8, stride 4, d_model 384, 8 heads, 8 layers, dropout 0.2.
The on-disk incremental checkpoints match that model and 24 features, but
their last session is 2026-11-20 and the incremental trainer updates on each
block after scoring it. Those weights have seen 2026 labels, so they are not
loaded. The scored network is trained from scratch on target dates before
2026-01-01, then finetuned at a lower learning rate on that same pre-2026
split. 2026 labels are scored once and do not choose the model.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/processed/index_forecast.sqlite"
LABEL_BEFORE = "2026-01-01"
TEST_FROM = "2026-01-02"
VAL_WINDOWS = 60
EXPECTED_CONFIG = {
    "patch_len": 8,
    "stride": 4,
    "d_model": 384,
    "n_heads": 8,
    "num_layers": 8,
    "dropout": 0.2,
}
SERIES_TOKEN_BRIER = {"KOSPI": 0.2426, "KOSDAQ": 0.2500}


def load_learner():
    path = ROOT / "scripts" / "run_incremental_index_learner.py"
    spec = importlib.util.spec_from_file_location("incremental_index_learner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def metrics(actual: np.ndarray, probability: np.ndarray) -> dict:
    clipped = np.clip(probability, 1e-6, 1 - 1e-6)
    return {
        "n": int(len(actual)),
        "brier": round(float(np.mean((clipped - actual) ** 2)), 4),
        "direction_hit": round(float(np.mean((clipped >= 0.5) == (actual >= 0.5))), 4),
        "mean_p_up": round(float(np.mean(clipped)), 4),
        "up_rate": round(float(np.mean(actual)), 4),
    }


def target_dates(conn, market: str, asof_dates: list[str]) -> list[str]:
    sessions = pd.read_sql_query(
        "SELECT session_date FROM index_eod WHERE index_code = ? ORDER BY session_date",
        conn,
        params=[market],
    )
    days = pd.to_datetime(sessions["session_date"]).dt.strftime("%Y-%m-%d").tolist()
    nxt = {days[index]: days[index + 1] for index in range(len(days) - 1)}
    missing = [day for day in asof_dates if day not in nxt]
    if missing:
        raise ValueError(f"{market} window has no next session: {missing[:3]}")
    return [nxt[day] for day in asof_dates]


def split_indices(targets: list[str]):
    target = np.asarray(targets)
    pre = np.flatnonzero(target < LABEL_BEFORE)
    test = np.flatnonzero(target >= TEST_FROM)
    if len(pre) <= VAL_WINDOWS + 200 or len(test) == 0:
        raise ValueError(f"bad split pre={len(pre)} test={len(test)}")
    train, val = pre[:-VAL_WINDOWS], pre[-VAL_WINDOWS:]
    if max(target[train].tolist()) >= LABEL_BEFORE or max(target[val].tolist()) >= LABEL_BEFORE:
        raise ValueError("a pre-2026 split contains a 2026 label")
    if min(target[test].tolist()) < TEST_FROM:
        raise ValueError("test starts before 2026-01-02")
    return train, val, test


def inspect_checkpoint(torch, learner, market: str, features: list[str]) -> dict:
    path = ROOT / "models" / "dnn" / f"incremental_{market.lower()}.pt"
    checkpoint = torch.load(path, map_location="cpu", weights_only=False)
    config = dict(checkpoint.get("config") or {})
    saved_features = list(checkpoint.get("features") or [])
    config_match = config == EXPECTED_CONFIG == dict(learner.MODEL_CONFIG)
    feature_match = saved_features == list(features)
    probe = learner.create_large_index_model(len(saved_features), config if config else EXPECTED_CONFIG)
    error = None
    try:
        probe.load_state_dict(checkpoint["state_dict"])
        compatible = True
    except Exception as exc:
        compatible = False
        error = f"{type(exc).__name__}: {exc}"
    del probe
    loaded = False
    if not config_match or not feature_match or not compatible:
        reason = "Weights were not loaded. " + (error or "Feature list or config does not match the 8-layer checkpoint.")
    else:
        reason = (
            f"Weights were not loaded. last_session is {checkpoint.get('last_session')}, and the "
            "incremental trainer updates on each block after scoring it, so this state dict has "
            "seen 2026 labels. The file is shape-compatible with the 8-layer, 24-feature model. "
            "The scored model uses that same architecture and feature list, trained from scratch "
            "on target dates before 2026-01-01."
        )
    return {
        "path": str(path.relative_to(ROOT)),
        "loaded": loaded,
        "state_dict_compatible": compatible,
        "config_match": config_match,
        "feature_match": feature_match,
        "config": config,
        "feature_count": len(saved_features),
        "features": saved_features,
        "last_session": checkpoint.get("last_session"),
        "production_weight": checkpoint.get("production_weight"),
        "reason": reason,
    }


def clone_state(model):
    return {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}


def predict_logits(torch, model, x, device):
    model.eval()
    pieces = []
    with torch.inference_mode():
        data = torch.from_numpy(np.ascontiguousarray(x, dtype=np.float32))
        for start in range(0, len(data), 128):
            pieces.append(model(data[start:start + 128].to(device)).float().cpu())
    return torch.cat(pieces).numpy()


def probability_from_logits(logits):
    return 1.0 / (1.0 + np.exp(-np.clip(logits, -30, 30)))


def validation_brier(torch, model, x, y, device) -> float:
    return float(np.mean((probability_from_logits(predict_logits(torch, model, x, device)) - y) ** 2))


def train_stage(torch, model, train_x, train_y, val_x, val_y, device, lr, epochs, patience, tag, keep_initial):
    loss_fn = torch.nn.BCEWithLogitsLoss()
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    features = torch.from_numpy(np.ascontiguousarray(train_x, dtype=np.float32)).to(device)
    labels = torch.from_numpy(np.ascontiguousarray(train_y, dtype=np.float32)).to(device)
    best_score = validation_brier(torch, model, val_x, val_y, device) if keep_initial else float("inf")
    best_state = clone_state(model) if keep_initial else None
    stale = 0
    ran = 0
    print(f"  {tag} start valid_brier={best_score if keep_initial else float('nan'):.5f}", flush=True)
    for epoch in range(epochs):
        model.train()
        order = torch.randperm(len(features), device=device)
        total = 0.0
        for start in range(0, len(features), 64):
            batch = order[start:start + 64]
            optimizer.zero_grad(set_to_none=True)
            loss = loss_fn(model(features[batch]), labels[batch])
            if not torch.isfinite(loss):
                raise FloatingPointError(f"{tag} produced a non-finite loss")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            total += float(loss.item()) * len(batch)
        score = validation_brier(torch, model, val_x, val_y, device)
        ran = epoch + 1
        print(f"  {tag} epoch {ran} train_loss={total / len(features):.4f} valid_brier={score:.5f}", flush=True)
        if score < best_score - 1e-4:
            best_score = score
            best_state = clone_state(model)
            stale = 0
        else:
            stale += 1
            if stale >= patience:
                break
    if best_state is None:
        raise RuntimeError(f"{tag} produced no checkpoint")
    model.load_state_dict(best_state)
    return round(float(best_score), 5), ran


def fit_lightgbm(x, y):
    import lightgbm as lgb

    model = lgb.LGBMClassifier(
        n_estimators=200,
        learning_rate=0.05,
        num_leaves=15,
        min_child_samples=20,
        subsample=0.9,
        colsample_bytree=0.9,
        random_state=42,
        verbosity=-1,
        n_jobs=1,
    )
    model.fit(x, y.astype(int))
    return model


def evaluate_market(torch, learner, conn, market: str, device) -> dict:
    frame, _latest, features = learner.market_frame(conn, market)
    windows, labels, asof_dates, features = learner.make_windows(frame, features)
    if list(features) != list(learner.market_frame(conn, market)[2]):
        raise RuntimeError("feature list changed while the windows were built")
    checkpoint = inspect_checkpoint(torch, learner, market, features)
    if len(features) != 24 or checkpoint["config"] != EXPECTED_CONFIG:
        raise RuntimeError(f"{market} is not the 24-feature 8-layer checkpoint")
    targets = target_dates(conn, market, asof_dates)
    train_idx, val_idx, test_idx = split_indices(targets)
    mean, std = learner.scale_fit(windows[train_idx])
    train_x = learner.apply_scale(windows[train_idx], mean, std)
    val_x = learner.apply_scale(windows[val_idx], mean, std)
    test_x = learner.apply_scale(windows[test_idx], mean, std)
    train_y = labels[train_idx]
    val_y = labels[val_idx]
    test_y = labels[test_idx]
    torch.manual_seed(42)
    np.random.seed(42)
    current = learner.create_large_index_model(len(features), EXPECTED_CONFIG).to(device)
    layer_ids = {key.split(".")[2] for key in current.state_dict() if key.startswith("encoder.layers.")}
    if layer_ids != {str(index) for index in range(8)}:
        raise RuntimeError(f"model does not have 8 encoder layers: {sorted(layer_ids)}")
    current_val, current_epochs = train_stage(
        torch, current, train_x, train_y, val_x, val_y, device,
        lr=2e-4, epochs=12, patience=4, tag=f"{market} 8-layer", keep_initial=False,
    )
    finetuned = learner.create_large_index_model(len(features), EXPECTED_CONFIG).to(device)
    finetuned.load_state_dict(clone_state(current))
    finetune_val, finetune_epochs = train_stage(
        torch, finetuned, train_x, train_y, val_x, val_y, device,
        lr=5e-5, epochs=8, patience=3, tag=f"{market} finetune", keep_initial=True,
    )
    current_p = probability_from_logits(predict_logits(torch, current, test_x, device))
    finetuned_p = probability_from_logits(predict_logits(torch, finetuned, test_x, device))
    tree = fit_lightgbm(windows[train_idx, -1, :], train_y)
    tree_p = tree.predict_proba(windows[test_idx, -1, :])[:, 1].astype(np.float32)
    ensemble_p = 0.5 * finetuned_p + 0.5 * tree_p
    training_up_rate = float(np.mean(np.concatenate([train_y, val_y])))
    constant_p = np.full(len(test_y), training_up_rate, dtype=np.float32)
    scored = {
        "constant_up_rate": metrics(test_y, constant_p),
        "patch_transformer_8layer": metrics(test_y, current_p),
        "finetuned_transformer": metrics(test_y, finetuned_p),
        "lightgbm": metrics(test_y, tree_p),
        "finetuned_lightgbm_ensemble": metrics(test_y, ensemble_p),
    }
    ranking = {name: row["brier"] for name, row in scored.items()}
    print(market, json.dumps(scored, ensure_ascii=False), flush=True)
    return {
        "market": market,
        "checkpoint": checkpoint,
        "architecture": "create_large_index_model",
        "config": EXPECTED_CONFIG,
        "features": features,
        "feature_count": len(features),
        "lookback": learner.LOOKBACK,
        "train_target_end": targets[train_idx[-1]],
        "validation_target_start": targets[val_idx[0]],
        "validation_target_end": targets[val_idx[-1]],
        "test_asof_start": asof_dates[test_idx[0]],
        "test_asof_end": asof_dates[test_idx[-1]],
        "test_target_start": targets[test_idx[0]],
        "test_target_end": targets[test_idx[-1]],
        "training_up_rate": round(training_up_rate, 4),
        "current_epochs_ran": current_epochs,
        "current_validation_brier": current_val,
        "finetune_epochs_ran": finetune_epochs,
        "finetune_validation_brier": finetune_val,
        "parameters": sum(parameter.numel() for parameter in finetuned.parameters()),
        "models": scored,
        "lowest_brier_model": min(ranking, key=ranking.get),
        "series_token_brier": SERIES_TOKEN_BRIER[market],
        "production_weight": 0.0,
    }


def main() -> int:
    import torch

    if not torch.cuda.is_available():
        raise SystemExit("CUDA GPU is not available")
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.benchmark = False
    device = torch.device("cuda")
    learner = load_learner()
    if dict(learner.MODEL_CONFIG) != EXPECTED_CONFIG:
        raise SystemExit("incremental MODEL_CONFIG is not the expected 8-layer patch transformer")
    print(torch.cuda.get_device_name(0), flush=True)
    conn = sqlite3.connect(DB)
    markets = [evaluate_market(torch, learner, conn, market, device) for market in ("KOSPI", "KOSDAQ")]
    deltas = {}
    for row in markets:
        best = min(row["models"]["finetuned_transformer"]["brier"], row["models"]["finetuned_lightgbm_ensemble"]["brier"])
        deltas[row["market"]] = round(best - SERIES_TOKEN_BRIER[row["market"]], 4)
    improved = [market for market, delta in deltas.items() if delta < 0]
    worsened = [market for market, delta in deltas.items() if delta > 0.005]
    if improved and not worsened:
        verdict = "finetune_beat_series_token_brier"
    else:
        verdict = "finetune_did_not_beat_series_token_without_making_the_other_market_worse"
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "protocol": "target_before_2026_train_score_targets_from_2026-01-02",
        "architecture": "create_large_index_model",
        "config": EXPECTED_CONFIG,
        "checkpoint_loaded": {row["market"]: row["checkpoint"]["loaded"] for row in markets},
        "production_weight": 0.0,
        "gpu": torch.cuda.get_device_name(0),
        "ensemble": "0.5 finetuned 8-layer transformer + 0.5 LightGBM, weights fixed before the 2026 test",
        "reference_series_token_brier": SERIES_TOKEN_BRIER,
        "finetune_brier_minus_series_token": deltas,
        "verdict": verdict,
        "markets": markets,
    }
    output = ROOT / "models/dnn/finetune_accuracy.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"verdict": verdict, "deltas": deltas, "loaded": payload["checkpoint_loaded"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
