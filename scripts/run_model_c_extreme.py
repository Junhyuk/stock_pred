#!/usr/bin/env python3
"""Fine-tune a copy of the 8-layer model on sharp KOSPI and KOSDAQ sessions.

A sharp session is one whose next close-to-close return has absolute value of
at least 3%. Model C trains and scores only those sessions. It does not replace
Model A or Model B, and its production weight stays 0.

The direction encoder is create_large_index_model. This script wraps that
encoder with a return head and does not modify large_index_transformer.py.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_LEARNER_PATH = ROOT / "scripts" / "run_incremental_index_learner.py"
_SPEC = importlib.util.spec_from_file_location("incremental_index_learner", _LEARNER_PATH)
learner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(learner)

EXTREME_THRESHOLD = 0.03
SETTING_CUTOFF = "2026-01-01"
LEARNING_RATES = (1e-5, 5e-5)
# Fixed before any 2026 score is consulted. Not chosen on 2026 rows.
UPDATE_EPOCHS = 3
ARCHITECTURE = "8층 미세조정 복사"
VARIANT = "8층 미세조정"
EXPECTED_CONFIG = {
    "patch_len": 8,
    "stride": 4,
    "d_model": 384,
    "n_heads": 8,
    "num_layers": 8,
    "dropout": 0.2,
}
SOURCES = {
    "KOSPI": ROOT / "models" / "dnn" / "incremental_kospi.pt",
    "KOSDAQ": ROOT / "models" / "dnn" / "incremental_kosdaq.pt",
}
OUTPUTS = {
    "KOSPI": ROOT / "models" / "dnn" / "model_c_kospi.pt",
    "KOSDAQ": ROOT / "models" / "dnn" / "model_c_kosdaq.pt",
}
ACCURACY_PATH = ROOT / "models" / "dnn" / "model_c_accuracy.json"
FORECAST_PATH = ROOT / "docs" / "data" / "model_c_forecast.json"
PUBLIC_METRICS = ("n", "direction_hit", "brier", "close_mae")
SEEDS = {"KOSPI": 44, "KOSDAQ": 44}


def is_extreme_return(value, threshold: float = EXTREME_THRESHOLD) -> bool:
    """True only when a realized next-session return is at least the threshold."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if not np.isfinite(number):
        return False
    return abs(number) >= float(threshold)


def rows_for_setting_selection(rows, cutoff: str = SETTING_CUTOFF) -> list:
    """Keep the pre-2026 pool. A 2026 session date never enters this list."""
    selected = []
    for row in rows:
        day = str(row["session_date"])
        if day >= cutoff:
            continue
        selected.append(row)
    if any(str(row["session_date"]) >= cutoff for row in selected):
        raise RuntimeError("setting selector included a 2026 row")
    return selected


def _predicted_close(row) -> float:
    if "predicted_close" in row and row["predicted_close"] is not None:
        return float(row["predicted_close"])
    return float(row["close"]) * (1.0 + float(row["expected_return"]))


def summarize_scores(rows) -> dict:
    if not rows:
        return {"n": 0, "direction_hit": None, "brier": None, "close_mae": None,
                "_rank_brier": None, "_rank_hit": None, "_rank_mae": None}
    hits, briers, maes = [], [], []
    for row in rows:
        actual_up = 1.0 if float(row["actual_return"]) > 0.0 else 0.0
        probability = float(row["p_up"])
        hits.append(float((probability >= 0.5) == (actual_up >= 0.5)))
        briers.append((probability - actual_up) ** 2)
        maes.append(abs(_predicted_close(row) - float(row["actual_close"])))
    rank_hit = float(np.mean(hits))
    rank_brier = float(np.mean(briers))
    rank_mae = float(np.mean(maes))
    return {
        "n": int(len(rows)),
        "direction_hit": round(rank_hit, 4),
        "brier": round(rank_brier, 4),
        "close_mae": round(rank_mae, 2),
        "_rank_brier": rank_brier,
        "_rank_hit": rank_hit,
        "_rank_mae": rank_mae,
    }


def public_metrics(summary: dict) -> dict:
    return {key: summary[key] for key in PUBLIC_METRICS}


