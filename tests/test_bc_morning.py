from datetime import UTC

import numpy as np
import pandas as pd
import pytest

from roboquant.index_forecast.bc_morning import (
    BASE_SYMBOLS,
    forecast_morning_market,
    us_return_features,
)
from roboquant.index_forecast.us_market_inputs import SCHEMA_VERSION, us_close_schedule


def observations_for(dates, symbols, *, receipt=None):
    timeline = us_close_schedule(dates[0], dates[-1])
    timeline = timeline.loc[timeline.symbol.eq("^GSPC")].reset_index(drop=True)
    rows = []
    for symbol in symbols:
        for i, row in enumerate(timeline.itertuples()):
            if row.session_date not in dates:
                continue
            close_at = pd.Timestamp(row.session_close_at)
            available_at = close_at + np.timedelta64(15, "m")
            rows.append(
                dict(
                    schema_version=SCHEMA_VERSION,
                    symbol=symbol,
                    session_date=row.session_date,
                    close=100 + i * 2 + np.sin(i),
                    session_close_at=close_at.isoformat(),
                    available_at=available_at.isoformat(),
                    first_seen_at=(receipt or available_at).isoformat(),
                    revision=0,
                    is_final=True,
                )
            )
    return pd.DataFrame(rows)


def test_morning_exact_session_and_actual_receipts():
    cutoffs = pd.DatetimeIndex(["2026-11-23T23:00Z", "2026-11-24T23:00Z"])
    observations = observations_for(["2026-11-20", "2026-11-23", "2026-11-24"], BASE_SYMBOLS)
    features = us_return_features(observations, cutoffs, BASE_SYMBOLS)
    symbol = observations.loc[observations.symbol.eq("^GSPC"), "close"].to_numpy()
    assert features[:, 0] == pytest.approx(symbol[1:] / symbol[:-1] - 1)
    late = observations.session_date.eq("2026-11-24")
    observations.loc[late, "first_seen_at"] = "2026-11-24T23:01:00Z"
    features = us_return_features(observations, cutoffs, BASE_SYMBOLS)
    assert np.isfinite(features[0]).all()
    assert not np.isfinite(features[-1]).any()


def test_initial_reconstruction_does_not_share_future_revision_with_history():
    cutoffs = pd.DatetimeIndex(["2026-11-23T23:00Z", "2026-11-24T23:00Z"])
    initial = observations_for(["2026-11-20", "2026-11-23", "2026-11-24"], BASE_SYMBOLS)
    version = initial.loc[initial.session_date.eq("2026-11-23")].assign(
        revision=1, first_seen_at="2026-11-24T23:00:00Z", close=200.0
    )
    observed = pd.concat((initial, version), ignore_index=True)
    base = us_return_features(initial, cutoffs, BASE_SYMBOLS)
    revised = us_return_features(observed, cutoffs, BASE_SYMBOLS)
    np.testing.assert_array_equal(base[0], revised[0])
    assert not np.array_equal(base[-1], revised[-1])


def test_holiday_with_no_new_us_close_uses_exact_latest_completed_session():
    observations = observations_for(["2026-11-24", "2026-11-25"], BASE_SYMBOLS)
    cutoffs = pd.DatetimeIndex(["2026-11-25T23:00Z", "2026-11-26T23:00Z"])
    features = us_return_features(observations, cutoffs, BASE_SYMBOLS)
    assert np.isfinite(features).all()
    np.testing.assert_array_equal(features[0], features[1])


@pytest.fixture(scope="module")
def market_history():
    # Use the exchange's KRX dates, leaving 60+ out-of-fold points beyond warmup.
    from roboquant.index_forecast.reliable_inputs import session_schedule

    sessions, _ = session_schedule("2024-04-01", "2026-11-26")
    target = sessions.iloc[-1]
    sessions = sessions.iloc[:-1]
    dates = (
        us_close_schedule("2024-03-01", "2026-11-25")
        .loc[lambda table: table.symbol.eq("^GSPC"), "session_date"]
        .tolist()
    )
    observations = observations_for(dates, (*BASE_SYMBOLS, "KORU"))
    positions = np.arange(len(sessions))
    returns = 0.002 + 0.007 * np.sin(positions / 3) + 0.004 * np.cos(positions / 2)
    closes = 1000 * np.cumprod(1 + returns)
    anchor = sessions.iloc[-1]
    return closes, {
        "sessions": sessions,
        "target": target.date,
        "observations": observations,
        "prediction_cutoff": pd.Timestamp("2026-11-25T23:00:00Z", tz=UTC),
        "close_received_at": {anchor.date: anchor.close_at + np.timedelta64(1, "h")},
    }


def test_forecast_is_a_fitted_price_with_a_calibrated_bounded_range(market_history):
    closes, params = market_history
    result = forecast_morning_market("KOSDAQ", closes, **params)
    assert result["close"] == closes[-1]
    assert result["expected_index"] == pytest.approx(closes[-1] * (1 + result["expected_return"]))
    assert result["expected_index"] != pytest.approx(closes[-1])
    assert 0 < result["p_up"] < 1
    assert result["p_up"] + result["p_down"] == pytest.approx(1)
    assert result["interval"]["level"] == 0.8
    assert result["interval"]["low"] < result["expected_index"] < result["interval"]["high"]


def test_koru_is_required_for_kospi_but_does_not_change_kosdaq(market_history):
    closes, params = market_history
    observations = params["observations"].loc[lambda rows: rows.symbol.ne("KORU")]
    params = {**params, "observations": observations}
    assert forecast_morning_market("KOSDAQ", closes, **params)["expected_index"] > 0
    with pytest.raises(ValueError, match="KORU"):
        forecast_morning_market("KOSPI", closes, **params)


def test_rejects_missing_anchor_receipt(market_history):
    closes, params = market_history
    with pytest.raises(ValueError, match="anchor receipt"):
        forecast_morning_market("KOSPI", closes, **{**params, "close_received_at": {}})


def test_later_duplicate_initial_snapshot_does_not_rewrite_history():
    cutoffs = pd.DatetimeIndex(["2026-11-23T23:00Z", "2026-11-24T23:00Z"])
    initial = observations_for(["2026-11-20", "2026-11-23", "2026-11-24"], BASE_SYMBOLS)
    duplicate = initial.loc[initial.session_date.eq("2026-11-23")].assign(
        first_seen_at="2026-11-24T22:00:00Z", close=200.0
    )
    changed = us_return_features(pd.concat((initial, duplicate)), cutoffs, BASE_SYMBOLS)
    np.testing.assert_array_equal(us_return_features(initial, cutoffs, BASE_SYMBOLS), changed)


def test_morning_model_rejects_post_open_cutoff(market_history):
    closes, params = market_history
    cutoff = pd.Timestamp("2026-11-26T00:00:00Z")
    with pytest.raises(ValueError, match="target open"):
        forecast_morning_market("KOSDAQ", closes, **{**params, "prediction_cutoff": cutoff})
