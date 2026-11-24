#!/usr/bin/env python3
"""Compare 2026 next-session forecasts that see 20 days of the same inputs.

Rates, investor net buying, US index returns, oil, and the dollar are aligned
before the forecast. 2026 rows are scored only. They do not choose a model.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/processed/index_forecast.sqlite"
LOOKBACK = 20
TEST_START = "2026-01-01"
YIELD_DIFF = {
    0: [f"KR_KTB_{tenor}Y" for tenor in (1, 3, 5, 10)] + [f"JP_JGB_{tenor}Y" for tenor in (1, 3, 5, 10)],
    1: [f"US_TREASURY_{tenor}Y" for tenor in (1, 3, 5, 10)],
}
RETURN_LEVELS = {0: ["YAHOO_USDKRW"], 1: ["YAHOO_WTI", "YAHOO_DXY", "YAHOO_BTC", "YAHOO_ETH"]}
READY_RETURNS = {1: ["US_SPX", "US_NASDAQ", "US_SOX", "US_VIX"]}
FLOW_TYPES = ("외국인", "기관", "개인")


def load_macro(conn, code: str) -> pd.DataFrame:
    frame = pd.read_sql_query(
        "SELECT observation_date, value FROM macro_series WHERE series_code = ? ORDER BY observation_date",
        conn, params=[code])
    frame["date"] = pd.to_datetime(frame["observation_date"])
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    return frame.dropna(subset=["date", "value"]).drop_duplicates("date")


def align_macro(dates: pd.Series, frame: pd.DataFrame, column: str, lag_days: int, transform: str) -> pd.DataFrame:
    right = frame[["date", "value"]].sort_values("date").copy()
    if transform == "diff":
        right["value"] = right["value"].diff()
    elif transform == "pct":
        right["value"] = right["value"].pct_change()
    right = right.dropna(subset=["value"])
    right["_asof"] = (right["date"] + pd.Timedelta(days=lag_days)).astype("datetime64[ns]")
    left = pd.DataFrame({"_asof": pd.to_datetime(dates).astype("datetime64[ns]")}).sort_values("_asof")
    merged = pd.merge_asof(left, right[["_asof", "value"]].sort_values("_asof"), on="_asof", direction="backward")
    return merged.rename(columns={"value": column})


def market_panel(conn, market: str) -> tuple[pd.DataFrame, list[str]]:
    price = pd.read_sql_query(
        "SELECT session_date, close FROM index_eod WHERE index_code = ? ORDER BY session_date",
        conn, params=[market])
    price["session_date"] = pd.to_datetime(price["session_date"])
    close = pd.to_numeric(price["close"], errors="coerce")
    price["ret_1d"] = close.pct_change()
    nxt = close.shift(-1)
    price["actual_up"] = (nxt > close).where(nxt.notna())
    columns = ["ret_1d"]
    flow = pd.read_sql_query(
        """
        SELECT session_date, investor_type, net_value FROM investor_flow
        WHERE market = ? AND source = 'naver_investor_trend_unofficial'
        """,
        conn, params=[market])
    flow["session_date"] = pd.to_datetime(flow["session_date"])
    flow["net_eok"] = pd.to_numeric(flow["net_value"], errors="coerce") / 1e8
    wide = flow.pivot_table(index="session_date", columns="investor_type", values="net_eok", aggfunc="last")
    wide = wide.reindex(columns=list(FLOW_TYPES))
    wide.columns = [f"flow_{name}" for name in wide.columns]
    price = price.merge(wide, left_on="session_date", right_index=True, how="left")
    for name in wide.columns:
        price[name] = price[name].shift(1)
        columns.append(name)
    dates = price["session_date"]
    price["_asof"] = pd.to_datetime(dates).astype("datetime64[ns]")
    for lag, codes in YIELD_DIFF.items():
        for code in codes:
            column = code.lower()
            piece = align_macro(dates, load_macro(conn, code), column, lag, "diff")
            price = price.merge(piece, on="_asof", how="left")
            columns.append(column)
    for lag, codes in RETURN_LEVELS.items():
        for code in codes:
            column = code.lower() + "_ret"
            piece = align_macro(dates, load_macro(conn, code), column, lag, "pct")
            price = price.merge(piece, on="_asof", how="left")
            columns.append(column)
    for lag, codes in READY_RETURNS.items():
        for code in codes:
            column = code.lower() + "_ret"
            piece = align_macro(dates, load_macro(conn, code), column, lag, "level")
            price = price.merge(piece, on="_asof", how="left")
            columns.append(column)
    return price, columns


def windows(frame: pd.DataFrame, columns: list[str]):
    values = frame[columns].to_numpy(dtype=np.float64)
    labels = frame["actual_up"].to_numpy()
    dates = frame["session_date"].dt.strftime("%Y-%m-%d").to_numpy()
    rows, ys, meta = [], [], []
    for end in range(LOOKBACK - 1, len(frame)):
        block = values[end - LOOKBACK + 1:end + 1]
        if not np.isfinite(block).all() or not np.isfinite(labels[end]):
            continue
        rows.append(block.astype(np.float32))
        ys.append(float(labels[end]))
        meta.append(dates[end])
    return np.stack(rows), np.asarray(ys, dtype=np.float32), meta


def metrics(actual: np.ndarray, probability: np.ndarray) -> dict:
    clipped = np.clip(probability, 1e-6, 1 - 1e-6)
    return {
        "n": int(len(actual)),
        "brier": round(float(np.mean((clipped - actual) ** 2)), 4),
        "direction_hit": round(float(np.mean((clipped >= 0.5) == (actual >= 0.5))), 4),
        "mean_p_up": round(float(np.mean(clipped)), 4),
        "up_rate": round(float(np.mean(actual)), 4),
    }


def scale_fit(train_x: np.ndarray):
    flat = train_x.reshape(-1, train_x.shape[-1])
    mean = flat.mean(axis=0)
    std = flat.std(axis=0)
    std[std < 1e-6] = 1.0
    return mean.astype(np.float32), std.astype(np.float32)


def apply_scale(x, mean, std):
    return (x - mean) / std


def fit_logistic(x, y):
    from sklearn.linear_model import LogisticRegression
    model = LogisticRegression(C=1.0, max_iter=400)
    model.fit(x, y.astype(int))
    return model


def fit_lightgbm(x, y):
    import lightgbm as lgb
    model = lgb.LGBMClassifier(
        n_estimators=200, learning_rate=0.05, num_leaves=15, min_child_samples=20,
        subsample=0.9, colsample_bytree=0.9, random_state=42, verbosity=-1)
    model.fit(x, y.astype(int))
    return model


def train_transformer(torch, train_x, train_y, val_x, val_y, device):
    model = torch.nn.Sequential()
    feature_dim = train_x.shape[-1]
    network = _SequenceModel(feature_dim).to(device)
    optimizer = torch.optim.AdamW(network.parameters(), lr=1e-3, weight_decay=0.01)
    loss_fn = torch.nn.BCEWithLogitsLoss()
    x = torch.from_numpy(train_x)
    y = torch.from_numpy(train_y)
    best_state, best_score, patience = None, 1e9, 0
    for _epoch in range(40):
        network.train()
        order = torch.randperm(len(x))
        for start in range(0, len(x), 32):
            batch = order[start:start + 32]
            optimizer.zero_grad(set_to_none=True)
            loss = loss_fn(network(x[batch].to(device)), y[batch].to(device))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(network.parameters(), 1.0)
            optimizer.step()
        probability = predict_transformer(torch, network, val_x, device)
        score = metrics(val_y, probability)["brier"]
        if score < best_score - 1e-4:
            best_score = score
            best_state = {key: value.detach().cpu().clone() for key, value in network.state_dict().items()}
            patience = 0
        else:
            patience += 1
            if patience >= 8:
                break
    if best_state is not None:
        network.load_state_dict(best_state)
    return network


class _SequenceModel:
    def __new__(cls, feature_dim: int):
        import torch
        from torch import nn

        class Net(nn.Module):
            def __init__(self):
                super().__init__()
                d_model = 64
                self.proj = nn.Linear(feature_dim, d_model)
                layer = nn.TransformerEncoderLayer(
                    d_model=d_model, nhead=4, dim_feedforward=128, dropout=0.2,
                    activation="gelu", batch_first=True, norm_first=True)
                self.encoder = nn.TransformerEncoder(layer, num_layers=2, enable_nested_tensor=False)
                self.head = nn.Linear(d_model, 1)

            def forward(self, x):
                hidden = self.encoder(self.proj(x))
                return self.head(hidden[:, -1, :]).squeeze(-1)

        return Net()


def predict_transformer(torch, model, x, device):
    model.eval()
    with torch.no_grad():
        logit = model(torch.from_numpy(x.astype(np.float32)).to(device))
    return torch.sigmoid(logit).float().cpu().numpy()


def evaluate_market(torch, conn, market: str, device) -> dict:
    frame, columns = market_panel(conn, market)
    x, y, dates = windows(frame, columns)
    test_at = next(index for index, day in enumerate(dates) if day >= TEST_START)
    val_at = max(test_at - 60, LOOKBACK)
    if val_at >= test_at or test_at < 80:
        raise ValueError(f"{market} does not have a pre-2026 validation block and a 2026 test")
    mean, std = scale_fit(x[:val_at])
    train_x = apply_scale(x[:val_at], mean, std)
    val_x = apply_scale(x[val_at:test_at], mean, std)
    test_x = apply_scale(x[test_at:], mean, std)
    train_y, val_y, test_y = y[:val_at], y[val_at:test_at], y[test_at:]
    last_train, last_test = train_x[:, -1, :], test_x[:, -1, :]
    logistic = fit_logistic(last_train, train_y)
    tree = fit_lightgbm(last_train, train_y)
    sequence = train_transformer(torch, train_x, train_y, val_x, val_y, device)
    logistic_p = logistic.predict_proba(last_test)[:, 1]
    tree_p = tree.predict_proba(last_test)[:, 1]
    sequence_p = predict_transformer(torch, sequence, test_x, device)
    constant_p = np.full(len(test_y), float(train_y.mean()), dtype=np.float32)
    ensemble_p = 0.5 * tree_p + 0.5 * sequence_p
    return {
        "market": market,
        "features": columns,
        "feature_count": len(columns),
        "lookback": LOOKBACK,
        "train_end": dates[val_at - 1],
        "validation_start": dates[val_at],
        "validation_end": dates[test_at - 1],
        "test_start": dates[test_at],
        "test_end": dates[-1],
        "models": {
            "constant_up_rate": metrics(test_y, constant_p),
            "logistic_last_day": metrics(test_y, logistic_p),
            "lightgbm_last_day": metrics(test_y, tree_p),
            "transformer_20d": metrics(test_y, sequence_p),
            "lightgbm_transformer_ensemble": metrics(test_y, ensemble_p),
        },
        "production_weight": 0.0,
    }


def main() -> int:
    import torch
    if not torch.cuda.is_available():
        raise SystemExit("CUDA GPU is not available")
    device = torch.device("cuda")
    conn = sqlite3.connect(DB)
    markets = [evaluate_market(torch, conn, market, device) for market in ("KOSPI", "KOSDAQ")]
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "protocol": "20_day_inputs_train_before_2026_score_2026",
        "flow_lag_sessions": 1,
        "us_lag_calendar_days": 1,
        "production_weight": 0.0,
        "gpu": torch.cuda.get_device_name(0),
        "markets": markets,
    }
    output = ROOT / "models/dnn/timeseries_20d_accuracy.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
