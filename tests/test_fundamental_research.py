"""Publication and matched-fit regression tests for macro feature research."""

import json
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from roboquant.index_forecast.accuracy_research import REFIT_STEP


@pytest.fixture(scope="module")
def inputs():
    days = pd.bdate_range("2019-01-02", periods=1420).strftime("%Y-%m-%d")
    sessions = pd.DataFrame({
        "date": days,
        "open_at": pd.to_datetime(days + "T00:00:00Z"),
        "close_at": pd.to_datetime(days + "T06:30:00Z"),
    })
    rng = np.random.default_rng(8)
    returns = rng.normal(.0004, .012, len(days))
    close = 1000 * np.exp(np.cumsum(returns))
    prices = pd.DataFrame({"index_code": "KOSPI", "session_date": days, "close": close})
    records = []
    for i in range(550, 1418):
        anchor = i // REFIT_STEP * REFIT_STEP
        records.append({
            "asof_date": days[i], "target_date": days[i + 1],
            "prediction_cutoff": days[i] + "T09:00:00Z",
            "target_close_at": days[i + 1] + "T06:30:00Z",
            "label_available_at": days[i + 1] + "T06:30:00Z",
            "fit_asof": days[anchor], "fit_cutoff": days[anchor] + "T09:00:00Z",
            "actual_return": close[i + 1] / close[i] - 1, "expected_return": .001,
            "baselines": {"mean": {"expected_return": .0005}},
        })
    evaluation = {
        "candidate_observations": {"linear:price": records}, "selected": {"family": "linear", "group": "price"},
        "observations": records[602:854], "development_start": days[900], "selection_frozen_before": days[1152],
        "settings": {"issuance_protocol": "after_close_daily_v1"},
    }
    return prices, {"markets": [{"market": "KOSPI", "evaluation": evaluation}]}, sessions, days


def evaluate(inputs, rows, i=1403):
    from roboquant.index_forecast.fundamental_research import evaluate_fundamental_research

    prices, archive, sessions, days = inputs
    return evaluate_fundamental_research(
        prices, archive, pd.DataFrame(rows), sessions=sessions,
        as_of=pd.Timestamp(days[i] + "T10:00:00Z"),
    )


def flow(day, stamp, *, market="KOSPI", final=True):
    return [{"market": market, "series": name, "observation_date": day, "value": amount,
             "unit": "KRW", "source": "krx_market_investor_value", "received_at": stamp,
             "published_at": stamp, "is_final": final}
            for name, amount in [("FOREIGN_NET", 1e12), ("INSTITUTION_NET", -2e12), ("RETAIL_NET", 1e12)]]


def test_flow_requires_exact_origin_market_and_cutoff(inputs):
    _, _, _, days = inputs
    rows = [*flow(days[1402], days[1402] + "T08:00:00Z"),
            *flow(days[1403], days[1403] + "T08:00:00Z", market="KOSDAQ"),
            *flow(days[1403], days[1403] + "T09:30:00Z")]
    result = evaluate(inputs, rows)
    family = result["markets"]["KOSPI"]["families"]["prior_session_flow"]
    assert family["latest_features"] is None
    now_ready = evaluate(inputs, [*rows, *flow(days[1403], days[1403] + "T08:00:00Z")])
    family = now_ready["markets"]["KOSPI"]["families"]["prior_session_flow"]
    assert family["latest_features"]["observation_dates"] == [days[1403]]
    assert family["latest_features"]["features"]["foreign_net_trillion_krw"] == 1.0
    assert family["next_forecast"] is None


def test_unapproved_policy_cannot_backdate_flow_or_unfinalize(inputs):
    _, _, _, days = inputs
    records = [dict(row, availability_policy="historical_dated_yield")
               for row in flow(days[1390], days[1403] + "T08:00:00Z")]
    records += flow(days[1403], days[1403] + "T08:00:00Z", final=False)
    result = evaluate(inputs, records)
    assert result["markets"]["KOSPI"]["families"]["prior_session_flow"]["latest_features"] is None
    assert result["production_weight"] == 0.0


