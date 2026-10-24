"""Synthetic contract tests; these are not real participant observations."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest


def module():
    assert importlib.util.find_spec("roboquant.index_forecast.participant_impact") is not None, (
        "participant observation bridge and impact analysis must be implemented"
    )
    return importlib.import_module("roboquant.index_forecast.participant_impact")


def observations(*, periods=45, scope="market_total", actor="FOREIGN"):
    result = []
    for i, day in enumerate(pd.bdate_range("2026-08-03", periods=periods)):
        date = day.date().isoformat()
        symbols = [(None, 1)] if scope == "market_total" else [("000001", 1), ("000002", 2)]
        for symbol, multiplier in symbols:
            result.append({
                "record_type": "flow", "scope": scope, "market": "KOSPI", "symbol": symbol,
                "investor_type": actor, "observation_date": date,
                "net_value": (i + 1) * multiplier, "net_qty": (i + 1) * multiplier,
                "turnover": 1000 * multiplier, "unit": "KRW", "quantity_unit": "shares",
                "source": "KRX_CSV", "source_url": "https://data.krx.co.kr/",
                "available_at": date + "T12:00:00+00:00", "ingested_at": date + "T12:00:00+00:00",
                "revision": "1", "is_final": True,
            })
    return result


def cutoffs(rows):
    return {r["observation_date"]: r["observation_date"] + "T14:00:00+00:00" for r in rows}


def bridge(tmp_path, rows):
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    path = tmp_path / f"bridge-{digest}.json"
    module().write_participant_bridge(path, rows)
    return path


def stock_context(rows):
    members, prices = [], []
    for i, date in enumerate(sorted(cutoffs(rows))):
        for symbol, cap in [("000001", 200), ("000002", 100)]:
            common = {"symbol": symbol, "available_at": date + "T12:00:00+00:00"}
            members.append({**common, "snapshot_date": date, "market": "KOSPI", "market_cap": cap,
                            "security_type": "COMMON", "trading_status": "ACTIVE"})
            prices.append({**common, "date": date, "close": 100 + i, "traded_value": cap * 10,
                           "price_adjustment": "unadjusted", "corporate_action_status": "verified_none",
                           "ingested_at": common["available_at"], "is_final": True, "revision": "1"})
    return pd.DataFrame(members), pd.DataFrame(prices)


def index_history(rows):
    dates = sorted(cutoffs(rows))
    return pd.DataFrame([
        {"asof_date": date, "target_date": dates[i + 1] if i + 1 < len(dates) else None,
         "label_return": ((i * 7) % 13 - 6) / 1000, "return_1d": ((i * 3) % 11 - 5) / 1000,
         "return_5d": ((i * 2) % 9 - 4) / 1000,
         "label_available_at": dates[i + 1] + "T12:00:00+00:00" if i + 1 < len(dates) else None}
        for i, date in enumerate(dates)
    ])


def run(tmp_path, rows, **kwargs):
    return module().build_participant_impact_inputs(
        market="KOSPI", asof_cutoffs=cutoffs(rows), bridge_path=bridge(tmp_path, rows), **kwargs
    )


def test_missing_source_is_insufficient_not_zero_impact(tmp_path):
    result = module().build_participant_impact_inputs(
        market="KOSPI", asof_cutoffs={"2026-10-23": "2026-10-23T14:00:00+00:00"},
        sqlite_path=tmp_path / "missing.sqlite",
    )
    assert result["features"].empty and not result["feature_groups"]
    assert result["report"]["status"] == "insufficient_data"
    assert result["report"]["forecast_uplift"] == "unmeasured"
    assert result["report"]["influence"] == {}
    assert len(result["report"]["evidence_cards"]) == 10
    assert result["report"]["position_evidence"]["inferred"] == "inconclusive"
    assert not (tmp_path / "missing.sqlite").exists()


def test_foreign_market_group_does_not_require_pension_or_institution(tmp_path):
    rows = observations()
    result = run(tmp_path, rows)
    key = "participant_market_foreign_raw"
    assert list(result["feature_groups"]) == [key]
    assert result["features"].iloc[-1][result["feature_groups"][key][0]] == pytest.approx(.045)
    assert result["report"]["missing_participants"] == ["institution", "pension"]
    assert result["report"]["forecast_uplift"] == "unmeasured"


def test_later_revision_cannot_change_earlier_features_or_associations(tmp_path):
    rows = observations()
    history = index_history(rows)
    before = run(tmp_path, rows, index_history=history)
    revision = {**rows[20], "net_value": -999999, "revision": "2", "available_at": "2027-01-01T12:00:00+00:00"}
    after = run(tmp_path, rows + [revision], index_history=history)
    pd.testing.assert_frame_equal(before["features"], after["features"])
    assert before["report"]["influence"] == after["report"]["influence"]


def test_stock_subset_cannot_be_promoted_to_market_total(tmp_path):
    result = run(tmp_path, observations(scope="stock"))
    assert not result["feature_groups"]
    assert result["report"]["status"] == "insufficient_data"
    assert "stock_universe_unverified" in result["report"]["limitations"]


def test_stock_raw_requires_complete_dated_membership_and_prices(tmp_path):
    rows = observations(scope="stock")
    members, prices = stock_context(rows)
    result = run(tmp_path, rows, membership=members, stock_prices=prices, target_count=2)
    key = "participant_stock_foreign_raw"
    assert key in result["feature_groups"]
    assert not any("market_foreign" in key for key in result["feature_groups"])
    assert result["features"].iloc[-1][result["feature_groups"][key][0]] == pytest.approx(135 / 3000)
    # A ranking snapshot published later cannot supply the earlier membership.
    members["available_at"] = "2027-01-01T00:00:00+00:00"
    blocked = run(tmp_path, rows, membership=members, stock_prices=prices, target_count=2)
    assert not blocked["feature_groups"]


@pytest.mark.parametrize("field,value", [("available_at", None), ("price_adjustment", "unknown"),
                                         ("corporate_action_status", "unknown")])
def test_unknown_price_availability_or_adjustment_blocks_stock_feature(tmp_path, field, value):
    rows = observations(scope="stock")
    members, prices = stock_context(rows)
    prices[field] = value
    result = run(tmp_path, rows, membership=members, stock_prices=prices, target_count=2)
    assert not result["feature_groups"]


@pytest.mark.parametrize("field,value", [("ingested_at", "__missing__"), ("ingested_at", None), ("ingested_at", "2027-01-01T00:00:00Z"),
                                         ("is_final", "__missing__"), ("is_final", None), ("is_final", False)])
def test_impact_prices_require_first_seen_and_explicit_finality(tmp_path, field, value):
    rows = observations(scope="stock")
    members, prices = stock_context(rows)
    if value == "__missing__":
        prices = prices.drop(columns=field)
    else:
        prices[field] = value
    result = run(tmp_path, rows, membership=members, stock_prices=prices, target_count=2)
    assert not result["feature_groups"]


def test_impact_price_revision_ties_are_deterministic_and_ambiguous_conflicts_rejected():
    rows = observations(scope="stock")
    _, prices = stock_context(rows)
    day = prices.date.max()
    symbols = ["000001", "000002"]
    cutoff = pd.Timestamp(day + "T14:00:00Z")
    newer = prices.iloc[[-1]].copy().assign(close=1, revision="2")
    a = module()._prices(pd.concat([prices, newer]), symbols, day, cutoff)
    b = module()._prices(pd.concat([newer, prices]), symbols, day, cutoff)
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))
    assert a.loc[a.symbol == "000002", "close"].iloc[0] == 1
    conflict = newer.assign(revision="1")
    assert module()._prices(pd.concat([prices, conflict]), symbols, day, cutoff).empty
    retry = module()._prices(pd.concat([prices, prices.iloc[[-1]]]), symbols, day, cutoff)
    expected = module()._prices(prices, symbols, day, cutoff)
    pd.testing.assert_frame_equal(retry.reset_index(drop=True), expected.reset_index(drop=True))


def test_current_membership_does_not_fill_historical_daily_universe(tmp_path):
    rows = observations(scope="stock")
    members, prices = stock_context(rows)
    members = members.loc[members["snapshot_date"] == members["snapshot_date"].min()]
    result = run(tmp_path, rows, membership=members, stock_prices=prices, target_count=2)
    assert not result["feature_groups"]


def test_missing_ownership_keeps_raw_but_omits_position_group(tmp_path):
    rows = observations(scope="stock")
    members, prices = stock_context(rows)
    result = run(tmp_path, rows, membership=members, stock_prices=prices, target_count=2)
    assert "participant_stock_foreign_raw" in result["feature_groups"]
    assert "participant_stock_foreign_position" not in result["feature_groups"]


def test_position_uses_observed_ownership_delta_and_never_holding_probability(tmp_path):
    flows = observations(scope="stock")
    members, prices = stock_context(flows)
    own = []
    for i, date in enumerate(sorted(cutoffs(flows))):
        for symbol in ("000001", "000002"):
            template = next(r for r in flows if r["symbol"] == symbol and r["observation_date"] == date)
            own.append({**template, "record_type": "ownership", "ownership_pct": 40 + i / 10,
                        "unit": "percent", "investor_type": "FOREIGN"})
    result = run(tmp_path, flows + own, membership=members, stock_prices=prices, target_count=2)
    cols = result["feature_groups"]["participant_stock_foreign_position"]
    delta = next(c for c in cols if "ownership_change_pp" in c)
    assert result["features"].iloc[-1][delta] == pytest.approx(2)
    assert "not_causal" in result["report"]["limitations"]
    assert not any("probability" in c or "holding" in c for c in cols)


def test_unknown_availability_uses_actual_first_seen_not_trade_date(tmp_path):
    rows = observations()
    for row in rows:
        row["available_at"] = None
        row["ingested_at"] = "2026-12-01T12:00:00+00:00"
    result = run(tmp_path, rows)
    assert not result["feature_groups"]
    assert result["bridge_manifest"]["availability_fallback_rows"] == len(rows)


def test_bridge_hash_is_stable_and_tampering_is_rejected(tmp_path):
    rows = observations()
    path = bridge(tmp_path, rows)
    original = path.read_bytes()
    module().write_participant_bridge(path, rows)
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="immutable"):
        module().write_participant_bridge(path, [{**rows[0], "net_value": 999}])
    content = json.loads(original)
    content["observations"][0]["net_value"] = 999
    path.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        module().build_participant_impact_inputs(market="KOSPI", asof_cutoffs=cutoffs(rows), bridge_path=path)


def test_sub_sqlite_adapter_is_read_only_and_preserves_metadata_gaps(tmp_path):
    path = tmp_path / "sub.sqlite"
    schema = Path(__file__).parents[1] / "sub_project/Smart_Money_Position_Analyzer/smartmoney/schema.sql"
    with sqlite3.connect(path) as conn:
        conn.executescript(schema.read_text())
        conn.execute("INSERT INTO kr_investor_flows(symbol,trade_date,investor_type,net_value,source_code,available_at,ingested_at) VALUES(?,?,?,?,?,?,?)",
                     ("005930", "2026-10-23", "FOREIGN", 100, "KRX_CSV", "2026-10-23T12:00:00+00:00", "2026-10-23T12:00:00+00:00"))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = module().build_participant_impact_inputs(
        market="KOSPI", asof_cutoffs={"2026-10-23": "2026-10-23T14:00:00+00:00"}, sqlite_path=path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert result["report"]["source_coverage"]["flow"] == 1
    assert "source_finality_not_recorded" in result["report"]["limitations"]
    assert not result["feature_groups"]


def test_same_session_association_is_not_next_session_or_forecast_uplift(tmp_path):
    rows = observations()
    history = index_history(rows)
    valid = run(tmp_path, rows, index_history=history)
    influence = valid["report"]["influence"]["participant_market_foreign_raw"][0]
    assert influence["next_session"]["n"] == 25
    assert influence["next_session"]["status"] == "observational_only"
    assert influence["next_session"]["conditional_slope"] is not None
    history["target_date"] = history["asof_date"]
    invalid = run(tmp_path, rows, index_history=history)
    effect = invalid["report"]["influence"]["participant_market_foreign_raw"][0]
    assert effect["same_session"]["n"] > 0
    assert effect["next_session"]["n"] == 0
    assert effect["next_session"]["conditional_slope"] is None
    assert invalid["report"]["forecast_uplift"] == "unmeasured"


def test_unknown_units_and_nonfinal_values_are_not_eligible(tmp_path):
    rows = observations()
    for row in rows:
        row["unit"] = "million_KRW"
    assert not run(tmp_path, rows)["feature_groups"]
    for row in rows:
        row["unit"] = "KRW"
        row["is_final"] = False
    assert not run(tmp_path, rows)["feature_groups"]


def test_later_ingestion_cannot_backdate_a_declared_publication(tmp_path):
    rows = observations()
    for row in rows:
        row["ingested_at"] = "2027-01-01T12:00:00+00:00"
    assert not run(tmp_path, rows)["feature_groups"]


def test_verified_bridge_prices_can_supply_stock_context(tmp_path):
    flows = observations(scope="stock")
    members, prices = stock_context(flows)
    price_rows = []
    for row in prices.to_dict("records"):
        price_rows.append({**row, "record_type": "price", "scope": "stock", "market": "KOSPI",
                           "observation_date": row["date"], "investor_type": None, "unit": "KRW",
                           "source": "KRX_CSV", "source_url": "https://data.krx.co.kr/", "is_final": True,
                           "ingested_at": row["available_at"], "revision": "1"})
    result = run(tmp_path, flows + price_rows, membership=members, target_count=2)
    assert "participant_stock_foreign_raw" in result["feature_groups"]
