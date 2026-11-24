#!/usr/bin/env python3
"""Score a series-token transformer on the 2026 sessions it has not trained on.

Each input series is patched into time tokens and all of those tokens enter
one encoder. This is a research measurement. It does not replace model A or
the stored manual model B forecast.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
LOOKBACK = 96
CONFIG = {"patch_len": 16, "stride": 16, "d_model": 512, "n_heads": 8, "num_layers": 12, "dropout": 0.1}
TEST_START = "2026-01-01"


def _learner():
    path = ROOT / "scripts" / "run_incremental_index_learner.py"
    spec = importlib.util.spec_from_file_location("incremental_index_learner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _metrics(actual: np.ndarray, probability: np.ndarray) -> dict:
    clipped = np.clip(probability, 1e-6, 1 - 1e-6)
    return {
        "n": int(len(actual)),
        "brier": round(float(np.mean((clipped - actual) ** 2)), 4),
        "direction_hit": round(float(np.mean((clipped >= 0.5) == (actual >= 0.5))), 4),
        "mean_p_up": round(float(np.mean(clipped)), 4),
        "up_rate": round(float(np.mean(actual)), 4),
    }


def score_market(torch, learner, conn, market: str, device) -> dict:
    from roboquant.models.large_index_transformer import create_series_token_model

    frame, _latest, features = learner.market_frame(conn, market)
    windows, labels, dates, features = learner.make_windows(frame, features)
    test_at = next(index for index, day in enumerate(dates) if day >= TEST_START)
    if test_at < 200 or test_at >= len(dates):
        raise ValueError(f"{market} does not have a 2026 holdout with earlier training history")
    mean, std = learner.scale_fit(windows[:test_at])
    train_x = learner.apply_scale(windows[:test_at], mean, std)
    test_x = learner.apply_scale(windows[test_at:], mean, std)
    model = create_series_token_model(len(features), LOOKBACK, CONFIG).to(device)
    learner.train_epochs(torch, model, train_x, labels[:test_at], device, 8, 2e-4)
    probability = learner.predict(torch, model, test_x, device)
    actual = labels[test_at:]
    constant = np.full(len(actual), float(labels[:test_at].mean()), dtype=np.float32)
    return {
        "market": market,
        "features": features,
        "feature_count": len(features),
        "tokens_per_series": (LOOKBACK - CONFIG["patch_len"]) // CONFIG["stride"] + 1,
        "train_end": dates[test_at - 1],
        "test_start": dates[test_at],
        "test_end": dates[-1],
        "parameters": sum(parameter.numel() for parameter in model.parameters()),
        "model": _metrics(actual, probability),
        "constant_up_rate": _metrics(actual, constant),
        "production_weight": 0.0,
    }


def main() -> int:
    import torch
    if not torch.cuda.is_available():
        raise SystemExit("CUDA GPU is not available")
    learner = _learner()
    device = torch.device("cuda")
    conn = sqlite3.connect(ROOT / "data/processed/index_forecast.sqlite")
    markets = [score_market(torch, learner, conn, market, device) for market in ("KOSPI", "KOSDAQ")]
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "architecture": "series_token_transformer",
        "config": CONFIG,
        "lookback": LOOKBACK,
        "protocol": "train_before_2026_score_2026",
        "production_weight": 0.0,
        "gpu": torch.cuda.get_device_name(0),
        "markets": markets,
    }
    output = ROOT / "models/dnn/series_token_accuracy.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