def yield_rows(days, positions, *, policy="observed", source="us_treasury_daily_par_yield_curve", received_index=None):
    rows = []
    for i in positions:
        observed_day = days[i]
        received_day = days[i if received_index is None else received_index]
        for j, horizon in enumerate(("1Y", "3Y", "10Y")):
            rows.append({
                "market": "GLOBAL", "series": f"US_YIELD_{horizon}", "observation_date": observed_day,
                "value": 3 + .2 * j + np.sin(i / 15) / 4, "unit": "percent",
                "source": source, "received_at": received_day + "T08:00:00Z",
                "published_at": None, "is_final": True, "availability_policy": policy,
            })
    return rows


def test_dated_yields_wait_two_scheduled_krx_sessions_and_need_all_tenors(inputs):
    _, _, _, days = inputs
    rows = yield_rows(days, [1398, 1400, 1401], policy="historical_dated_yield", received_index=1403)
    rows = [row for row in rows if row["observation_date"] != days[1401] or row["series"] != "US_YIELD_3Y"]
    rows += yield_rows(days, [1402], policy="historical_dated_yield", received_index=1403)
    family = evaluate(inputs, rows)["markets"]["KOSPI"]["families"]["us_rates"]
    assert family["latest_features"]["observation_dates"] == [days[1398], days[1400]]
    assert family["latest_features"]["availability_policy"] == "historical_dated_yield"
    assert family["latest_features"]["available_at"] == days[1402] + "T09:00:00+00:00"
    assert family["next_forecast"] is None
    forbidden = [dict(row, source="fred") for row in rows]
    assert evaluate(inputs, forbidden)["markets"]["KOSPI"]["families"]["us_rates"]["latest_features"] is None


def test_no_future_actual_enters_identically_matched_training(inputs):
    _, _, _, days = inputs
    rows = yield_rows(days, range(1419))
    result = evaluate(inputs, rows)
    family = result["markets"]["KOSPI"]["families"]["us_rates"]
    assert family["windows"]["development"]["scored_n"] == 252
    assert family["windows"]["development"]["models"]["price_only_matched"]["n"] == 252
    fit = family["observations"][0]
    assert fit["train_n"] == 504
    position = list(days).index(fit["fit_asof"])
    assert position % 21 == 0
    assert fit["train_last_asof"] < days[position - 1]
    assert fit["train_latest_label_available_at"] <= fit["fit_cutoff"]
    forecast = family["next_forecast"]
    assert forecast["target_date"] == days[1404]
    assert forecast["production_weight"] == 0.0
    changed = [dict(row, value=99) if row["observation_date"] > days[1403] else row for row in rows]
    assert evaluate(inputs, changed) == result
    json.dumps(result, allow_nan=False)


def test_margin_keeps_scope_and_waits_for_later_verified_publication(inputs):
    _, _, _, days = inputs
    rows = [
        {"market": scope, "series": "MARGIN_CREDIT_BALANCE", "observation_date": days[i],
         "value": value, "unit": "KRW", "source": "kofia_freesis_margin_credit", "availability_policy": "observed",
         "received_at": days[i] + "T07:00:00Z", "published_at": days[i] + hour, "is_final": True}
        for scope, i, hour, value in [
            ("GLOBAL", 1400, "T08:00:00Z", 1e12), ("GLOBAL", 1401, "T08:00:00Z", 2e12),
            ("KOSPI", 1402, "T08:00:00Z", 3e12), ("KOSPI", 1403, "T09:30:00Z", 4e12),
        ]
    ]
    family = evaluate(inputs, rows)["markets"]["KOSPI"]["families"]["margin_credit"]
    assert family["latest_features"]["observation_markets"] == ["GLOBAL"]
    assert family["latest_features"]["features"]["log_credit_change"] == pytest.approx(np.log(2))


