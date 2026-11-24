#!/usr/bin/env python3
"""Fit a Model B next-close head on the frozen direction checkpoints.

The direction weights are read and then left untouched. The new head maps the
frozen transformer embedding to the next close return and is trained on CUDA.
Hyperparameters are chosen by walk-forward close error on sessions before
2026. The 2026 sessions are scored after that choice, with each block fit only
on earlier labels. The live forecast may then use every label that already has
a next close.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.models.incremental_timeseries import iter_learning_blocks
from roboquant.models.large_index_transformer import create_large_index_model
from roboquant.models.model_b_close import expected_index, period_close_mae

PROTECTED = (
    "incremental_kospi.pt",
    "incremental_kosdaq.pt",
    "incremental_kospi_prior_2e-4.pt",
)
PRE_2026 = "2026-01-01"
BLOCK_SIZE = 60
EPOCHS = 200
LEARNING_RATE = 1e-3
SEED = 44
CANDIDATES = tuple(
    {"kind": kind, "weight_decay": weight_decay, "learning_rate": LEARNING_RATE, "epochs": EPOCHS, "seed": SEED}
    for kind in ("linear", "mlp")
    for weight_decay in (0.01, 0.1, 1.0, 10.0)
)


def direction_path(market: str) -> Path:
    return ROOT / "models" / "dnn" / f"incremental_{market.lower()}.pt"


def close_head_path(market: str) -> Path:
    path = ROOT / "models" / "dnn" / f"model_b_close_{market.lower()}.pt"
    if path.name in PROTECTED:
        raise SystemExit(f"refusing to write a direction checkpoint: {path}")
    return path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def protected_hashes() -> dict[str, str]:
    return {name: file_sha256(ROOT / "models" / "dnn" / name) for name in PROTECTED}


def load_learner():
    path = ROOT / "scripts" / "run_incremental_index_learner.py"
    spec = importlib.util.spec_from_file_location("incremental_index_learner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_cuda(torch):
    if not torch.cuda.is_available():
        raise SystemExit("CUDA GPU is required for the close head")
    device = torch.device("cuda")
    if device.type != "cuda":
        raise SystemExit("refusing to train the close head on CPU")
    return device


def assert_visible_on_3090() -> None:
    pid = str(os.getpid())
    for _attempt in range(6):
        listing = subprocess.check_output(["nvidia-smi"], text=True)
        if pid in listing and "NVIDIA GeForce RTX 3090" in listing:
            print(f"nvidia-smi shows pid {pid} on NVIDIA GeForce RTX 3090", flush=True)
            return
        time.sleep(1)
    raise SystemExit("close-head process is not visible on the NVIDIA GeForce RTX 3090")


def build_head(torch, d_model: int, kind: str):
    nn = torch.nn

    class CloseReturnHead(nn.Module):
        def __init__(self):
            super().__init__()
            if kind == "linear":
                self.body = nn.Linear(d_model, 1)
            elif kind == "mlp":
                hidden = d_model // 2
                self.body = nn.Sequential(nn.Linear(d_model, hidden), nn.GELU(), nn.Linear(hidden, 1))
            else:
                raise ValueError(f"unknown close head {kind}")

        def forward(self, features):
            return self.body(features).squeeze(-1)

    return CloseReturnHead()


def embed_windows(torch, model, windows: np.ndarray, device):
    holder: dict[str, object] = {}

    def hook(_module, _inputs, output):
        holder["hidden"] = output

    handle = model.norm.register_forward_hook(hook)
    chunks = []
    model.eval()
    try:
        with torch.no_grad():
            for start in range(0, len(windows), 128):
                batch = torch.from_numpy(np.ascontiguousarray(windows[start:start + 128])).to(device)
                logits = model(batch)
                hidden = holder["hidden"]
                manual = model.head(hidden).squeeze(-1)
                if not torch.allclose(logits, manual, atol=1e-4, rtol=1e-4):
                    raise RuntimeError("close head is not reading the frozen direction embedding")
                chunks.append(hidden.detach().clone())
    finally:
        handle.remove()
    embeddings = torch.cat(chunks, dim=0)
    if embeddings.device.type != "cuda" or not torch.isfinite(embeddings).all():
        raise RuntimeError("embeddings must be finite CUDA tensors")
    return embeddings


def fit_return_head(torch, features, targets, device, config: dict, *, announce: bool):
    if device.type != "cuda" or features.device.type != "cuda" or targets.device.type != "cuda":
        raise RuntimeError("close head training must stay on cuda")
    torch.manual_seed(config["seed"])
    torch.cuda.manual_seed_all(config["seed"])
    x_mean = features.mean(dim=0)
    x_std = features.std(dim=0, unbiased=False).clamp_min(1e-6)
    y_mean = targets.mean()
    y_std = targets.std(unbiased=False).clamp_min(1e-6)
    scaled_x = (features - x_mean) / x_std
    scaled_y = (targets - y_mean) / y_std
    head = build_head(torch, features.shape[1], config["kind"]).to(device)
    if next(head.parameters()).device.type != "cuda":
        raise RuntimeError("close head left cuda")
    optimizer = torch.optim.AdamW(head.parameters(), lr=config["learning_rate"], weight_decay=config["weight_decay"])
    loss_fn = torch.nn.MSELoss()
    head.train()
    loss = None
    for _epoch in range(config["epochs"]):
        optimizer.zero_grad(set_to_none=True)
        loss = loss_fn(head(scaled_x), scaled_y)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(head.parameters(), 1.0)
        optimizer.step()
        if announce and _epoch == 0:
            torch.cuda.synchronize()
            assert_visible_on_3090()
    if loss is None or not torch.isfinite(loss.detach()):
        raise RuntimeError("close head loss was not finite")
    return head, x_mean, x_std, y_mean, y_std, float(loss.detach())


def predict_returns(torch, head, features, x_mean, x_std, y_mean, y_std):
    head.eval()
    with torch.no_grad():
        scaled = (features - x_mean) / x_std
        return head(scaled) * y_std + y_mean


def walk_forward(torch, embeddings, targets, device, config: dict, min_train: int, dates: list[str], *, announce: bool):
    predictions = torch.full((embeddings.shape[0],), float("nan"), device=device)
    first = True
    for start, end in iter_learning_blocks(embeddings.shape[0], min_train, BLOCK_SIZE):
        print(
            f"fit {config['kind']} wd {config['weight_decay']} on sessions before {dates[start]}",
            flush=True,
        )
        head, x_mean, x_std, y_mean, y_std, _loss = fit_return_head(
            torch, embeddings[:start], targets[:start], device, config, announce=announce and first
        )
        first = False
        predictions[start:end] = predict_returns(torch, head, embeddings[start:end], x_mean, x_std, y_mean, y_std)
        del head
    if not torch.isfinite(predictions[min_train:]).all():
        raise RuntimeError("walk-forward close head produced a non-finite return")
    return predictions.detach().float().cpu().numpy()


def price_lookup(conn, market: str):
    prices = pd.read_sql_query(
        "SELECT session_date, close FROM index_eod WHERE index_code = ? ORDER BY session_date",
        conn,
        params=[market],
    )
    prices["session_date"] = pd.to_datetime(prices["session_date"])
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    prices = prices.dropna(subset=["session_date", "close"])
    returns = prices["close"].shift(-1) / prices["close"] - 1.0
    keys = prices["session_date"].dt.strftime("%Y-%m-%d")
    return dict(zip(keys, prices["close"].to_numpy(dtype=np.float64))), dict(zip(keys, returns.to_numpy(dtype=np.float64)))


def selection_key(row: dict):
    return (row["pre_2026"]["close_mae"], row["kind"] != "linear", -row["weight_decay"])


def load_direction(torch, market: str, device):
    path = direction_path(market)
    checkpoint = torch.load(path, map_location="cpu", weights_only=False)
    model = create_large_index_model(len(checkpoint["features"]), checkpoint["config"]).to(device)
    model.load_state_dict(checkpoint["state_dict"])
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    if any(parameter.requires_grad for parameter in model.parameters()):
        raise RuntimeError("direction model must stay frozen")
    frozen = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
    return path, checkpoint, model, frozen


def run_market(torch, learner, conn, market: str, device, existing: dict) -> dict:
    path, checkpoint, model, frozen = load_direction(torch, market, device)
    if market == "KOSPI" and (checkpoint.get("variant") != "8층 미세조정" or existing.get("variant") != "8층 미세조정"):
        raise SystemExit(f"refusing to rename KOSPI variant {checkpoint.get('variant')!r}")
    if checkpoint.get("last_session") != existing["trained_through"]:
        raise SystemExit(f"{market} trained_through does not match the direction checkpoint")
    labeled, latest, columns = learner.market_frame(conn, market)
    if list(columns) != list(checkpoint["features"]):
        raise SystemExit(f"{market} feature timing does not match the direction checkpoint")
    windows, _direction_labels, dates, features = learner.make_windows(labeled, list(checkpoint["features"]))
    if dates != sorted(dates):
        raise SystemExit(f"{market} windows are not in session order")
    closes_by_day, returns_by_day = price_lookup(conn, market)
    actual = np.array([returns_by_day[day] for day in dates], dtype=np.float64)
    closes = np.array([closes_by_day[day] for day in dates], dtype=np.float64)
    if not np.isfinite(actual).all():
        raise SystemExit(f"{market} labeled window is missing a next close")
    asof = latest["session_date"].iloc[-1].strftime("%Y-%m-%d")
    if asof in dates or np.isfinite(returns_by_day[asof]):
        raise SystemExit(f"{market} latest session {asof} already has a next close")
    min_train = 504 if len(dates) >= 700 else 252
    if len(dates) <= min_train:
        raise SystemExit(f"{market} does not have enough labeled sessions")
    mean = np.asarray(checkpoint["mean"], dtype=np.float32)
    std = np.asarray(checkpoint["std"], dtype=np.float32)
    scaled = learner.apply_scale(windows, mean, std).astype(np.float32)
    embeddings = embed_windows(torch, model, scaled, device)
    targets = torch.tensor(actual, dtype=torch.float32, device=device)
    print(f"{market} windows {len(dates)} {dates[0]}..{dates[-1]} min_train {min_train}", flush=True)
    scored = []
    for config in CANDIDATES:
        started = time.perf_counter()
        predicted = walk_forward(
            torch, embeddings, targets, device, config, min_train, dates, announce=not scored
        )
        pre_2026 = period_close_mae(closes, predicted, actual, dates, end=PRE_2026)
        row = {
            "kind": config["kind"],
            "weight_decay": config["weight_decay"],
            "pre_2026": pre_2026,
            "predicted": predicted,
        }
        scored.append(row)
        print(
            f"{market} {config['kind']} wd {config['weight_decay']} "
            f"pre-2026 MAE {pre_2026['close_mae']:.4f} n {pre_2026['n']} "
            f"{time.perf_counter() - started:.1f}s",
            flush=True,
        )
    chosen = min(scored, key=selection_key)
    year_2026 = period_close_mae(closes, chosen["predicted"], actual, dates, start=PRE_2026)
    pre_2026 = chosen["pre_2026"]
    naive = np.where(np.isfinite(chosen["predicted"]), 0.0, np.nan)
    naive_pre = period_close_mae(closes, naive, actual, dates, end=PRE_2026)
    naive_2026 = period_close_mae(closes, naive, actual, dates, start=PRE_2026)
    config = next(
        item for item in CANDIDATES if item["kind"] == chosen["kind"] and item["weight_decay"] == chosen["weight_decay"]
    )
    head, x_mean, x_std, y_mean, y_std, _loss = fit_return_head(
        torch, embeddings, targets, device, config, announce=False
    )
    forecast_values = latest[features].to_numpy(dtype=np.float32)[-learner.LOOKBACK:]
    forecast_window = learner.apply_scale(forecast_values[None, ...], mean, std).astype(np.float32)
    forecast_embedding = embed_windows(torch, model, forecast_window, device)
    predicted_return = float(predict_returns(torch, head, forecast_embedding, x_mean, x_std, y_mean, y_std)[0].detach())
    latest_close = round(float(latest["close"].iloc[-1]), 2)
    quoted_return = predicted_return
    quoted_index = expected_index(latest_close, quoted_return)
    if quoted_index != latest_close * (1.0 + quoted_return):
        raise SystemExit("expected index broke the close-return identity")
    with torch.no_grad():
        fresh_probability = float(torch.sigmoid(model(torch.from_numpy(forecast_window).to(device)))[0])
    stored_probability = float(existing["p_up"])
    if abs(fresh_probability - stored_probability) <= 5e-5:
        p_up = stored_probability
        p_down = float(existing["p_down"])
    else:
        p_up = round(fresh_probability, 4)
        p_down = round(1.0 - fresh_probability, 4)
    for key, value in model.state_dict().items():
        if not torch.equal(frozen[key], value.detach().cpu()):
            raise SystemExit("frozen direction weights changed in memory")
    del model
    checkpoint_payload = {
        "state_dict": {key: value.detach().cpu() for key, value in head.state_dict().items()},
        "kind": config["kind"],
        "d_model": int(embeddings.shape[1]),
        "embedding_mean": x_mean.detach().cpu(),
        "embedding_std": x_std.detach().cpu(),
        "return_mean": y_mean.detach().cpu(),
        "return_std": y_std.detach().cpu(),
        "learning_rate": config["learning_rate"],
        "weight_decay": config["weight_decay"],
        "epochs": config["epochs"],
        "seed": config["seed"],
        "block_size": BLOCK_SIZE,
        "min_train": min_train,
        "market": market,
        "direction_checkpoint": path.name,
        "direction_sha256": file_sha256(path),
        "selection": "walk_forward_close_mae_before_2026",
        "candidates": [
            {
                "kind": row["kind"],
                "weight_decay": row["weight_decay"],
                "pre_2026_close_mae": row["pre_2026"]["close_mae"],
                "pre_2026_n": row["pre_2026"]["n"],
            }
            for row in scored
        ],
        "pre_2026_close_mae": pre_2026["close_mae"],
        "pre_2026_n": pre_2026["n"],
        "pre_2026_start": pre_2026["start"],
        "pre_2026_end": pre_2026["end"],
        "pre_2026_naive_close_mae": naive_pre["close_mae"],
        "year_2026_close_mae": year_2026["close_mae"],
        "year_2026_n": year_2026["n"],
        "year_2026_start": year_2026["start"],
        "year_2026_end": year_2026["end"],
        "year_2026_naive_close_mae": naive_2026["close_mae"],
        "production_weight": 0.0,
        "device": "cuda",
    }
    return {
        "market": market,
        "asof_date": asof,
        "target_date": learner.next_session(asof),
        "close": latest_close,
        "p_up": p_up,
        "p_down": p_down,
        "fresh_p_up": fresh_probability,
        "expected_return": quoted_return,
        "expected_index": quoted_index,
        "trained_through": existing["trained_through"],
        "variant": existing.get("variant"),
        "pre_2026": pre_2026,
        "year_2026": year_2026,
        "naive_pre_2026": naive_pre["close_mae"],
        "naive_2026": naive_2026["close_mae"],
        "selected": {"kind": config["kind"], "weight_decay": config["weight_decay"]},
        "checkpoint": checkpoint_payload,
    }


def write_forecast(path: Path, existing: dict, rows: list[dict], gpu_name: str) -> None:
    by_market = {row["market"]: row for row in rows}
    existing["schema_version"] = "model_b_forecast_v1"
    existing["model_name"] = "모델 B"
    existing["production_weight"] = 0.0
    existing["generated_at"] = datetime.now(UTC).isoformat()
    existing["gpu"] = gpu_name
    for row in existing["markets"]:
        update = by_market[row["market"]]
        row["asof_date"] = update["asof_date"]
        row["target_date"] = update["target_date"]
        row["close"] = update["close"]
        row["p_up"] = update["p_up"]
        row["p_down"] = update["p_down"]
        row["trained_through"] = update["trained_through"]
        if update.get("variant"):
            row["variant"] = update["variant"]
        row["expected_return"] = update["expected_return"]
        row["expected_index"] = update["expected_index"]
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default=str(ROOT / "data/processed/index_forecast.sqlite"))
    parser.add_argument("--forecast-json", default=str(ROOT / "docs/data/model_b_forecast.json"))
    args = parser.parse_args()
    import torch

    device = require_cuda(torch)
    torch.backends.cudnn.benchmark = False
    before = protected_hashes()
    print(f"cuda {torch.cuda.get_device_name(0)} pid {os.getpid()}", flush=True)
    learner = load_learner()
    forecast_path = Path(args.forecast_json)
    existing = json.loads(forecast_path.read_text())
    existing_rows = {row["market"]: row for row in existing["markets"]}
    conn = sqlite3.connect(args.sqlite)
    results = []
    try:
        for market in ("KOSPI", "KOSDAQ"):
            results.append(run_market(torch, learner, conn, market, device, existing_rows[market]))
    finally:
        conn.close()
    after = protected_hashes()
    if before != after:
        raise SystemExit("direction checkpoints changed on disk")
    for row in results:
        destination = close_head_path(row["market"])
        torch.save(row["checkpoint"], destination)
    write_forecast(forecast_path, existing, results, torch.cuda.get_device_name(0))
    if protected_hashes() != before:
        raise SystemExit("direction checkpoints changed while writing the forecast")
    summary = []
    for row in results:
        summary.append({
            "market": row["market"],
            "expected_index": row["expected_index"],
            "expected_return": row["expected_return"],
            "close": row["close"],
            "p_up": row["p_up"],
            "fresh_p_up": row["fresh_p_up"],
            "selected": row["selected"],
            "pre_2026_close_mae": row["pre_2026"]["close_mae"],
            "pre_2026_n": row["pre_2026"]["n"],
            "pre_2026_start": row["pre_2026"]["start"],
            "pre_2026_end": row["pre_2026"]["end"],
            "pre_2026_naive_close_mae": row["naive_pre_2026"],
            "year_2026_close_mae": row["year_2026"]["close_mae"],
            "year_2026_n": row["year_2026"]["n"],
            "year_2026_start": row["year_2026"]["start"],
            "year_2026_end": row["year_2026"]["end"],
            "year_2026_naive_close_mae": row["naive_2026"],
            "variant": row.get("variant"),
        })
    print(json.dumps({"device": "cuda", "direction_checkpoints_unchanged": True, "markets": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
