from datetime import date

from scripts.publish_bc_ranges import Row, b_band, c_band, calibrate, event_class, surge_context


def test_b_range_uses_finite_sample_error_quantile():
    assert b_band(0.01, [0.01, 0.02, 0.03, 0.04]) == (-0.03, 0.05)


def test_c_probability_mass_selects_the_expected_tail():
    past = [-0.05, -0.04, -0.01, 0.01, 0.04, 0.05]
    low, high = c_band(past, (0.92, 0.04, 0.04))
    assert (low, high) == (-0.05, -0.04)
    assert [event_class(x) for x in [-0.03, 0, 0.03]] == [0, 1, 2]


def test_calibration_ignores_unmatured_returns():
    forecast = date(2025, 12, 30)
    rows = [
        Row(date(2025, 12, 29), forecast, 0.02, 0.01, (0.1, 0.8, 0.1)),
        Row(forecast, date(2026, 1, 2), 0.99, 0.0, (0.1, 0.8, 0.1)),
    ]
    state, _ = calibrate(rows, forecast, minimum=1)
    assert state.b_errors == [0.01]
    assert state.returns == [0.02]



def test_surge_context_counts_only_matured_consecutive_matches():
    probs = (0.1, 0.8, 0.1)

    def row(origin, target, actual):
        return Row(date(2026, 11, origin), date(2026, 11, target), actual, 0.0, probs)

    rows = [
        row(9, 10, 0.03),
        row(10, 11, -0.04),
        row(11, 12, 0.05),
        row(13, 16, 0.05),  # Missing 12→13 follow-up.
        row(16, 17, 0.033),
        row(23, 24, 0.0632),
        row(24, 25, -0.08),  # Target has not closed at the forecast cutoff.
    ]
    assert surge_context(rows, date(2026, 11, 24)) == (0.0632, (1, 0, 1))
    assert surge_context(rows, date(2026, 11, 11)) is None
