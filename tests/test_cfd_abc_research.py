from datetime import UTC, datetime

import numpy as np
import pandas as pd

from scripts.run_cfd_abc_research import cfd_features, chronological_splits, extreme_labels


def test_model_c_classifies_exact_thresholds_and_ordinary_days():
    assert extreme_labels(np.array([-0.031, -0.03, -0.02, 0.0, 0.029, 0.03, 0.031])).tolist() == [
        0,
        0,
        1,
        1,
        1,
        2,
        2,
    ]


def test_split_never_trains_on_unresolved_labels():
    dates = pd.date_range("2026-08-01", periods=9, tz="UTC")
    rows = pd.DataFrame({"cutoff_at": dates, "label_at": dates + pd.Timedelta(days=2)})
    splits = list(chronological_splits(rows, minimum_train=3, refit_sessions=2))
    assert splits
    for train, test in splits:
        assert max(train) < min(test)
        assert (rows.label_at.iloc[train] <= rows.cutoff_at.iloc[test[0]]).all()


def test_cfd_cutoff_uses_release_timestamp_and_never_replaces_a_missing_comparison_session():
    obs = pd.DataFrame(
        {
            "market": ["KOSPI"] * 3,
            "observation_date": ["2026-11-19", "2026-11-20", "2026-11-24"],
            "retrospective_available_at": [
                "2026-11-20T15:00Z",
                "2026-11-23T15:00Z",
                "2026-11-25T15:00Z",
            ],
            "received_at": ["2026-11-25T08:00Z"] * 3,
            "value_krw": [100, 110, 130],
            "valid_count": [1, 2, 3],
        }
    )
    base = pd.DataFrame(
        {
            "market": ["KOSPI"] * 3,
            "asof_date": ["2026-11-20", "2026-11-24", "2026-11-25"],
            "cutoff_at": pd.to_datetime(
                ["2026-11-20T09:00Z", "2026-11-24T09:00Z", "2026-11-25T23:00Z"]
            ),
        }
    )
    frame = cfd_features(
        base, obs, timing="retrospective", knowledge_at=datetime(2026, 11, 26, tzinfo=UTC)
    )
    assert frame.cfd_observation.tolist()[1:] == ["2026-11-20", "2026-11-24"]
    assert pd.isna(frame.cfd_observation.iloc[0])
    assert np.isclose(frame.feature_cfd_change1.iloc[1], 0.1)
    assert pd.isna(frame.feature_cfd_change1.iloc[2])
    observed = cfd_features(
        base.iloc[:2], obs, timing="observed", knowledge_at=datetime(2026, 11, 26, tzinfo=UTC)
    )
    assert observed.cfd_observation.isna().all()
