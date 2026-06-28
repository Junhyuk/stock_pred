from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SequenceArrays:
    x: np.ndarray
    y: np.ndarray
    meta: pd.DataFrame
    feature_columns: list[str]
    fill_values: dict[str, float]
    scale_values: dict[str, float]


def prepare_sequence_frame(
    dataset: pd.DataFrame,
    horizon: str,
    feature_columns: list[str],
    label_column: str = "is_top20pct",
) -> pd.DataFrame:
    if dataset.empty:
        return pd.DataFrame()
    frame = dataset[dataset["horizon"] == horizon].copy()
    if frame.empty:
        return frame
    missing = {"date", "symbol", "horizon", label_column}.difference(frame.columns)
    if missing:
        raise ValueError(f"sequence dataset is missing columns: {sorted(missing)}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    usable_features = [column for column in feature_columns if column in frame.columns]
    if not usable_features:
        raise ValueError("No usable feature columns were found for sequence dataset")
    for column in usable_features:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame[label_column] = pd.to_numeric(frame[label_column], errors="coerce")
    frame = frame.replace([np.inf, -np.inf], np.nan)
    return frame.dropna(subset=["date", "symbol", label_column]).sort_values(["symbol", "date"]).reset_index(drop=True)


def chronological_split_ranges(
    frame: pd.DataFrame,
    split_config: dict[str, Any] | None = None,
) -> dict[str, dict[str, pd.Timestamp]]:
    if frame.empty:
        return {}
    split_config = split_config or {}
    dates = pd.Series(pd.to_datetime(frame["date"]).dropna().unique()).sort_values().reset_index(drop=True)
    if dates.empty:
        return {}

    explicit = {
        key: split_config.get(key)
        for key in (
            "train_start",
            "train_end",
            "valid_start",
            "valid_end",
            "test_start",
            "test_end",
        )
    }
    if any(value is not None for value in explicit.values()):
        return {
            "train": _range(explicit["train_start"] or dates.iloc[0], explicit["train_end"] or dates.iloc[-1]),
            "valid": _range(explicit["valid_start"] or explicit["train_end"] or dates.iloc[0], explicit["valid_end"] or dates.iloc[-1]),
            "test": _range(explicit["test_start"] or explicit["valid_end"] or dates.iloc[0], explicit["test_end"] or dates.iloc[-1]),
        }

    train_ratio = float(split_config.get("train_ratio", 0.70))
    valid_ratio = float(split_config.get("valid_ratio", 0.15))
    n_dates = len(dates)
    train_end_idx = max(0, min(n_dates - 1, int(n_dates * train_ratio) - 1))
    valid_end_idx = max(train_end_idx, min(n_dates - 1, int(n_dates * (train_ratio + valid_ratio)) - 1))
    ranges = {
        "train": _range(dates.iloc[0], dates.iloc[train_end_idx]),
        "valid": _range(dates.iloc[min(train_end_idx + 1, n_dates - 1)], dates.iloc[valid_end_idx]),
        "test": _range(dates.iloc[min(valid_end_idx + 1, n_dates - 1)], dates.iloc[-1]),
    }
    return ranges


def fit_sequence_normalizer(
    frame: pd.DataFrame,
    feature_columns: list[str],
    start_date,
    end_date,
) -> tuple[dict[str, float], dict[str, float]]:
    window = _date_slice(frame, start_date, end_date)
    values = window[feature_columns].replace([np.inf, -np.inf], np.nan)
    fill_values = values.median(numeric_only=True).fillna(0.0)
    scale_values = values.std(numeric_only=True, ddof=0).replace(0.0, np.nan).fillna(1.0)
    return fill_values.to_dict(), scale_values.to_dict()


def build_sequence_arrays(
    frame: pd.DataFrame,
    feature_columns: list[str],
    lookback: int,
    start_date=None,
    end_date=None,
    fill_values: dict[str, float] | None = None,
    scale_values: dict[str, float] | None = None,
    label_column: str = "is_top20pct",
) -> SequenceArrays:
    if frame.empty:
        return _empty(feature_columns, fill_values, scale_values, lookback)

    usable_features = [column for column in feature_columns if column in frame.columns]
    fill_values = fill_values or {column: 0.0 for column in usable_features}
    scale_values = scale_values or {column: 1.0 for column in usable_features}
    start = pd.Timestamp(start_date) if start_date is not None else pd.Timestamp(frame["date"].min())
    end = pd.Timestamp(end_date) if end_date is not None else pd.Timestamp(frame["date"].max())

    x_values: list[np.ndarray] = []
    y_values: list[float] = []
    meta_rows: list[dict[str, object]] = []

    for (symbol, horizon), group in frame.sort_values(["symbol", "date"]).groupby(["symbol", "horizon"]):
        group = group.reset_index(drop=True)
        features = group[usable_features].copy()
        for column in usable_features:
            features[column] = pd.to_numeric(features[column], errors="coerce").fillna(fill_values.get(column, 0.0))
            features[column] = (features[column] - fill_values.get(column, 0.0)) / scale_values.get(column, 1.0)
        matrix = features.to_numpy(dtype=np.float32)
        dates = pd.to_datetime(group["date"])
        labels = pd.to_numeric(group[label_column], errors="coerce").to_numpy(dtype=np.float32)

        for idx in range(int(lookback) - 1, len(group)):
            asof_date = pd.Timestamp(dates.iloc[idx])
            if asof_date < start or asof_date > end or np.isnan(labels[idx]):
                continue
            sequence = matrix[idx - int(lookback) + 1 : idx + 1]
            if sequence.shape != (int(lookback), len(usable_features)):
                continue
            x_values.append(sequence)
            y_values.append(float(labels[idx]))
            meta_rows.append(
                {
                    "date": asof_date.date(),
                    "symbol": symbol,
                    "horizon": horizon,
                    "label": float(labels[idx]),
                    "sequence_start": pd.Timestamp(dates.iloc[idx - int(lookback) + 1]).date(),
                    "sequence_end": asof_date.date(),
                }
            )

    if not x_values:
        return _empty(usable_features, fill_values, scale_values, lookback)
    return SequenceArrays(
        x=np.stack(x_values).astype(np.float32),
        y=np.asarray(y_values, dtype=np.float32),
        meta=pd.DataFrame(meta_rows),
        feature_columns=usable_features,
        fill_values={column: float(fill_values.get(column, 0.0)) for column in usable_features},
        scale_values={column: float(scale_values.get(column, 1.0)) for column in usable_features},
    )


def _date_slice(frame: pd.DataFrame, start_date, end_date) -> pd.DataFrame:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    dates = pd.to_datetime(frame["date"])
    return frame[(dates >= start) & (dates <= end)].copy()


def _range(start, end) -> dict[str, pd.Timestamp]:
    return {"start": pd.Timestamp(start), "end": pd.Timestamp(end)}


def _empty(
    feature_columns: list[str],
    fill_values: dict[str, float] | None,
    scale_values: dict[str, float] | None,
    lookback: int,
) -> SequenceArrays:
    return SequenceArrays(
        x=np.empty((0, int(lookback), len(feature_columns)), dtype=np.float32),
        y=np.empty((0,), dtype=np.float32),
        meta=pd.DataFrame(columns=["date", "symbol", "horizon", "label", "sequence_start", "sequence_end"]),
        feature_columns=feature_columns,
        fill_values=fill_values or {column: 0.0 for column in feature_columns},
        scale_values=scale_values or {column: 1.0 for column in feature_columns},
    )