def choose_learning_rate(trials, cutoff: str = SETTING_CUTOFF):
    """Pick a learning rate using only session dates before cutoff.

    Lower Brier wins. Ties prefer a higher direction hit, then a lower close
    MAE, then the smaller learning rate. Rows on or after the cutoff are ignored.
    """
    ranked = []
    for trial in trials:
        summary = summarize_scores(rows_for_setting_selection(trial["rows"], cutoff))
        if summary["n"] == 0:
            continue
        ranked.append((
            summary["_rank_brier"],
            -summary["_rank_hit"],
            summary["_rank_mae"],
            float(trial["learning_rate"]),
        ))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][3]


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_session_targets(conn, market: str) -> dict[str, dict]:
    frame = pd.read_sql_query(
        "SELECT session_date, close FROM index_eod WHERE index_code = ? ORDER BY session_date",
        conn,
        params=[market],
    )
    frame["session_date"] = pd.to_datetime(frame["session_date"])
    close = pd.to_numeric(frame["close"], errors="coerce")
    nxt = close.shift(-1)
    forward = nxt / close - 1.0
    targets = {}
    for day, px, next_px, value in zip(frame["session_date"], close, nxt, forward):
        if pd.isna(px) or pd.isna(next_px) or pd.isna(value):
            continue
        targets[day.strftime("%Y-%m-%d")] = {
            "close": float(px),
            "actual_close": float(next_px),
            "actual_return": float(value),
        }
    return targets


def extreme_samples(dates, targets: dict[str, dict]) -> list[dict]:
    rows = []
    for index, day in enumerate(dates):
        target = targets.get(day)
        if target is None or not is_extreme_return(target["actual_return"]):
            continue
        actual = float(target["actual_return"])
        rows.append({
            "index": int(index),
            "session_date": day,
            "close": float(target["close"]),
            "actual_close": float(target["actual_close"]),
            "actual_return": actual,
            "up": 1.0 if actual > 0.0 else 0.0,
        })
    days = [row["session_date"] for row in rows]
    if days != sorted(days):
        raise RuntimeError("extreme sessions are not in time order")
    return rows


def require_compatible(checkpoint: dict, features: list[str], market: str) -> None:
    config = dict(checkpoint.get("config") or {})
    problems = []
    saved = list(checkpoint.get("features") or [])
    if saved != list(features):
        problems.append(f"feature count {len(saved)} does not match prepared {len(features)}")
    if int(config.get("num_layers", -1)) != 8:
        problems.append(f"num_layers is {config.get('num_layers')}")
    layers = sorted({
        int(key.split(".")[2]) for key in checkpoint["state_dict"] if key.startswith("encoder.layers.")
    })
    if layers != list(range(8)):
        problems.append(f"state dict encoder layers are {layers}")
    for key, value in EXPECTED_CONFIG.items():
        if config.get(key) != value:
            problems.append(f"{key} is {config.get(key)}")
    if checkpoint.get("market") != market:
        problems.append(f"checkpoint market is {checkpoint.get('market')}")
    if market == "KOSPI" and checkpoint.get("variant") != VARIANT:
        problems.append(f"variant is {checkpoint.get('variant')!r}, expected {VARIANT}")
    if problems:
        raise SystemExit(f"{market} checkpoint does not match the 8-layer copy: " + "; ".join(problems))


def weight_fingerprint(state) -> tuple[float, int]:
    total = 0.0
    count = 0
    for value in state.values():
        array = np.asarray(value.detach().float().cpu().numpy() if hasattr(value, "detach") else value, dtype=np.float64)
        total += float(array.sum())
        count += int(array.size)
    return round(total, 4), count


