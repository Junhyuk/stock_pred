from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from roboquant.index_forecast import reliable_inputs as inputs


def test_context_separates_friday_issue_from_monday_target():
    sessions = pd.to_datetime(["2026-10-22", "2026-10-23", "2026-10-26"])
    now = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    result = inputs.resolve_forecast_context("2026-10-23", now=now, sessions=sessions)
    assert result["target_date"] == "2026-10-26"
    assert result["asof_date"] == "2026-10-23"
    assert pd.Timestamp(result["prediction_cutoff"]) <= pd.Timestamp(now)
    assert result["stage"] == "preliminary"
    assert result["status"] == "ready"


def test_target_session_snapshot_cutoff_and_open_issuance_deadline():
    sessions = pd.to_datetime(["2026-10-23", "2026-10-26", "2026-10-27"])
    before = inputs.resolve_forecast_context("2026-10-23", now=datetime(2026, 10, 25, 22, 59, tzinfo=UTC), sessions=sessions)
    boundary = inputs.resolve_forecast_context("2026-10-23", now=datetime(2026, 10, 25, 23, tzinfo=UTC), sessions=sessions)
    final = inputs.resolve_forecast_context("2026-10-23", now=datetime(2026, 10, 25, 23, 30, tzinfo=UTC), sessions=sessions)
    late = inputs.resolve_forecast_context("2026-10-23", now=datetime(2026, 10, 26, 0, tzinfo=UTC), sessions=sessions)
    assert before["status"] == "ready" and before["stage"] == "preliminary"
    assert boundary["status"] == "ready" and boundary["stage"] == "premarket_final"
    assert final["status"] == "ready" and final["stage"] == "premarket_final"
    assert final["prediction_cutoff"] == boundary["prediction_cutoff"]
    assert final["issued_at"] != final["prediction_cutoff"]
    assert late["status"] == "blocked_issuance_deadline"
    assert late["stage"] == "late"
    assert late["issued_at"] == "2026-10-26T00:00:00+00:00"


def test_stale_input_and_invalid_target_are_blocked():
    sessions = pd.to_datetime(["2026-10-22", "2026-10-23", "2026-10-26"])
    now = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    stale = inputs.resolve_forecast_context("2026-07-23", now=now, sessions=sessions)
    assert stale["status"] == "blocked_stale_input"
    assert stale["expected_asof_date"] == "2026-10-23"
    invalid = inputs.resolve_forecast_context("2026-10-23", now=now, sessions=sessions, target_date="2026-10-24")
    assert invalid["status"] == "blocked_invalid_target"


def test_breadth_uses_only_dated_available_common_share_membership():
    dates = pd.bdate_range("2026-09-01", periods=23)
    prices = pd.DataFrame([
        {"date": day, "symbol": symbol, "close": 100 + i * sign,
         "available_at": day.tz_localize("UTC") + timedelta(hours=12),
         "ingested_at": day.tz_localize("UTC") + timedelta(hours=12), "is_final": True, "revision": "1"}
        for symbol, sign in [("000001", 1), ("000002", -1), ("000003", 2)]
        for i, day in enumerate(dates)
    ])
    asof = dates[-1].date().isoformat()
    cutoff = pd.Timestamp(asof + "T12:00:00Z")
    members = pd.DataFrame([
        {"snapshot_date": asof, "market": "KOSPI", "symbol": "000001", "market_cap": 200, "security_type": "COMMON", "is_suspended": False, "available_at": cutoff},
        {"snapshot_date": asof, "market": "KOSPI", "symbol": "000002", "market_cap": 100, "security_type": "COMMON", "is_suspended": False, "available_at": cutoff},
        {"snapshot_date": asof, "market": "KOSPI", "symbol": "000003", "market_cap": 999, "security_type": "PREFERRED", "is_suspended": False, "available_at": cutoff},
    ])
    result = inputs.build_stock_breadth(prices, members, market="KOSPI", asof_cutoffs={asof: cutoff}, target_count=2)
    assert result.iloc[0]["breadth_member_count"] == 2
    assert result.iloc[0]["breadth_up_share_1d"] == .5
    assert result.iloc[0]["breadth_above_ma20_share"] == .5
    future = members.copy()
    future["available_at"] = cutoff + timedelta(days=1)
    assert inputs.build_stock_breadth(prices, future, market="KOSPI", asof_cutoffs={asof: cutoff}, target_count=2).empty


def test_unverified_membership_cannot_be_backdated():
    members = pd.DataFrame([{"snapshot_date": "2026-10-23", "market": "KOSPI", "symbol": "000001", "market_cap": 1}])
    prices = pd.DataFrame([{"date": "2026-10-23", "symbol": "000001", "close": 100}])
    assert inputs.build_stock_breadth(prices, members, market="KOSPI", asof_cutoffs={"2026-10-23": "2026-10-23T12:00:00Z"}, target_count=1).empty


def test_unknown_trading_status_is_not_verified_common_share_membership():
    dates = pd.bdate_range("2026-09-01", periods=23)
    asof = dates[-1].date().isoformat()
    prices = pd.DataFrame({"date": dates, "symbol": "000001", "close": range(100, 123),
                           "available_at": [day.tz_localize("UTC") + timedelta(hours=12) for day in dates]})
    members = pd.DataFrame([{"snapshot_date": asof, "market": "KOSPI", "symbol": "000001",
                            "market_cap": 1, "security_type": "COMMON", "available_at": asof + "T12:00:00Z"}])
    assert inputs.build_stock_breadth(prices, members, market="KOSPI", asof_cutoffs={asof: asof + "T14:00:00Z"}, target_count=1).empty


