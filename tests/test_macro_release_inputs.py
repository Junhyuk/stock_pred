import importlib.util

import pandas as pd
import pytest


def prepare(rows):
    assert importlib.util.find_spec("roboquant.index_forecast.macro_release_inputs")
    from roboquant.index_forecast.macro_release_inputs import prepare_macro_releases

    return prepare_macro_releases(pd.DataFrame(rows), as_of=pd.Timestamp("2026-11-19T13:00:00Z"))


def release(period, day, value, **overrides):
    return {
        "country": "US", "measure": "industrial_production_mom", "reference_period": period,
        "value": value, "unit": "percent", "seasonal_adjustment": "SA", "vintage": "first_release",
        "source_id": "fed_g17_initial", "source_url": "https://www.federalreserve.gov/releases/g17/revhist.htm",
        "release_date": day, "actual_release_at": None, "received_at": "2026-11-19T12:38:00Z",
        **overrides,
    }


def test_date_only_first_release_uses_end_of_source_day_and_actual_receipt():
    model = prepare([release("2026-09", "2026-10-16", .6626), release("2026-10", "2026-11-17", -.4337)])
    family, = model.families
    assert model.features_at(family, pd.Timestamp("2026-11-18T04:59:59Z"), prospective=False) is None
    historical = model.features_at(family, pd.Timestamp("2026-11-18T05:00:00Z"), prospective=False)
    assert historical["latest_value"] == -.4337
    assert historical["features"]["release_change_pp"] == pytest.approx(-1.0963)
    assert model.features_at(family, pd.Timestamp("2026-11-19T09:13:32Z"), prospective=True) is None
    live = model.features_at(family, pd.Timestamp("2026-11-19T12:39:00Z"), prospective=True)
    assert live["snapshot_received_at"] == "2026-11-19T12:38:00+00:00"
    assert live["reference_periods"] == ["2026-09", "2026-10"]


def test_revisions_unapproved_sources_and_conflicting_values_do_not_enter():
    model = prepare([
        release("2026-08", "2026-09-18", 1),
        release("2026-09", "2026-10-16", 2, vintage="current_snapshot"),
        release("2026-09", "2026-10-16", 3, source_url="https://not-federalreserve.gov/g17"),
        release("2026-10", "2026-11-17", 2), release("2026-10", "2026-11-17", 3),
    ])
    assert model.evidence["rejections"] == {
        "non_first_release": 1, "unapproved_source_or_definition": 1, "conflicting_period": 1,
    }
    family, = model.families
    assert model.features_at(family, pd.Timestamp("2026-11-19T13:00:00Z"), prospective=True) is None


def test_same_day_catch_up_release_uses_latest_reference_period():
    rows = [
        release("2025-10", "2025-12-23", .1, actual_release_at="2025-12-23T09:15:00-05:00"),
        release("2025-11", "2025-12-23", .2, actual_release_at="2025-12-23T09:15:00-05:00"),
    ]
    model = prepare(rows)
    family, = model.families
    report = model.features_at(family, pd.Timestamp("2025-12-23T14:15:00Z"), prospective=False)
    assert report["latest_reference_period"] == "2025-11"
    assert report["features"]["release_level"] == .2