def build_extreme_model(torch, backbone):
    """Wrap the copied encoder. The backbone forward still returns one up-logit."""
    from torch import nn

    class ExtremeReturnModel(nn.Module):
        def __init__(self, encoder):
            super().__init__()
            self.backbone = encoder
            d_model = int(encoder.norm.normalized_shape[0])
            dropout = float(EXPECTED_CONFIG["dropout"])
            self.return_head = nn.Sequential(
                nn.Dropout(dropout),
                nn.Linear(d_model, d_model // 2),
                nn.GELU(),
                nn.Dropout(dropout),
                nn.Linear(d_model // 2, 1),
            )
            last = self.return_head[-1]
            nn.init.zeros_(last.weight)
            nn.init.zeros_(last.bias)

        def encode(self, x):
            if x.ndim != 3:
                raise ValueError("input must have shape [batch, lookback, feature_dim]")
            patches = x.unfold(dimension=1, size=self.backbone.patch_len, step=self.backbone.stride)
            patches = patches.contiguous().view(patches.shape[0], patches.shape[1], -1)
            hidden = self.backbone.patch_proj(patches)
            cls = self.backbone.cls.expand(hidden.shape[0], -1, -1)
            hidden = torch.cat([cls, hidden], dim=1)
            if hidden.shape[1] > self.backbone.pos_embedding.shape[1]:
                raise ValueError("too many patches for positional embedding")
            hidden = hidden + self.backbone.pos_embedding[:, : hidden.shape[1], :]
            return self.backbone.norm(self.backbone.encoder(hidden)[:, 0, :])

        def forward(self, x):
            encoded = self.encode(x)
            logit = self.backbone.head(encoded).squeeze(-1)
            expected = self.return_head(encoded).squeeze(-1)
            return logit, expected

    return ExtremeReturnModel(backbone)


def require_cuda(torch):
    if Path(sys.executable) != Path("/usr/bin/python3.12"):
        raise SystemExit(f"training must use /usr/bin/python3.12, not {sys.executable}")
    if not torch.cuda.is_available():
        raise SystemExit("CUDA GPU is not available; refusing CPU")
    device = torch.device("cuda")
    if device.type != "cuda":
        raise SystemExit("refusing CPU")
    name = torch.cuda.get_device_name(0)
    if "RTX 3090" not in name:
        raise SystemExit(f"training GPU is not NVIDIA GeForce RTX 3090: {name}")
    print(f"device cuda {name}", flush=True)
    return device, name


def assert_process_on_3090() -> None:
    import os
    import subprocess
    import time

    pid = str(os.getpid())
    last = ""
    for _attempt in range(10):
        listed = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            check=False,
        )
        last = listed.stdout
        matched = [
            line for line in last.splitlines()
            if line.strip() and line.split(",")[0].strip() == pid
        ]
        if matched:
            gpu = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            if "RTX 3090" not in gpu:
                raise SystemExit(f"nvidia-smi GPU is not RTX 3090: {gpu}")
            print(f"nvidia-smi shows pid {pid} on {gpu}: {matched[0].strip()}", flush=True)
            return
        time.sleep(1)
    raise SystemExit(f"nvidia-smi does not list training pid {pid} during the fit: {last!r}")


def reseed(torch, seed: int) -> None:
    torch.manual_seed(int(seed))
    torch.cuda.manual_seed_all(int(seed))


def cuda_tensors(torch, windows, rows, device):
    features = torch.from_numpy(np.ascontiguousarray(windows, dtype=np.float32)).to(device)
    ups = torch.tensor([row["up"] for row in rows], dtype=torch.float32, device=device)
    returns = torch.tensor([row["actual_return"] for row in rows], dtype=torch.float32, device=device)
    if features.device.type != "cuda" or ups.device.type != "cuda":
        raise RuntimeError("refusing CPU tensors")
    return features, ups, returns


def predict_row(torch, model, features):
    if features.device.type != "cuda":
        raise RuntimeError("refusing CPU prediction")
    model.eval()
    with torch.no_grad():
        logit, expected = model(features)
        probability = torch.sigmoid(logit)
    return float(probability.reshape(-1)[0].item()), float(expected.reshape(-1)[0].item())


def train_prefix(torch, model, features, ups, returns, end: int, epochs: int, learning_rate: float, checked: dict) -> None:
    if end <= 0:
        return
    if features.device.type != "cuda":
        raise RuntimeError("refusing CPU training")
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=0.01)
    loss_fn = torch.nn.BCEWithLogitsLoss()
    scale = float(EXTREME_THRESHOLD) ** 2
    feat = features[:end]
    up = ups[:end]
    target = returns[:end]
    model.train()
    for _epoch in range(epochs):
        order = torch.randperm(end, device=features.device)
        for start in range(0, end, 64):
            batch = order[start:start + 64]
            optimizer.zero_grad(set_to_none=True)
            logit, predicted = model(feat[batch])
            loss = loss_fn(logit, up[batch]) + torch.mean((predicted - target[batch]) ** 2) / scale
            if not torch.isfinite(loss):
                raise FloatingPointError("non-finite loss on cuda")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            if not checked["seen"]:
                assert_process_on_3090()
                checked["seen"] = True