def test_historical_kofia_margin_waits_for_d_plus_two_and_requires_actual_receipt(inputs):
    from roboquant.index_forecast.fundamental_research import evaluate_fundamental_research

    prices, archive, sessions, days = inputs
    i = 1403
    received = days[i] + "T09:30:00Z"
    rows = [{"market": "KOSPI", "series": "MARGIN_CREDIT_BALANCE", "observation_date": day,
             "value": 1e12 + position * 1e8, "unit": "KRW", "source": "kofia_freesis_margin_credit",
             "received_at": received, "published_at": None, "is_final": None,
             "availability_policy": "historical_dated_credit"}
            for position, day in enumerate(days[:i + 1])]
    before = evaluate_fundamental_research(
        prices, archive, pd.DataFrame(rows), sessions=sessions,
        as_of=pd.Timestamp(days[i] + "T09:29:00Z"),
    )
    assert before["markets"]["KOSPI"]["families"]["margin_credit"]["next_forecast"] is None
    assert before["markets"]["KOSPI"]["families"]["prior_session_margin_credit"]["next_forecast"] is None
    result = evaluate(inputs, rows, i=i)
    family = result["markets"]["KOSPI"]["families"]["margin_credit"]
    assert family["windows"]["development"]["scored_n"] == 252
    latest = family["latest_features"]
    assert latest["observation_dates"] == [days[i - 3], days[i - 2]]
    assert latest["available_at"] == days[i] + "T09:00:00+00:00"
    assert latest["availability_policy"] == "historical_dated_credit"
    assert latest["source_finality"] == "unverified_settlement_snapshot"
    forecast = family["next_forecast"]
    assert forecast["target_date"] == days[i + 1]
    assert forecast["feature_evidence"]["available_at"] == pd.Timestamp(received).isoformat()
    assert forecast["fit_provenance"] == "retrospective_reconstruction"
    assert forecast["model_training_clock"] == "simulated_historical_cutoffs"
    assert result["markets"]["KOSDAQ"]["families"]["margin_credit"]["next_forecast"] is None

    prior = result["markets"]["KOSPI"]["families"]["prior_session_margin_credit"]
    assert prior["windows"]["development"]["scored_n"] == 252
    evidence = prior["latest_features"]
    assert evidence["availability_policy"] == "historical_dated_credit_d_plus_1_after_close"
    assert evidence["balance_observation_date"] == days[i - 1]
    balance = rows[i - 1]["value"]
    assert evidence["balance_krw"] == balance
    assert evidence["available_at"] == days[i] + "T09:00:00+00:00"
    for lag in (1, 5, 20):
        older = i - 1 - lag
        ratio = balance / rows[older]["value"]
        assert evidence["comparison_observation_dates"][str(lag)] == days[older]
        assert evidence["native_change_pct"][str(lag)] == pytest.approx((ratio - 1.) * 100)
        assert evidence["features"][f"credit_log_change_{lag}d"] == pytest.approx(np.log(ratio))
    assert len(evidence["features"]) == 3
    prior_forecast = prior["next_forecast"]
    assert prior_forecast["feature_evidence"]["available_at"] == pd.Timestamp(received).isoformat()
    assert prior_forecast["feature_evidence"]["source_finality"] == "unverified_settlement_snapshot"
    effect = prior_forecast["credit_counterfactual"]
    assert effect["point_effect"] == pytest.approx(
        prior_forecast["expected_index"] - effect["flat_credit_change_expected_index"],
    )
    assert result["markets"]["KOSDAQ"]["families"]["prior_session_margin_credit"]["next_forecast"] is None

    unverified = [dict(row, source="unverified_credit_export") for row in rows[-7:]]
    other = evaluate(inputs, unverified, i=i)["markets"]["KOSPI"]["families"]["margin_credit"]
    assert other["latest_features"] is None