def test_stock_price_availability_and_later_revisions_are_point_in_time():
    dates = pd.bdate_range("2026-09-01", periods=23)
    asof = dates[-1].date().isoformat()
    cutoff = pd.Timestamp(asof + "T14:00:00Z")
    prices = pd.DataFrame({"date": dates, "symbol": "000001", "close": range(100, 123)})
    members = pd.DataFrame([{"snapshot_date": asof, "market": "KOSPI", "symbol": "000001",
                            "market_cap": 1, "security_type": "COMMON", "is_suspended": False,
                            "available_at": cutoff}])
    kwargs = {"market": "KOSPI", "asof_cutoffs": {asof: cutoff}, "target_count": 1}
    assert inputs.build_stock_breadth(prices, members, **kwargs).empty
    prices["available_at"] = [day.tz_localize("UTC") + timedelta(hours=12) for day in dates]
    prices["ingested_at"] = prices["available_at"]
    prices["is_final"] = True
    prices["revision"] = "1"
    before = inputs.build_stock_breadth(prices, members, **kwargs)
    assert len(before) == 1
    revision = prices.iloc[[-1]].copy()
    revision["close"] = 1
    revision["available_at"] = cutoff + timedelta(days=1)
    after = inputs.build_stock_breadth(pd.concat([prices, revision]), members, **kwargs)
    pd.testing.assert_frame_equal(before, after)
    unknown = prices.copy()
    unknown.loc[unknown.index[-1], "available_at"] = None
    assert inputs.build_stock_breadth(unknown, members, **kwargs).empty


def verified_breadth_prices():
    dates = pd.bdate_range("2026-09-01", periods=23)
    asof = dates[-1].date().isoformat()
    cutoff = pd.Timestamp(asof + "T14:00:00Z")
    prices = pd.DataFrame({"date": dates, "symbol": "000001", "close": range(100, 123),
                           "available_at": [day.tz_localize("UTC") + timedelta(hours=12) for day in dates]})
    prices = prices.assign(ingested_at=prices.available_at, is_final=True, revision="1")
    members = pd.DataFrame([{"snapshot_date": asof, "market": "KOSPI", "symbol": "000001",
                            "market_cap": 1, "security_type": "COMMON", "is_suspended": False,
                            "available_at": cutoff}])
    return prices, members, {"market": "KOSPI", "asof_cutoffs": {asof: cutoff}, "target_count": 1}


@pytest.mark.parametrize("field,value", [("ingested_at", "__missing__"), ("ingested_at", None), ("ingested_at", "2027-01-01T00:00:00Z"),
                                         ("is_final", "__missing__"), ("is_final", None), ("is_final", False)])
def test_breadth_prices_require_first_seen_and_explicit_finality(field, value):
    prices, members, kwargs = verified_breadth_prices()
    if value == "__missing__":
        prices = prices.drop(columns=field)
    else:
        prices[field] = value
    assert inputs.build_stock_breadth(prices, members, **kwargs).empty


def test_breadth_revision_ties_are_deterministic_and_conflicts_fail_closed():
    prices, members, kwargs = verified_breadth_prices()
    newer = prices.iloc[[-1]].copy().assign(close=1, revision="2")
    a = inputs.build_stock_breadth(pd.concat([prices, newer]), members, **kwargs)
    b = inputs.build_stock_breadth(pd.concat([newer, prices]), members, **kwargs)
    pd.testing.assert_frame_equal(a, b)
    assert a.iloc[0].breadth_up_share_1d == 0
    conflict = newer.assign(revision="1")
    assert inputs.build_stock_breadth(pd.concat([prices, conflict]), members, **kwargs).empty
    retry = inputs.build_stock_breadth(pd.concat([prices, prices.iloc[[-1]]]), members, **kwargs)
    pd.testing.assert_frame_equal(retry, inputs.build_stock_breadth(prices, members, **kwargs))


def test_missing_position_data_remains_inconclusive():
    summary = inputs.summarize_participant_pressure(pd.DataFrame(), market="KOSPI", cutoff="2026-10-23T14:00:00Z")
    assert summary["status"] == "missing"
    assert summary["inferred"] == "inconclusive"
    assert summary["observed"] == {}
    assert summary["missing"]


def test_flow_ignores_future_corrections_and_never_fills_absence_with_zero():
    rows = []
    for i, day in enumerate(pd.bdate_range("2026-09-01", periods=22)):
        for investor in ["FOREIGN", "INSTITUTION", "PENSION"]:
            rows.append({"trade_date": day, "market": "KOSPI", "investor": investor, "net_value": 10+i, "turnover": 1000, "is_final": True, "available_at": day.tz_localize("UTC") + timedelta(hours=12)})
    frame = pd.DataFrame(rows)
    asof = frame["trade_date"].max().date().isoformat()
    cutoff = pd.Timestamp(asof + "T14:00:00Z")
    before = inputs.build_participant_features(frame, market="KOSPI", asof_cutoffs={asof: cutoff})
    assert before.iloc[0]["foreign_net_to_turnover"] == .031
    late = frame.iloc[[-3]].copy()
    late["net_value"] = -999999
    late["available_at"] = cutoff + timedelta(days=1)
    after = inputs.build_participant_features(pd.concat([frame, late]), market="KOSPI", asof_cutoffs={asof: cutoff})
    pd.testing.assert_frame_equal(before, after)
    assert inputs.build_participant_features(frame.iloc[0:0], market="KOSPI", asof_cutoffs={asof: cutoff}).empty