def walk_forward(torch, model, features, ups, returns, rows, start: int, end: int, learning_rate: float, epochs: int, checked: dict):
    scores = []
    for index in range(start, end):
        probability, expected = predict_row(torch, model, features[index:index + 1])
        row = rows[index]
        if not np.isfinite(probability) or not np.isfinite(expected):
            raise FloatingPointError(f"non-finite forecast at {row['session_date']}")
        scores.append({
            "session_date": row["session_date"],
            "actual_return": row["actual_return"],
            "p_up": probability,
            "expected_return": expected,
            "close": row["close"],
            "actual_close": row["actual_close"],
            "predicted_close": row["close"] * (1.0 + expected),
        })
        train_prefix(torch, model, features, ups, returns, index + 1, epochs, learning_rate, checked)
        if (index - start + 1) % 20 == 0 or index + 1 == end:
            print(f"  scored {row['session_date']} ({index + 1}/{end})", flush=True)
    return scores


def capture_rng(torch):
    return {
        "cpu": torch.get_rng_state().clone(),
        "cuda": [state.clone() for state in torch.cuda.get_rng_state_all()],
    }


def restore_rng(torch, saved) -> None:
    torch.set_rng_state(saved["cpu"])
    torch.cuda.set_rng_state_all(saved["cuda"])


def clone_state(model):
    return {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}


def make_model(torch, checkpoint, device):
    backbone = learner.create_large_index_model(len(checkpoint["features"]), checkpoint["config"])
    backbone.load_state_dict(checkpoint["state_dict"], strict=True)
    model = build_extreme_model(torch, backbone).to(device)
    parameter = next(model.parameters())
    if parameter.device.type != "cuda":
        raise RuntimeError("model is not on cuda")
    return model


def confirm_wrapper(torch, model, features) -> None:
    model.eval()
    sample = features[:1]
    with torch.no_grad():
        bare = model.backbone(sample)
        logit, expected = model(sample)
    if not torch.allclose(bare, logit, atol=1e-5, rtol=1e-5):
        raise SystemExit("return-head wrapper changed the copied direction logit")
    if not torch.allclose(expected, torch.zeros_like(expected), atol=1e-6):
        raise SystemExit("return head did not start at zero")


def latest_forecast(torch, model, latest, features, mean, std, device, trained_through: str) -> dict:
    values = latest[features].to_numpy(dtype=np.float32)
    if len(values) < learner.LOOKBACK:
        raise ValueError("not enough rows to score the latest session")
    scaled = learner.apply_scale(values[-learner.LOOKBACK:][None, ...], mean, std)
    features_row = torch.from_numpy(np.ascontiguousarray(scaled, dtype=np.float32)).to(device)
    probability, expected = predict_row(torch, model, features_row)
    asof = latest["session_date"].iloc[-1].strftime("%Y-%m-%d")
    close = float(latest["close"].iloc[-1])
    return {
        "market": str(latest.attrs.get("market") or ""),
        "asof_date": asof,
        "target_date": learner.next_session(asof),
        "close": round(close, 2),
        "p_up": round(probability, 4),
        "p_down": round(1.0 - probability, 4),
        "expected_return": round(float(expected), 6),
        "expected_index": round(close * (1.0 + float(expected)), 2),
        "trained_through": trained_through,
    }