def test_prior_credit_does_not_fill_exact_dates_or_ignore_later_publication(inputs):
    _, _, _, days = inputs
    i = 1403
    positions = [i - 22, i - 6, i - 2, i - 1]
    rows = [{"market": "KOSPI", "series": "MARGIN_CREDIT_BALANCE", "observation_date": days[pos],
             "value": 1e12 + pos * 1e8, "unit": "KRW", "source": "kofia_freesis_margin_credit",
             "received_at": days[i] + "T09:55:00Z", "published_at": None, "is_final": None,
             "availability_policy": "historical_dated_credit"}
            for pos in positions]
    result = evaluate(inputs, rows, i=i)
    assert result["markets"]["KOSPI"]["families"]["prior_session_margin_credit"]["latest_features"] is None
    rows[0]["observation_date"] = days[i - 21]
    rows[-1]["published_at"] = days[i] + "T09:45:00Z"
    result = evaluate(inputs, rows, i=i)
    assert result["markets"]["KOSPI"]["families"]["prior_session_margin_credit"]["latest_features"] is None


def test_unapproved_final_flows_and_pending_credit_are_excluded(inputs):
    _, _, _, days = inputs
    i = 1403
    received = days[i] + "T08:00:00Z"
    rows = [dict(row, source="unverified_flow_export") for row in flow(days[i], received)]
    for day in days[i - 6:i - 3]:
        rows.append({"market": "KOSPI", "series": "MARGIN_CREDIT_BALANCE", "observation_date": day,
                     "value": 1e12, "unit": "KRW", "source": "kofia_freesis_margin_credit",
                     "received_at": received, "published_at": None, "is_final": False,
                     "availability_policy": "historical_dated_credit"})
        rows.append(dict(rows[-1], source="unverified_credit_export", is_final=True, availability_policy="observed"))
        rows.append(dict(rows[-2], is_final=None, availability_policy="observed"))
    result = evaluate(inputs, rows, i=i)
    market = result["markets"]["KOSPI"]["families"]
    assert market["prior_session_flow"]["latest_features"] is None
    assert market["margin_credit"]["latest_features"] is None


def test_kofia_freesis_complete_korean_yield_panel_can_be_evaluated(inputs):
    _, _, _, days = inputs
    rows = yield_rows(days, [1401, 1402], source="kofia_freesis_bond_yield")
    for row in rows:
        row["series"] = row["series"].replace("US_", "KR_")
    family = evaluate(inputs, rows)["markets"]["KOSPI"]["families"]["kr_rates"]
    assert family["latest_features"]["observation_dates"] == [days[1401], days[1402]]


def test_optional_macro_families_use_matched_windows_and_gate_prospective_inputs(inputs, monkeypatch):
    from roboquant.index_forecast.fundamental_research import evaluate_fundamental_research

    prices, archive, sessions, days = inputs
    now = pd.Timestamp(days[1403] + "T10:00:00Z")
    raw = pd.DataFrame({"family": ["fed_g17"]})
    evidence = {"fed_g17": {"rows": 1}, "kr_exports": {"status": "unavailable"}}

    def features_at(family, cutoff, *, prospective):
        assert family == "fed_g17"
        if prospective:
            return None
        return {"features": {"level_change": cutoff.month / 100.},
                "available_at": cutoff.isoformat(), "sources": ["federal_reserve_g17"]}

    def prepare_macro_releases(releases, *, as_of):
        pd.testing.assert_frame_equal(releases, raw)
        assert as_of == now
        return SimpleNamespace(families=("fed_g17",), evidence=evidence, features_at=features_at)

    monkeypatch.setitem(sys.modules, "roboquant.index_forecast.macro_release_inputs",
                        SimpleNamespace(prepare_macro_releases=prepare_macro_releases))
    result = evaluate_fundamental_research(
        prices, archive, pd.DataFrame(), sessions=sessions, as_of=now, macro_releases=raw,
    )
    assert result["macro_release_evidence"] == evidence
    family = result["markets"]["KOSPI"]["families"]["fed_g17"]
    development = family["windows"]["development"]
    assert development["expected_n"] == development["scored_n"] == 252
    assert development["models"]["price_only_matched"]["n"] == 252
    assert family["latest_features"]["sources"] == ["federal_reserve_g17"]
    assert family["next_forecast"] is None
    assert result["production_weight"] == 0.0
