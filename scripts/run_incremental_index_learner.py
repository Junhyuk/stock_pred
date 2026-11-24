#!/usr/bin/env python3
"""Continue index training as new sessions arrive.

The model scores the next block of sessions first. Only after that score is
stored are those labels used for another short time-ordered update.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.models.incremental_timeseries import block_score, iter_learning_blocks
from roboquant.models.large_index_transformer import create_large_index_model

PRICE = ["return_1d", "return_5d", "return_20d", "volatility_5d", "volatility_20d", "ma_gap_5d", "ma_gap_20d"]
LAG1_LEVELS = {
    "dxy_return_1d": "YAHOO_DXY",
    "btc_return_1d": "YAHOO_BTC",
    "eth_return_1d": "YAHOO_ETH",
    "oil_return_1d": "YAHOO_WTI",
}
LAG1_YIELDS = [f"US_TREASURY_{tenor}Y" for tenor in (1, 3, 5, 10)]
SAME_DAY_YIELDS = [f"JP_JGB_{tenor}Y" for tenor in (1, 3, 5, 10)] + [f"KR_KTB_{tenor}Y" for tenor in (1, 3, 5, 10)]
LOOKBACK = 96
BLOCK_SIZE = 60
MODEL_CONFIG = {"patch_len": 8, "stride": 4, "d_model": 384, "n_heads": 8, "num_layers": 8, "dropout": 0.2}
# Chosen only on walk-forward blocks ending before 2026. Not retuned on 2026.
KOSPI_FINETUNE = {
    "seed": 44,
    "initial_epochs": 6,
    "initial_lr": 2e-4,
    "update_epochs": 1,
    "update_lr": 5e-5,
    "block_size": 60,
    "variant": "8층 미세조정",
}


def load_series(conn, code: str) -> pd.DataFrame:
    frame = pd.read_sql_query(
        "SELECT observation_date, value FROM macro_series WHERE series_code = ? ORDER BY observation_date",
        conn, params=[code])
    frame["date"] = pd.to_datetime(frame["observation_date"]).dt.date
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    return frame.dropna(subset=["date", "value"]).drop_duplicates("date")


def align(dates, frame, column, lag_days: int, difference: bool) -> pd.DataFrame:
    right = frame[["date", "value"]].sort_values("date").copy()
    if difference:
        right["value"] = right["value"].diff()
    else:
        right["value"] = right["value"].pct_change()
    right = right.dropna(subset=["value"])
    right["_asof"] = pd.to_datetime(right["date"]).astype("datetime64[ns]") + pd.Timedelta(days=lag_days)
    left = pd.DataFrame({"_asof": pd.to_datetime(pd.Series(list(dates)).drop_duplicates().sort_values()).astype("datetime64[ns]")})
    merged = pd.merge_asof(left, right[["_asof", "value"]].sort_values("_asof"), on="_asof", direction="backward")
    return merged.rename(columns={"value": column})


def market_frame(conn, market: str) -> pd.DataFrame:
    frame = pd.read_sql_query(
        "SELECT session_date, close FROM index_eod WHERE index_code = ? ORDER BY session_date",
        conn, params=[market])
    frame["session_date"] = pd.to_datetime(frame["session_date"])
    close = pd.to_numeric(frame["close"], errors="coerce")
    log_ret = np.log(close / close.shift(1))
    frame["return_1d"] = close.pct_change()
    frame["return_5d"] = close.pct_change(5)
    frame["return_20d"] = close.pct_change(20)
    frame["volatility_5d"] = log_ret.rolling(5, min_periods=5).std()
    frame["volatility_20d"] = log_ret.rolling(20, min_periods=20).std()
    frame["ma_gap_5d"] = close / close.rolling(5, min_periods=5).mean() - 1.0
    frame["ma_gap_20d"] = close / close.rolling(20, min_periods=20).mean() - 1.0
    next_close = close.shift(-1)
    frame["actual_up"] = (next_close > close).where(next_close.notna())
    dates = frame["session_date"].dt.date
    frame["_asof"] = pd.to_datetime(dates).astype("datetime64[ns]")
    columns = list(PRICE)
    for column, code in LAG1_LEVELS.items():
        frame = frame.merge(align(dates, load_series(conn, code), column, 1, False), on="_asof", how="left")
        columns.append(column)
    usd = load_series(conn, "YAHOO_USDKRW")
    frame = frame.merge(align(dates, usd, "usdkrw_return_1d", 0, False), on="_asof", how="left")
    columns.append("usdkrw_return_1d")
    for code in LAG1_YIELDS:
        column = code.lower()
        frame = frame.merge(align(dates, load_series(conn, code), column, 1, True), on="_asof", how="left")
        columns.append(column)
    for code in SAME_DAY_YIELDS:
        column = code.lower()
        frame = frame.merge(align(dates, load_series(conn, code), column, 0, True), on="_asof", how="left")
        columns.append(column)
    labeled = frame.dropna(subset=[*columns, "actual_up"]).reset_index(drop=True)
    latest = frame.dropna(subset=columns).reset_index(drop=True)
    return labeled, latest, columns


def make_windows(frame: pd.DataFrame, features: list[str]):
    values = frame[features].to_numpy(dtype=np.float32)
    labels = frame["actual_up"].to_numpy(dtype=np.float32)
    dates = frame["session_date"].dt.strftime("%Y-%m-%d").tolist()
    rows, ys, meta = [], [], []
    for end in range(LOOKBACK - 1, len(frame)):
        rows.append(values[end - LOOKBACK + 1:end + 1])
        ys.append(labels[end])
        meta.append(dates[end])
    return np.stack(rows), np.asarray(ys, dtype=np.float32), meta, features


def scale_fit(train_x):
    flat = train_x.reshape(-1, train_x.shape[-1])
    mean = flat.mean(axis=0)
    std = flat.std(axis=0)
    std[std < 1e-6] = 1.0
    return mean, std


def apply_scale(x, mean, std):
    return (x - mean) / std


def train_epochs(torch, model, x, y, device, epochs: int, learning_rate: float):
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=0.01)
    loss_fn = torch.nn.BCEWithLogitsLoss()
    features = torch.from_numpy(x.astype(np.float32))
    labels = torch.from_numpy(y.astype(np.float32))
    model.train()
    for _epoch in range(epochs):
        order = torch.randperm(len(features))
        for start in range(0, len(features), 64):
            batch = order[start:start + 64]
            optimizer.zero_grad(set_to_none=True)
            loss = loss_fn(model(features[batch].to(device)), labels[batch].to(device))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()


def predict(torch, model, x, device):
    model.eval()
    with torch.no_grad():
        logit = model(torch.from_numpy(x.astype(np.float32)).to(device))
    return torch.sigmoid(logit).float().cpu().numpy()


def learn_market(torch, conn, market: str, device, checkpoint: Path, schedule: dict | None = None) -> dict:
    schedule = schedule or {
        "initial_epochs": 6, "initial_lr": 2e-4, "update_epochs": 2, "update_lr": 2e-4, "block_size": BLOCK_SIZE,
    }
    frame, _latest, features = market_frame(conn, market)
    x, y, dates, features = make_windows(frame, features)
    first_test = next(index for index, day in enumerate(dates) if day >= "2024-01-01")
    if first_test < 200:
        raise ValueError(f"{market} does not have enough history before 2024")
    if schedule.get("seed") is not None:
        torch.manual_seed(schedule["seed"])
        if device.type == "cuda":
            torch.cuda.manual_seed_all(schedule["seed"])
    model = create_large_index_model(len(features), MODEL_CONFIG).to(device)
    initial_mean, initial_std = scale_fit(x[:first_test])
    train_epochs(
        torch, model, apply_scale(x[:first_test], initial_mean, initial_std), y[:first_test], device,
        schedule["initial_epochs"], schedule["initial_lr"])
    curve = []
    for train_end, block_end in iter_learning_blocks(len(x), first_test, schedule["block_size"]):
        mean, std = scale_fit(x[:train_end])
        probability = predict(torch, model, apply_scale(x[train_end:block_end], mean, std), device)
        scored = block_score(y[train_end:block_end] > 0.5, probability)
        scored.update(start=dates[train_end], end=dates[block_end - 1], train_end=dates[train_end - 1])
        curve.append(scored)
        train_epochs(
            torch, model, apply_scale(x[:block_end], mean, std), y[:block_end], device,
            schedule["update_epochs"], schedule["update_lr"])
        print(market, scored["start"], scored["end"], "brier", scored["brier"], "hit", scored["direction_hit"], flush=True)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    mean, std = scale_fit(x)
    torch.save({
        "state_dict": model.state_dict(),
        "mean": mean,
        "std": std,
        "features": features,
        "config": MODEL_CONFIG,
        "last_session": dates[-1],
        "market": market,
        "schedule": schedule,
        "variant": schedule.get("variant"),
        "production_weight": 0.0,
    }, checkpoint)
    return {"market": market, "blocks": curve, "checkpoint": str(checkpoint), "last_session": dates[-1]}


def next_session(day: str) -> str:
    try:
        import exchange_calendars as calendars
        return calendars.get_calendar("XKRX").next_session(day).date().isoformat()
    except Exception:
        nxt = pd.Timestamp(day) + pd.Timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += pd.Timedelta(days=1)
        return nxt.date().isoformat()


def predict_market(torch, conn, market: str, device) -> dict:
    checkpoint = torch.load(Path("models/dnn") / f"incremental_{market.lower()}.pt", map_location=device, weights_only=False)
    _labeled, latest, features = market_frame(conn, market)
    values = latest[features].to_numpy(dtype=np.float32)
    if len(values) < LOOKBACK:
        raise ValueError(f"{market} does not have enough rows to score the latest session")
    window = values[-LOOKBACK:]
    scaled = apply_scale(window[None, ...], checkpoint["mean"], checkpoint["std"])
    model = create_large_index_model(len(features), checkpoint["config"]).to(device)
    model.load_state_dict(checkpoint["state_dict"])
    probability = float(predict(torch, model, scaled, device)[0])
    asof = latest["session_date"].iloc[-1].strftime("%Y-%m-%d")
    row = {
        "market": market,
        "asof_date": asof,
        "target_date": next_session(asof),
        "close": round(float(latest["close"].iloc[-1]), 2),
        "p_up": round(probability, 4),
        "p_down": round(1 - probability, 4),
        "trained_through": checkpoint.get("last_session"),
    }
    if checkpoint.get("variant"):
        row["variant"] = checkpoint["variant"]
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default="data/processed/index_forecast.sqlite")
    parser.add_argument("--output", default="models/dnn/incremental_learning_curve.json")
    parser.add_argument("--market", action="append", choices=("KOSPI", "KOSDAQ"), help="Train one market. Repeatable.")
    parser.add_argument("--predict", action="store_true", help="Score the latest session only. Does not train.")
    parser.add_argument("--forecast-json", default="docs/data/model_b_forecast.json")
    args = parser.parse_args()
    import torch
    if not torch.cuda.is_available():
        raise SystemExit("CUDA GPU is not available")
    device = torch.device("cuda")
    if args.predict:
        conn = sqlite3.connect(args.sqlite)
        markets = [predict_market(torch, conn, market, device) for market in ("KOSPI", "KOSDAQ")]
        payload = {
            "schema_version": "model_b_forecast_v1",
            "model_name": "모델 B",
            "architecture": "8층 트랜스포머",
            "trigger": "manual_gpu",
            "production_weight": 0.0,
            "generated_at": datetime.now(UTC).isoformat(),
            "gpu": torch.cuda.get_device_name(0),
            "markets": markets,
        }
        output = Path(args.forecast_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    conn = sqlite3.connect(args.sqlite)
    markets = []
    for market in (args.market or ["KOSPI", "KOSDAQ"]):
        schedule = KOSPI_FINETUNE if market == "KOSPI" else None
        markets.append(learn_market(
            torch, conn, market, device, Path("models/dnn") / f"incremental_{market.lower()}.pt", schedule))
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "method": "expanding_window_time_series_update",
        "block_size": BLOCK_SIZE,
        "production_weight": 0.0,
        "gpu": torch.cuda.get_device_name(0),
        "markets": markets,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