def fit_market(torch, conn, market: str, device, checked: dict) -> dict:
    source = SOURCES[market]
    output = OUTPUTS[market]
    if source.resolve() == output.resolve():
        raise SystemExit("refusing to overwrite the source checkpoint")
    checkpoint = torch.load(source, map_location="cpu", weights_only=False)
    labeled, latest, features = learner.market_frame(conn, market)
    _windows, _labels, dates, features = learner.make_windows(labeled, features)
    require_compatible(checkpoint, features, market)
    fingerprint = weight_fingerprint(checkpoint["state_dict"])
    targets = load_session_targets(conn, market)
    rows = extreme_samples(dates, targets)
    pre_n = sum(1 for row in rows if row["session_date"] < SETTING_CUTOFF)
    if rows and not all(row["session_date"] < SETTING_CUTOFF for row in rows[:pre_n]):
        raise RuntimeError("pre-2026 extreme rows are not a time prefix")
    print(f"{market} extreme {len(rows)} pre-2026 {pre_n} examined-2026 {len(rows) - pre_n} features {len(features)}", flush=True)
    mean = np.asarray(checkpoint["mean"], dtype=np.float32)
    std = np.asarray(checkpoint["std"], dtype=np.float32)
    scaled = learner.apply_scale(_windows, mean, std)
    extreme_x = scaled[[row["index"] for row in rows]] if rows else np.zeros((0, learner.LOOKBACK, len(features)), np.float32)
    seed = SEEDS[market]
    trials = []
    if pre_n == 0:
        chosen_lr = None
        pre_scores = []
        state = None
        rng = None
    else:
        for learning_rate in LEARNING_RATES:
            reseed(torch, seed)
            model = make_model(torch, checkpoint, device)
            confirm_wrapper(torch, model, torch.from_numpy(np.ascontiguousarray(extreme_x[:1], dtype=np.float32)).to(device))
            features_t, ups, returns = cuda_tensors(torch, extreme_x, rows, device)
            scores = walk_forward(
                torch, model, features_t, ups, returns, rows, 0, pre_n, learning_rate, UPDATE_EPOCHS, checked,
            )
            summary = public_metrics(summarize_scores(scores))
            print(f"{market} lr {learning_rate:g} pre-2026 {summary}", flush=True)
            trials.append({
                "learning_rate": learning_rate,
                "rows": scores,
                "state": clone_state(model),
                "rng": capture_rng(torch),
                "summary": summary,
            })
            del model, features_t, ups, returns
            torch.cuda.empty_cache()
        chosen_lr = choose_learning_rate(trials)
        winner = next(trial for trial in trials if float(trial["learning_rate"]) == float(chosen_lr))
        pre_scores = winner["rows"]
        state = winner["state"]
        rng = winner["rng"]
    if chosen_lr is None:
        chosen_lr = 5e-5
        selection_note = "2026년 이전 급등·급락 표본이 없어 학습률을 고르지 않고 5e-5를 사용했다. 2026 성적은 선택에 쓰지 않았다."
        reseed(torch, seed)
        model = make_model(torch, checkpoint, device)
    else:
        selection_note = "학습률은 2026년 이전 급등·급락일의 Brier로만 골랐다."
        model = make_model(torch, checkpoint, device)
        model.load_state_dict(state)
        model.to(device)
        restore_rng(torch, rng)
    if rows:
        features_t, ups, returns = cuda_tensors(torch, extreme_x, rows, device)
        post_scores = walk_forward(
            torch, model, features_t, ups, returns, rows, pre_n, len(rows), chosen_lr, UPDATE_EPOCHS, checked,
        )
    else:
        post_scores = []
    trained_through = rows[-1]["session_date"] if rows else checkpoint.get("last_session")
    if not rows:
        selection_note = "급등·급락 표본이 없어 미세조정하지 않았다. 3% 기준은 낮추지 않았다."
    latest.attrs["market"] = market
    forecast = latest_forecast(torch, model, latest, features, mean, std, device, trained_through)
    forecast["market"] = market
    forecast["examined"] = {
        "pre_2026": public_metrics(summarize_scores(pre_scores)),
        "year_2026": public_metrics(summarize_scores(post_scores)),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    torch.save({
        "state_dict": model.state_dict(),
        "mean": mean,
        "std": std,
        "features": list(features),
        "config": EXPECTED_CONFIG,
        "last_session": forecast["asof_date"],
        "trained_through": trained_through,
        "market": market,
        "variant": VARIANT,
        "architecture": ARCHITECTURE,
        "threshold": EXTREME_THRESHOLD,
        "learning_rate": chosen_lr,
        "update_epochs": UPDATE_EPOCHS,
        "seed": seed,
        "source_checkpoint": str(source.relative_to(ROOT)),
        "source_last_session": checkpoint.get("last_session"),
        "production_weight": 0.0,
    }, output)
    if float(torch.load(output, map_location="cpu", weights_only=False)["production_weight"]) != 0.0:
        raise SystemExit("saved Model C production_weight is not 0")
    too_small = len(rows) < 2
    report = {
        "market": market,
        "source_checkpoint": str(source.relative_to(ROOT)),
        "source_variant": checkpoint.get("variant"),
        "source_last_session": checkpoint.get("last_session"),
        "source_fingerprint": fingerprint,
        "num_layers": 8,
        "feature_count": len(features),
        "seed": seed,
        "update_epochs": UPDATE_EPOCHS,
        "chosen_learning_rate": chosen_lr,
        "selection_note": selection_note,
        "too_small_to_train": too_small,
        "counts": {
            "extreme_pre_2026": pre_n,
            "extreme_2026": len(rows) - pre_n,
            "extreme_total": len(rows),
        },
        "candidates": [
            {"learning_rate": trial["learning_rate"], "pre_2026": trial["summary"]}
            for trial in trials
        ],
        "pre_2026": forecast["examined"]["pre_2026"],
        "examined_2026": forecast["examined"]["year_2026"],
        "forecast": {key: forecast[key] for key in (
            "asof_date", "target_date", "close", "p_up", "p_down", "expected_return", "expected_index", "trained_through",
        )},
    }
    del model
    torch.cuda.empty_cache()
    return report


def main() -> int:
    import torch

    device, gpu_name = require_cuda(torch)
    if any(SOURCES[market].resolve() == OUTPUTS[market].resolve() for market in SOURCES):
        raise SystemExit("output path collides with a source checkpoint")
    before = {str(path.relative_to(ROOT)): file_sha256(path) for path in (
        *SOURCES.values(), ROOT / "models" / "dnn" / "incremental_kospi_prior_2e-4.pt",
    )}
    conn = sqlite3.connect(ROOT / "data" / "processed" / "index_forecast.sqlite")
    checked = {"seen": False}
    reports = [fit_market(torch, conn, market, device, checked) for market in ("KOSPI", "KOSDAQ")]
    if not checked["seen"]:
        raise SystemExit("training never reached a CUDA optimizer step")
    fingerprints = [tuple(report["source_fingerprint"]) for report in reports]
    if fingerprints[0] == fingerprints[1]:
        raise SystemExit("KOSPI and KOSDAQ source weights are identical")
    after = {str(path.relative_to(ROOT)): file_sha256(path) for path in (
        *SOURCES.values(), ROOT / "models" / "dnn" / "incremental_kospi_prior_2e-4.pt",
    )}
    if after != before:
        raise SystemExit(f"source checkpoint bytes changed: {before} -> {after}")
    generated_at = utc_stamp()
    accuracy = {
        "schema_version": "model_c_accuracy_v1",
        "model_name": "모델 C",
        "architecture": ARCHITECTURE,
        "variant": VARIANT,
        "threshold": EXTREME_THRESHOLD,
        "production_weight": 0.0,
        "trigger": "manual_gpu",
        "device": "cuda",
        "gpu": gpu_name,
        "generated_at": generated_at,
        "update_epochs": UPDATE_EPOCHS,
        "learning_rate_candidates": list(LEARNING_RATES),
        "setting_pool": "session_date < 2026-01-01",
        "counting_rule": "다음 세션 종가 수익률의 절대값이 3% 이상이고 다음 종가가 있는 날만 학습하고 채점한다.",
        "score_protocol": "각 급등·급락일은 그보다 이전 급등·급락만 반영한 모델이 채점한 뒤 갱신한다. 출발 가중치와 입력 표준화는 복사한 8층 체크포인트이며, 그 last_session까지 전체 세션 학습을 포함한다.",
        "kospi_weights_loaded_into_kosdaq": False,
        "source_sha256": before,
        "markets": reports,
    }
    forecast = {
        "schema_version": "model_c_forecast_v1",
        "model_name": "모델 C",
        "architecture": ARCHITECTURE,
        "trigger": "manual_gpu",
        "production_weight": 0.0,
        "generated_at": generated_at,
        "gpu": gpu_name,
        "threshold": EXTREME_THRESHOLD,
        "scope": "급등·급락 전용",
        "counting_rule": accuracy["counting_rule"],
        "markets": [
            {
                "market": report["market"],
                **report["forecast"],
                "examined": {
                    "pre_2026": report["pre_2026"],
                    "year_2026": report["examined_2026"],
                },
            }
            for report in reports
        ],
    }
    ACCURACY_PATH.write_text(json.dumps(accuracy, ensure_ascii=False, indent=2) + "\n")
    FORECAST_PATH.parent.mkdir(parents=True, exist_ok=True)
    FORECAST_PATH.write_text(json.dumps(forecast, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"accuracy": str(ACCURACY_PATH), "forecast": str(FORECAST_PATH), "markets": [
        {"market": report["market"], "counts": report["counts"], "pre_2026": report["pre_2026"], "examined_2026": report["examined_2026"], "chosen_learning_rate": report["chosen_learning_rate"], "forecast": report["forecast"]}
        for report in reports
    ]}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
