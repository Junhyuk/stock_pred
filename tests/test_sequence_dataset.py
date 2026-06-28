from __future__ import annotations

import pandas as pd

from roboquant.datasets.sequence_dataset import (
    build_sequence_arrays,
    chronological_split_ranges,
    fit_sequence_normalizer,
    prepare_sequence_frame,
)


def test_sequence_dataset_uses_only_asof_and_past_rows() -> None:
    dates = pd.date_range("2024-01-01", periods=8, freq="B")
    dataset = pd.DataFrame(
        {
            "date": list(dates.date) * 2,
            "symbol": ["000001"] * 8 + ["000002"] * 8,
            "horizon": ["3M"] * 16,
            "momentum_score": list(range(8)) + list(range(10, 18)),
            "risk_score": [0.1] * 16,
            "is_top20pct": [0, 1] * 8,
            "excess_return": [0.01] * 16,
        }
    )
    frame = prepare_sequence_frame(dataset, "3M", ["momentum_score", "risk_score"])
    fill, scale = fit_sequence_normalizer(frame, ["momentum_score", "risk_score"], dates[0], dates[4])
    arrays = build_sequence_arrays(
        frame,
        ["momentum_score", "risk_score"],
        lookback=3,
        start_date=dates[3],
        end_date=dates[3],
        fill_values=fill,
        scale_values=scale,
    )

    assert len(arrays.y) == 2
    assert set(arrays.meta["sequence_end"]) == {dates[3].date()}
    assert set(arrays.meta["sequence_start"]) == {dates[1].date()}
    assert (pd.to_datetime(arrays.meta["sequence_start"]) <= pd.to_datetime(arrays.meta["date"])).all()
    assert (pd.to_datetime(arrays.meta["sequence_end"]) <= pd.to_datetime(arrays.meta["date"])).all()


def test_chronological_split_preserves_date_order() -> None:
    dates = pd.date_range("2024-01-01", periods=10, freq="B")
    frame = pd.DataFrame({"date": dates, "symbol": ["000001"] * 10, "horizon": ["3M"] * 10})

    ranges = chronological_split_ranges(frame, {"train_ratio": 0.6, "valid_ratio": 0.2})

    assert ranges["train"]["end"] < ranges["valid"]["start"]
    assert ranges["valid"]["end"] < ranges["test"]["start"]
