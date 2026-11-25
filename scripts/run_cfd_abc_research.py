#!/usr/bin/env python3
"""Fit local A/B/C CFD challengers and score them on identical chronological sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.index_forecast.market_calendar import exchange_schedule
from roboquant.index_forecast.reliable import ReliableSettings, _fit_base

CORE_COLUMNS = (
    "feature_kr_return_1",
    "feature_kr_return_5",
    "feature_kr_vol20",
    "feature_kr_range",
    "feature_kr_volume_ratio",
)
US_COLUMNS = tuple(
    f"feature_us_{market}_return" for market in ("spx", "nasdaq", "sox", "ewy", "koru", "vix")
) + ("feature_us_vix_level", "feature_fx_h10_change_5d")
CFD_COLUMNS = (
    "feature_cfd_log_balance",
    "feature_cfd_log_count",
    "feature_cfd_change1",
    "feature_cfd_change5",
)
SEED = 42
EPOCHS = 60
CLASSES = ("down", "neutral", "up")
STAGE = {"A": "postclose", "B": "premarket", "C": "premarket"}


def extreme_labels(returns):
    values = np.asarray(returns, dtype=float)
    threshold = 0.03 - 4 * np.finfo(float).eps
    return np.where(values <= -threshold, 0, np.where(values >= threshold, 2, 1))


def chronological_splits(rows, *, minimum_train, refit_sessions):
    positions = np.arange(len(rows))
    first = minimum_train
    while first < len(rows):
        cutoff = rows.cutoff_at.iloc[first]
        eligible = (rows.label_at <= cutoff).to_numpy()
        train = positions[(positions < first) & eligible]
        if len(train) < minimum_train:
            first += 1
            continue
        end = min(first + refit_sessions, len(rows))
        yield train, positions[first:end]
        first = end


def _clock(values):
    stamps = values.astype("string").dropna()
    if not stamps.str.contains(r"(?:Z|[+-]\d\d:\d\d)$").all():
        raise ValueError(f"{values.name} must carry an explicit timezone")
    return pd.to_datetime(values, utc=True, format="mixed")


def cfd_features(base, observations, *, timing, knowledge_at):
    """Join releases to each original cutoff and compare exact KRX sessions."""
    if timing not in {"observed", "retrospective"}:
        raise ValueError("unknown CFD availability policy")
    joined = base.copy()
    joined["cfd_observation"] = pd.Series(pd.NA, index=joined.index, dtype="string")
    joined["cfd_available_at"] = pd.Series(pd.NaT, index=joined.index, dtype="datetime64[ns, UTC]")
    for name in (*CFD_COLUMNS, "feature_cfd_change20"):
        joined[name] = np.nan
    if observations.empty:
        return joined
    source = observations.copy()
    source["received_at"] = _clock(source.received_at)
    source = source.loc[source.received_at.le(knowledge_at)].copy()
    if source.empty:
        return joined
    date_column = "received_at" if timing == "observed" else "retrospective_available_at"
    source["available_at"] = _clock(source[date_column])
    if "published_at" in source:
        published = _clock(source.published_at)
        source["available_at"] = source.available_at.mask(
            published > source.available_at, published
        )
    source = source.dropna(subset=["available_at"])
    if source.empty:
        return joined
    if source.duplicated(["market", "observation_date"]).any():
        raise ValueError("multiple raw versions require an explicit CFD snapshot")
    if not source.value_krw.gt(0).all():
        raise ValueError("market aggregates require a positive reported balance")
    if "valid_count" in source and not source.valid_count.gt(0).all():
        raise ValueError("available market row counts must be positive")

    _, labels = exchange_schedule(
        "XKRX",
        pd.Timestamp(source.observation_date.min()) - pd.Timedelta("45d"),
        max(source.observation_date.max(), base.asof_date.max()),
    )
    sessions = labels.strftime("%Y-%m-%d").to_list()
    offsets = {day: i for i, day in enumerate(sessions)}
    for market, values in source.groupby("market"):
        history = (
            values.loc[values.observation_date.isin(offsets)]
            .set_index("observation_date")
            .sort_index()
        )
        for index, row in base.loc[base.market.eq(market)].iterrows():
            known = history.loc[
                (history.index <= row.asof_date) & history.available_at.le(row.cutoff_at)
            ]
            if known.empty:
                continue
            date = known.index[-1]
            latest = known.iloc[-1]
            anchor = offsets.get(row.asof_date)
            if anchor is None or anchor - offsets[date] > 3:
                continue
            joined.at[index, "cfd_observation"] = date
            joined.at[index, "cfd_available_at"] = latest.available_at
            joined.at[index, "feature_cfd_log_balance"] = np.log(latest.value_krw)
            if "valid_count" in latest:
                joined.at[index, "feature_cfd_log_count"] = np.log1p(latest.valid_count)
            for lag in (1, 5, 20):
                previous = offsets[date] - lag
                if previous >= 0 and sessions[previous] in known.index:
                    amount = known.at[sessions[previous], "value_krw"]
                    joined.at[index, f"feature_cfd_change{lag}"] = latest.value_krw / amount - 1
    return joined


def freesis_features(base, document, *, knowledge_at):
    """Use a separately labeled next-session publication proxy for FreeSIS archives."""
    tables = document["series"]
    dates = [item["date"] for item in tables["with_margin"]]
    if not dates:
        return base, ()
    _, sessions = exchange_schedule("XKRX", dates[0], pd.Timestamp(dates[-1]) + pd.Timedelta("15d"))
    labels = sessions.strftime("%Y-%m-%d").tolist()
    next_session = dict(zip(labels[:-1], labels[1:], strict=True))
    result = base.copy()
    added = []
    for metric, table in tables.items():
        if metric not in {"with_margin", "without_margin"}:
            raise ValueError("unknown FreeSIS margin definition")
        records = []
        if [item["date"] for item in table] != dates:
            raise ValueError("FreeSIS gross and net observation calendars differ")
        for item in table:
            date = item["date"]
            if date not in next_session:
                continue
            published_proxy = pd.Timestamp(next_session[date], tz="Asia/Seoul") + pd.Timedelta("1d")
            for market in ("KOSPI", "KOSDAQ"):
                records.append(
                    {
                        "market": market,
                        "observation_date": date,
                        "received_at": document["generated_at"],
                        "retrospective_available_at": published_proxy.isoformat(),
                        "value_krw": item[f"{market.lower()}_krw"],
                    }
                )
        if not records:
            continue
        joined = cfd_features(
            base, pd.DataFrame(records), timing="retrospective", knowledge_at=knowledge_at
        )
        for name in ("log_balance", "change1", "change5", "change20"):
            column = f"feature_frees_is_{metric}_{name}"
            result[column] = joined[f"feature_cfd_{name}"]
            added.append(column)
    return result, tuple(added)


def price_features(base, payload):
    """Align SKHY and KORU to completed NY sessions for the archived price vintage."""
    result = base.copy()
    for ticker in ("SKHY", "KORU"):
        name = f"feature_{ticker.lower()}_chart_return"
        result[name] = np.nan
        price = payload["instruments"][ticker]["points"]
        if not price:
            continue
        values = pd.DataFrame(price).set_index("date").close.sort_index()
        calendar, sessions = exchange_schedule("XNYS", values.index.min(), values.index.max())
        dates = sessions.strftime("%Y-%m-%d")
        completed = pd.DatetimeIndex(
            [calendar.session_close(day) for day in sessions]
        ) + pd.Timedelta("15min")
        returns = values.reindex(dates).pct_change(fill_method=None)
        cutoffs = pd.DatetimeIndex(result.cutoff_at)
        indices = completed.searchsorted(cutoffs, side="right") - 1
        # Historical rows can use this current-vintage source only in retrospective runs.
        unique_days = pd.Series(dates.take(np.maximum(indices, 0)), index=result.index)
        mapped = unique_days.map(returns).where(indices >= 0)
        # A truncated quote archive cannot supply returns after its last complete session.
        _, subsequent = exchange_schedule(
            "XNYS",
            values.index.max(),
            max(values.index.max(), str(cutoffs.max().tz_convert("America/New_York").date())),
        )
        if len(subsequent) > 1:
            next_close = calendar.session_close(subsequent[1]) + pd.Timedelta("15min")
            mapped = mapped.where(cutoffs < next_close)
        result[name] = mapped
    return result


def _predict_a(train, test, columns):
    work = train.rename(columns={"actual_return": "label_return"})
    model = _fit_base(
        work, columns, "linear", ReliableSettings(issuance_protocol="after_close_daily_v1")
    )
    x = test[columns].to_numpy()
    prediction = model["regressor"].predict(x)
    classifier = model["classifier"]
    probability = (
        classifier.predict_proba(x)[:, 1]
        if classifier is not None
        else np.full(len(test), model["up_rate"])
    )
    return np.column_stack((prediction, probability))


def _cuda_predict(model, train, test, columns):
    import torch
    from torch import nn

    torch.manual_seed(SEED)
    x = torch.as_tensor(train[columns].to_numpy(), dtype=torch.float32, device="cuda")
    y = torch.as_tensor(train.actual_return.to_numpy(), dtype=torch.float32, device="cuda")
    mean, scale = x.mean(dim=0), x.std(dim=0, unbiased=False).clamp_min(1e-6)
    x = ((x - mean) / scale).clamp(-8, 8)
    target = torch.as_tensor(test[columns].to_numpy(), dtype=torch.float32, device="cuda")
    target = ((target - mean) / scale).clamp(-8, 8)
    output_count = 2 if model == "B" else 3
    network = nn.Sequential(
        nn.Linear(len(columns), 16),
        nn.Tanh(),
        nn.Linear(16, 8),
        nn.Tanh(),
        nn.Linear(8, output_count),
    ).to("cuda")
    optim = torch.optim.AdamW(network.parameters(), lr=0.004, weight_decay=0.04)
    tail = torch.as_tensor(extreme_labels(train.actual_return), device="cuda")
    for _ in range(EPOCHS):
        optim.zero_grad(set_to_none=True)
        scores = network(x)
        if model == "B":
            loss = nn.functional.smooth_l1_loss(scores[:, 0], y * 100.0)
            loss = loss + nn.functional.binary_cross_entropy_with_logits(
                scores[:, 1], (y > 0).float()
            )
        else:
            loss = nn.functional.cross_entropy(scores, tail)
        loss.backward()
        optim.step()
    with torch.inference_mode():
        scores = network(target)
        if model == "B":
            return torch.stack((scores[:, 0] / 100.0, scores[:, 1].sigmoid()), dim=1)
        prior = (torch.bincount(tail, minlength=3).float() + 1) / (len(train) + 3)
        weight = len(train) / (len(train) + 60)
        return weight * scores.softmax(dim=1) + (1 - weight) * prior


def _predict_fold(model, train, test, columns):
    if model == "A":
        output = _predict_a(train, test, columns)
        return output, np.column_stack(
            (
                np.zeros(len(test)),
                np.full(len(test), (train.actual_return.gt(0).sum() + 1) / (len(train) + 2)),
            )
        )
    import torch

    output = _cuda_predict(model, train, test, columns)
    y = torch.as_tensor(train.actual_return.to_numpy(), dtype=torch.float32, device="cuda")
    if model == "B":
        prior = torch.stack((y.new_zeros(()), (y.gt(0).sum() + 1) / (len(train) + 2)))
    else:
        classes = torch.as_tensor(extreme_labels(train.actual_return), device="cuda")
        prior = (torch.bincount(classes, minlength=3).float() + 1) / (len(train) + 3)
    return output, prior.expand(len(test), -1)


def _gpu_ap(scores, events):
    """Average precision on CUDA using the last rank of each equal-score group."""
    import torch

    if not events.any():
        return None
    sorted_scores, order = scores.sort(descending=True)
    actual = events[order].float()
    group_ends = torch.cat(
        (
            sorted_scores[:-1] != sorted_scores[1:],
            torch.ones(1, dtype=torch.bool, device=scores.device),
        )
    )
    positives = actual.cumsum(dim=0)[group_ends]
    ranks = torch.arange(1, len(scores) + 1, device=scores.device)[group_ends]
    increments = torch.diff(positives, prepend=positives.new_zeros(1))
    return float((increments / positives[-1] * positives / ranks).sum().item())


def _metrics(model, output, rows):
    y = rows.actual_return.to_numpy()
    close = rows.close.to_numpy()
    if model == "A":
        return {
            "close_mae_points": float(np.mean(np.abs(output[:, 0] - y) * close)),
            "direction_hit": float(np.mean((output[:, 1] > 0.5) == (y > 0))),
            "direction_brier": float(np.mean(np.square(output[:, 1] - (y > 0)))),
        }
    import torch

    realised = torch.as_tensor(y, dtype=torch.float32, device="cuda")
    if model == "B":
        anchor = torch.as_tensor(close, dtype=torch.float32, device="cuda")
        return {
            "close_mae_points": float(((output[:, 0] - realised).abs() * anchor).mean().item()),
            "direction_hit": float(((output[:, 1] > 0.5) == realised.gt(0)).float().mean().item()),
            "direction_brier": float(
                (output[:, 1] - realised.gt(0).float()).square().mean().item()
            ),
        }
    classes = torch.as_tensor(extreme_labels(y), device="cuda")
    target = torch.nn.functional.one_hot(classes, 3).float()
    event = classes.ne(1)
    p_event = output[:, 0] + output[:, 2]
    return {
        "three_way_brier": float((output - target).square().sum(dim=1).mean().item()),
        "event_brier": float((p_event - event.float()).square().mean().item()),
        "event_average_precision": _gpu_ap(p_event, event),
        "down_average_precision": _gpu_ap(output[:, 0], classes.eq(0)),
        "up_average_precision": _gpu_ap(output[:, 2], classes.eq(2)),
        "counts": dict(zip(CLASSES, torch.bincount(classes, minlength=3).tolist(), strict=True)),
    }


def _array(series):
    return series if isinstance(series, np.ndarray) else series.detach().cpu().numpy()


def _walk(model, rows, base_columns, added_columns, *, minimum_train, refit_sessions):
    chunks = []
    for train_idx, test_idx in chronological_splits(
        rows, minimum_train=minimum_train, refit_sessions=refit_sessions
    ):
        train, test = rows.iloc[train_idx], rows.iloc[test_idx]
        raw, prior = _predict_fold(model, train, test, list(base_columns))
        candidate = None
        if added_columns:
            candidate, _ = _predict_fold(model, train, test, [*base_columns, *added_columns])
        chunks.append((test, raw, prior, candidate, len(train)))
    if not chunks:
        return {
            "status": "insufficient_history",
            "eligible_rows": len(rows),
            "scored_rows": 0,
        }, pd.DataFrame()
    forecasts = pd.concat([chunk[0] for chunk in chunks]).reset_index(drop=True)
    if model == "A":
        stack = np.concatenate
    else:
        import torch

        stack = torch.cat
    assembled = {
        "no_cfd": stack([chunk[1] for chunk in chunks]),
        "prior": stack([chunk[2] for chunk in chunks]),
    }
    if added_columns:
        assembled["with_cfd"] = stack([chunk[3] for chunk in chunks])
    main = {key: _metrics(model, value, forecasts) for key, value in assembled.items()}
    recent_count = min(30, len(forecasts))
    recent = {
        key: _metrics(model, value[-recent_count:], forecasts.tail(recent_count))
        for key, value in assembled.items()
    }
    oof_fields = [
        "asof_date",
        "target_date",
        "cutoff_at",
        "close",
        "actual_return",
        "cfd_observation",
    ]
    oof = forecasts[oof_fields].copy()
    labels = CLASSES if model == "C" else ("return", "p_up")
    for key, predictions in assembled.items():
        for i, label in enumerate(labels):
            oof[f"{key}_{label}"] = _array(predictions)[:, i]
    summary = {
        "status": "research_only" if len(forecasts) >= 20 else "very_small_sample",
        "scored_rows": len(forecasts),
        "eligible_rows": len(rows),
        "first_target": str(forecasts.target_date.iloc[0]),
        "last_target": str(forecasts.target_date.iloc[-1]),
        "folds": len(chunks),
        "first_fold_train_rows": chunks[0][-1],
        "last_fold_train_rows": chunks[-1][-1],
        "evaluation_device": "CPU" if model == "A" else "CUDA",
        "metrics": main,
        "last_30_or_available": recent,
    }
    return summary, oof


def _eligible(rows, columns):
    complete = np.isfinite(rows[list(columns)].to_numpy(dtype=float)).all(axis=1)
    return rows.loc[complete & rows.actual_return.notna()].reset_index(drop=True)


def _snapshot(path):
    raw = path.read_bytes()
    rows = pd.read_csv(BytesIO(raw), dtype={"asof_date": str, "target_date": str})
    if rows.duplicated(["market", "stage", "target_date"]).any():
        raise ValueError("duplicate forecast target in input snapshot")
    rows["cutoff_at"] = _clock(rows.cutoff_at)
    rows["label_at"] = _clock(rows.target_close_at)
    if "target_received_at" in rows:
        arrival = _clock(rows.target_received_at)
        same_day = arrival.dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d").eq(rows.target_date)
        rows["label_at"] = rows.label_at.mask(same_day & arrival.ge(rows.label_at), arrival)
    labeled = rows.actual_return.notna()
    if not (rows.loc[labeled, "label_at"] > rows.loc[labeled, "cutoff_at"]).all():
        raise ValueError("resolved labels must follow their own forecast cutoff")
    return rows, hashlib.sha256(raw).hexdigest()


def _visuals(output, balances, evaluations, marketwide):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    if not balances.empty:
        figure, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True, layout="constrained")
        for ax, (market, series) in zip(axes, balances.groupby("market", sort=True), strict=True):
            ax.plot(
                pd.to_datetime(series.observation_date),
                series.value_krw / 1e8,
                color="#2563eb",
                linewidth=1.7,
            )
            ax.set_title(market)
            ax.set_ylabel("KRW 100m")
            ax.grid(alpha=0.2)
        figure.suptitle("KOFIA disclosed domestic CFD balance")
        figure.autofmt_xdate()
        figure.savefig(output / "cfd-market-balances.png", dpi=150)
        plt.close(figure)

    if marketwide:
        figure, ax = plt.subplots(figsize=(10, 4), layout="constrained")
        for metric, label in (("with_margin", "With margin"), ("without_margin", "Less margin")):
            data = pd.DataFrame(marketwide["series"][metric])
            ax.plot(pd.to_datetime(data.date), data.total_krw / 1e12, label=label)
        ax.set(title="FreeSIS CFD notional balance · all reported markets", ylabel="KRW trillion")
        ax.legend()
        ax.grid(alpha=0.2)
        figure.savefig(output / "frees-is-cfd-balances.png", dpi=150)
        plt.close(figure)

    figure, axes = plt.subplots(1, 3, figsize=(11, 3.8), layout="constrained")
    for ax, model in zip(axes, STAGE, strict=True):
        markets, baseline, challenger = [], [], []
        metric = "three_way_brier" if model == "C" else "direction_brier"
        for market in ("KOSPI", "KOSDAQ"):
            comparison = "matched_freesis" if marketwide else "matched_cfd"
            records = evaluations[model][market][comparison].get("metrics")
            if records:
                markets.append(market)
                baseline.append(records["no_cfd"][metric])
                challenger.append(records["with_cfd"][metric])
        if markets:
            positions = np.arange(len(markets))
            ax.bar(positions - 0.175, baseline, 0.35, label="Without CFD", color="#64748b")
            ax.bar(positions + 0.175, challenger, 0.35, label="With CFD", color="#2563eb")
            ax.set_xticks(positions, markets)
            ax.legend(fontsize=8, loc="best")
        else:
            ax.text(
                0.5,
                0.5,
                "Insufficient matched history",
                ha="center",
                va="center",
                transform=ax.transAxes,
            )
            ax.set_xticks([])
        ax.set_title(f"Model {model}")
        ax.set_ylabel("Brier loss")
        ax.grid(axis="y", alpha=0.2)
    source_label = "FreeSIS" if marketwide else "KOFIA PDFs"
    figure.suptitle(f"Retrospective matched forecast comparison · {source_label}")
    figure.savefig(output / "abc-cfd-backtest.png", dpi=150)
    plt.close(figure)


def run(inputs, output, cfd=None, prices=None, frees_is=None):
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("Models B and C require CUDA for this local experiment")
    torch.set_num_threads(1)
    knowledge_at = datetime.now(UTC)
    raw, input_hash = _snapshot(inputs)
    cfd_bytes = cfd.read_bytes() if cfd else None
    price_bytes = prices.read_bytes() if prices else None
    free_bytes = frees_is.read_bytes() if frees_is else None
    marketwide = json.loads(free_bytes) if free_bytes else None
    balances = (
        pd.read_csv(BytesIO(cfd_bytes), dtype={"observation_date": str})
        if cfd_bytes
        else pd.DataFrame()
    )
    features = cfd_features(raw, balances, timing="retrospective", knowledge_at=knowledge_at)
    observed = cfd_features(raw, balances, timing="observed", knowledge_at=knowledge_at)
    if price_bytes:
        features = price_features(features, json.loads(price_bytes))
    free_columns = ()
    if marketwide:
        features, free_columns = freesis_features(features, marketwide, knowledge_at=knowledge_at)
    output.mkdir(parents=True, exist_ok=True)
    evaluations = {model: {} for model in STAGE}
    for model, stage in STAGE.items():
        for market in ("KOSPI", "KOSDAQ"):
            rows = features.loc[
                (features.market == market) & (features.stage == stage)
            ].sort_values("target_date")
            base = (*CORE_COLUMNS, *US_COLUMNS)
            eligible = _eligible(rows, base)
            broad, broad_oof = _walk(
                model, eligible, base, (), minimum_train=504, refit_sessions=60
            )
            cfd_columns = (
                (*CFD_COLUMNS, "feature_cfd_change20")
                if len(_eligible(rows, [*base, *CFD_COLUMNS, "feature_cfd_change20"])) >= 60
                else CFD_COLUMNS
            )
            matched = _eligible(rows, [*base, *cfd_columns])
            matched_review, paired_oof = _walk(
                model, matched, base, cfd_columns, minimum_train=24, refit_sessions=10
            )
            marketwide_review = None
            if free_columns:
                matched_free = _eligible(rows, [*base, *free_columns])
                marketwide_review, free_oof = _walk(
                    model,
                    matched_free,
                    base,
                    free_columns,
                    minimum_train=252,
                    refit_sessions=30,
                )
                free_oof.to_csv(
                    output / f"{model.lower()}-{market.lower()}-freesis-oof.csv", index=False
                )
            recent_prices = None
            if "feature_skhy_chart_return" in rows:
                skhy = ("feature_skhy_chart_return",)
                skhy_rows = _eligible(rows, [*base, *skhy])
                recent_prices, _ = _walk(
                    model, skhy_rows, base, skhy, minimum_train=45, refit_sessions=15
                )
                if "metrics" in recent_prices:
                    recent_prices["metrics"]["with_skhy"] = recent_prices["metrics"].pop("with_cfd")
            stem = f"{model.lower()}-{market.lower()}"
            broad_oof.to_csv(output / f"{stem}-baseline-oof.csv", index=False)
            paired_oof.to_csv(output / f"{stem}-cfd-oof.csv", index=False)
            actual_recent = observed.loc[(observed.market == market) & (observed.stage == stage)]
            evaluations[model][market] = {
                "stage": stage,
                "full_no_cfd": broad,
                "matched_cfd": matched_review,
                "matched_skhy": recent_prices,
                "matched_freesis": marketwide_review,
                "cfd_features": cfd_columns,
                "historical_rows_with_actual_cfd_receipt": int(
                    actual_recent.loc[actual_recent.actual_return.notna(), "cfd_observation"]
                    .notna()
                    .sum()
                ),
            }
            free_count = marketwide_review["scored_rows"] if marketwide_review else 0
            print(
                f"{model} {market}: {broad['scored_rows']} baseline, "
                f"{matched_review['scored_rows']} PDF and {free_count} FreeSIS CFD targets",
                flush=True,
            )
    report = {
        "generated_at": knowledge_at.isoformat(),
        "production_weight": 0,
        "input_sha256": input_hash,
        "cfd_input_sha256": hashlib.sha256(cfd_bytes).hexdigest() if cfd_bytes else None,
        "freesis_input_sha256": hashlib.sha256(free_bytes).hexdigest() if free_bytes else None,
        "freesis_feature_columns": free_columns,
        "chart_price_sha256": hashlib.sha256(price_bytes).hexdigest() if price_bytes else None,
        "gpu": torch.cuda.get_device_name(0),
        "torch": str(torch.__version__),
        "models": {
            "A": "existing linear fit family, CPU",
            "B": "local CUDA MLP return and direction",
            "C": "local CUDA MLP all-session three-way 3 percent",
        },
        "commissioning": "Counterfactual historical replay. Past release timestamp unavailable; CFD enters at midnight KST after its posted date, and uses archived-current prices. No independent live claims.",
        "results": evaluations,
    }
    _visuals(output, balances, evaluations, marketwide)
    (output / "summary.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--cfd", type=Path)
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--frees-is", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.inputs, args.output, args.cfd, args.prices, args.frees_is)


if __name__ == "__main__":
    main()
