#!/usr/bin/env python3
"""Train the large index transformer on the current CUDA GPU.

The result is a research shadow. It does not change the published linear forecast.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from roboquant.models.large_index_transformer import create_large_index_model

FEATURES = [
    "return_1d",
    "return_5d",
    "return_20d",
    "volatility_5d",
    "volatility_20d",
    "ma_gap_5d",
    "ma_gap_20d",
]


def price_frame(conn, market: str) -> pd.DataFrame:
    prices = pd.read_sql_query(
        "SELECT session_date, close FROM index_eod WHERE index_code = ? ORDER BY session_date",
        conn,
        params=[market],
    )
    prices["session_date"] = pd.to_datetime(prices["session_date"]).dt.date
    close = pd.to_numeric(prices["close"], errors="coerce")
    log_ret = np.log(close / close.shift(1))
    prices["return_1d"] = close.pct_change()
    prices["return_5d"] = close.pct_change(5)
    prices["return_20d"] = close.pct_change(20)
    prices["volatility_5d"] = log_ret.rolling(5, min_periods=5).std()
    prices["volatility_20d"] = log_ret.rolling(20, min_periods=20).std()
    prices["ma_gap_5d"] = close / close.rolling(5, min_periods=5).mean() - 1.0
    prices["ma_gap_20d"] = close / close.rolling(20, min_periods=20).mean() - 1.0
    prices["label_return"] = close.shift(-1) / close - 1.0
    return prices.dropna(subset=[*FEATURES, "label_return"]).reset_index(drop=True)


def windows(frame: pd.DataFrame, lookback: int):
    values = frame[FEATURES].to_numpy(dtype=np.float32)
    labels = (frame["label_return"].to_numpy(dtype=np.float32) > 0).astype(np.float32)
    dates = frame["session_date"].astype(str).tolist()
    rows, ys, meta = [], [], []
    for end in range(lookback - 1, len(frame)):
        rows.append(values[end - lookback + 1 : end + 1])
        ys.append(labels[end])
        meta.append(dates[end])
    return np.stack(rows), np.asarray(ys), meta


def split(x, y, dates, valid_days: int, test_days: int):
    test_start = len(x) - test_days
    valid_start = test_start - valid_days
    if valid_start < 200:
        raise ValueError("not enough history for the large model split")
    return {
        "train": (x[:valid_start], y[:valid_start], dates[:valid_start]),
        "valid": (x[valid_start:test_start], y[valid_start:test_start], dates[valid_start:test_start]),
        "test": (x[test_start:], y[test_start:], dates[test_start:]),
    }


def scale(train_x, other_x):
    flat = train_x.reshape(-1, train_x.shape[-1])
    mean = flat.mean(axis=0)
    std = flat.std(axis=0)
    std[std < 1e-6] = 1.0
    return (train_x - mean) / std, (other_x - mean) / std


def scores(probability, actual):
    probability = np.clip(probability, 1e-6, 1 - 1e-6)
    binary = actual > 0.5
    return {
        "n": int(len(actual)),
        "direction_hit": float(np.mean((probability >= 0.5) == binary)),
        "brier": float(np.mean((probability - actual) ** 2)),
        "up_rate": float(np.mean(actual)),
    }


def run_market(torch, conn, market: str, config: dict, device) -> dict:
    model_cfg = config["model"]
    frame = price_frame(conn, market)
    x, y, dates = windows(frame, int(model_cfg["lookback"]))
    parts = split(x, y, dates, int(config["split"]["valid_days"]), int(config["split"]["test_days"]))
    x_train, x_valid = scale(parts["train"][0], parts["valid"][0])
    _, x_test = scale(parts["train"][0], parts["test"][0])
    y_train, y_valid, y_test = parts["train"][1], parts["valid"][1], parts["test"][1]

    def loader(features, labels, shuffle):
        dataset = torch.utils.data.TensorDataset(
            torch.from_numpy(features.astype(np.float32)),
            torch.from_numpy(labels.astype(np.float32)),
        )
        return torch.utils.data.DataLoader(
            dataset, batch_size=int(model_cfg["batch_size"]), shuffle=shuffle
        )

    model = create_large_index_model(len(FEATURES), model_cfg).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=float(model_cfg["learning_rate"]),
        weight_decay=float(model_cfg["weight_decay"]),
    )
    criterion = torch.nn.BCEWithLogitsLoss()
    use_amp = device.type == "cuda"
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)
    best_state, best_loss, patience = None, float("inf"), 0
    for _epoch in range(int(model_cfg["max_epochs"])):
        model.train()
        for batch_x, batch_y in loader(x_train, y_train, True):
            batch_x, batch_y = batch_x.to(device), batch_y.to(device)
            optimizer.zero_grad(set_to_none=True)
            with torch.amp.autocast(device.type, enabled=use_amp):
                loss = criterion(model(batch_x), batch_y)
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(optimizer)
            scaler.update()
        probability = predict(torch, model, loader(x_valid, y_valid, False), device, use_amp)
        valid_loss = float(np.mean((probability - y_valid) ** 2))
        if valid_loss < best_loss:
            best_loss = valid_loss
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
            patience = 0
        else:
            patience += 1
            if patience >= int(model_cfg["early_stopping_patience"]):
                break
    model.load_state_dict(best_state)
    test_probability = predict(torch, model, loader(x_test, y_test, False), device, use_amp)
    up_rate = float(y_train.mean())
    return {
        "train_end": parts["train"][2][-1],
        "test_start": parts["test"][2][0],
        "test_end": parts["test"][2][-1],
        "model": scores(test_probability, y_test),
        "constant_up_rate": scores(np.full_like(y_test, up_rate), y_test),
    }


def predict(torch, model, loader, device, use_amp):
    model.eval()
    output = []
    with torch.no_grad():
        for batch_x, _batch_y in loader:
            with torch.amp.autocast(device.type, enabled=use_amp):
                logit = model(batch_x.to(device))
            output.append(torch.sigmoid(logit).float().cpu().numpy())
    return np.concatenate(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/train_large_index_transformer.yaml")
    parser.add_argument("--sqlite", default="data/processed/index_forecast.sqlite")
    parser.add_argument("--output", default="models/dnn/large_index_transformer_status.json")
    args = parser.parse_args()
    import torch

    if not torch.cuda.is_available():
        raise SystemExit("CUDA GPU is not available")
    device = torch.device("cuda")
    config = yaml.safe_load(Path(args.config).read_text())
    conn = sqlite3.connect(args.sqlite)
    started = torch.cuda.max_memory_allocated()
    markets = {market: run_market(torch, conn, market, config, device) for market in ("KOSPI", "KOSDAQ")}
    parameter_count = sum(parameter.numel() for parameter in create_large_index_model(len(FEATURES), config["model"]).parameters())
    payload = {
        "model_name": config["model"]["model_name"],
        "production_weight": 0.0,
        "device": torch.cuda.get_device_name(0),
        "parameter_count": int(parameter_count),
        "peak_gpu_memory_mib": round(torch.cuda.max_memory_allocated() / (1024 ** 2), 1),
        "memory_before_mib": round(started / (1024 ** 2), 1),
        "markets": markets,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
