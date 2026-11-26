import json
import math
from contextlib import closing

import pytest

from roboquant.index_forecast import automation_state
from roboquant.index_forecast.market_closes import record_observation, write_market_closes
from roboquant.index_forecast.postclose import PROTOCOL
from roboquant.index_forecast.reliable_inputs import session_schedule
from roboquant.index_forecast.sqlite_store import connect_index_forecast
from scripts import run_bc_cpu_afterclose as daily

ASOF = "2026-11-25"
PRIOR = "2026-11-24"
TARGET = "2026-11-26"
RECEIVED = "2026-11-25T09:12:00+00:00"
CUTOFF = "2026-11-25T09:13:00+00:00"
NOW = "2026-11-25T09:14:00+00:00"


def prepared_inputs(tmp_path, moves, *, conflicts=(), asof=ASOF, received=RECEIVED,
                    cutoff=CUTOFF, operation_mode=None, observations=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    sqlite = tmp_path / "input.sqlite"
    sessions, _ = session_schedule("2019-01-01", asof)
    prior = sessions["date"].iloc[-2]
    markets = {}
    with closing(connect_index_forecast(sqlite)) as db:
        for market, endpoint in moves.items():
            levels = [
                round(1000 + 75 * math.sin(i * .12) + 51 * math.cos(i * .91) + i * .08, 2)
                for i in range(len(sessions))
            ]
            levels[-2:] = [1000.0, endpoint]
            db.executemany(
                "INSERT INTO index_eod(index_code,session_date,close,source,ingested_at) VALUES(?,?,?,?,?)",
                [(market, day, close, "smoke_history", received)
                 for day, close in zip(sessions["date"], levels, strict=True)],
            )
            db.commit()
            for day, close in zip((prior, asof), levels[-2:], strict=True):
                if (market, day) not in conflicts:
                    status = "verified"
                else:
                    status = "conflict"
                record_observation(db, {
                    "index_code": market, "session_date": day, "close": close, "source": "smoke_pair",
                    "received_at": received, "status": status,
                })
            markets[market] = {
                "session_date": asof, "close": endpoint, "received_at": received, "source": "smoke_pair",
                "status": "conflict" if (market, asof) in conflicts else "verified",
            }
    public = tmp_path / "market_closes.json"
    write_market_closes(
        {"schema_version": "market_closes_v1", "run_id": "smoke", "checked_at": received, "markets": markets},
        public, published_at=received,
    )
    frozen = tmp_path / "frozen"
    files = {"sqlite": sqlite, "market_closes": public,
             "market_closes_receipt": public.with_name("market_closes_publication.json")}
    if operation_mode == "morning":
        us = tmp_path / "us.csv"
        if observations is None:
            us.write_text("symbol,close\n^GSPC,1000\n")
        else:
            observations.to_csv(us, index=False)
        files["us_csv"] = us
    automation_state.freeze_inputs(files, frozen, prepared_at=cutoff,
                                   code_hash=automation_state.source_fingerprint(),
                                   issuance_protocol=PROTOCOL, operation_mode=operation_mode)
    return frozen / "manifest.json"


def stub_forecast(monkeypatch):
    calls = []

    def predict(market, closes, *, run_c, **kwargs):
        calls.append((market, run_c))
        close = closes[-1]
        b = {"close": close, "expected_index": close, "expected_return": 0.0, "p_up": .55, "p_down": .45,
             "interval": {"low": close * .95, "high": close * 1.05, "level": .8}}
        c = {"close": close, "method": "hist_gradient" if market == "KOSPI" else "regime_prior",
             "p_up": .06, "p_normal": .9, "p_down": .04, "p_extreme": .1, "p_up_given_extreme": .6}
        return {"b": b, "c": c if run_c else None}

    monkeypatch.setattr(daily, "forecast_market", predict)
    return calls


def payloads(output):
    return tuple(json.loads((output / f"model_{letter}_forecast.json").read_text()) for letter in "bc")


@pytest.mark.parametrize(
    "levels,expected",
    [
        ({"KOSPI": 1030, "KOSDAQ": 1000}, "ready"),
        ({"KOSPI": 1000, "KOSDAQ": 970}, "ready"),
        ({"KOSPI": 1029.99, "KOSDAQ": 970.01}, "not_triggered"),
        ({"KOSPI": 1020}, "not_ready"),
        ({"KOSPI": 1030}, "ready"),
    ],
)
def test_threshold_and_missing_market(tmp_path, monkeypatch, levels, expected):
    calls = stub_forecast(monkeypatch)
    output = tmp_path / "publish"
    result = daily.run_prepared(prepared_inputs(tmp_path, levels), output, now=NOW, run_id="101")
    b, c = payloads(output)
    assert c["status"] == expected
    assert {path.name for path in automation_state.public_payload_paths(output)} == {
        "model_b_forecast.json", "model_c_forecast.json",
    }
    assert b["publication_id"] == c["publication_id"]
    assert (c["asof_date"], c["target_date"]) == (ASOF, TARGET)
    assert {name for name, _ in calls} == levels.keys()
    assert all(run_c == (expected == "ready") for _, run_c in calls)
    assert bool(c["markets"]) == (expected == "ready")
    assert result["status"] == "published"


def test_conflicting_prior_cannot_trigger_model_c(tmp_path, monkeypatch):
    calls = stub_forecast(monkeypatch)
    manifest = prepared_inputs(tmp_path, {"KOSPI": 1050, "KOSDAQ": 1000}, conflicts=(("KOSPI", PRIOR),))
    output = tmp_path / "publish"
    daily.run_prepared(manifest, output, now=NOW)
    _, c = payloads(output)
    assert c["status"] == "not_ready"
    assert c["trigger_returns"] == {"KOSDAQ": 0.0}
    assert calls and not any(run_c for _, run_c in calls)


def test_trigger_in_one_market_does_not_score_other_markets_conflicting_history(tmp_path, monkeypatch):
    calls = stub_forecast(monkeypatch)
    manifest = prepared_inputs(tmp_path, {"KOSPI": 1030, "KOSDAQ": 970}, conflicts=(("KOSDAQ", PRIOR),))
    output = tmp_path / "publish"
    daily.run_prepared(manifest, output, now=NOW)
    _, c = payloads(output)
    assert c["status"] == "ready"
    assert [row["market"] for row in c["markets"]] == ["KOSPI"]
    assert calls == [("KOSPI", True)]


@pytest.mark.parametrize("when", ["2026-06-03T18:01:00+09:00", "2026-11-22T18:01:00+09:00"])
def test_holiday_or_weekend_never_opens_inputs(tmp_path, when):
    result = daily.run_prepared(tmp_path / "missing.json", tmp_path / "publish", now=when)
    assert result["status"] == "non_trading_day"
    assert not (tmp_path / "publish").exists()


def test_same_session_replay_preserves_first_result(tmp_path, monkeypatch):
    calls = stub_forecast(monkeypatch)
    manifest = prepared_inputs(tmp_path, {"KOSPI": 1010, "KOSDAQ": 1000})
    output = tmp_path / "publish"
    daily.run_prepared(manifest, output, now=NOW, run_id="101")
    original = payloads(output)
    assert daily.run_prepared(manifest, output, now=NOW, run_id="101") == {
        "status": "already_published", "redeploy": True,
    }
    assert daily.run_prepared(manifest, output, now=NOW, run_id="102") == {
        "status": "already_published", "redeploy": False,
    }
    assert len(calls) == 2
    assert payloads(output) == original


def test_same_session_retry_recovers_only_missing_results(tmp_path, monkeypatch):
    calls = stub_forecast(monkeypatch)
    output = tmp_path / "publish"
    daily.run_prepared(prepared_inputs(tmp_path / "first", {"KOSPI": 1020}), output, now=NOW, run_id="101")
    first_b, first_c = payloads(output)
    assert first_c["status"] == "not_ready"
    daily.run_prepared(prepared_inputs(tmp_path / "retry", {"KOSPI": 1020, "KOSDAQ": 970}),
                       output, now=NOW, run_id="102")
    b, c = payloads(output)
    assert first_b["markets"][0] == b["markets"][0]
    assert c["status"] == "ready"
    assert {row["market"] for row in c["markets"]} == {"KOSPI", "KOSDAQ"}
    assert calls == [("KOSPI", False), ("KOSPI", True), ("KOSDAQ", True)]


def test_full_cpu_run(tmp_path):
    manifest = prepared_inputs(tmp_path, {"KOSPI": 1030.0, "KOSDAQ": 991.0})
    output = tmp_path / "publish"
    result = daily.run_prepared(manifest, output, now=NOW, run_id="smoke")
    assert result["status"] == "published"
    b, c = payloads(output)
    assert b["inference_device"] == c["inference_device"] == "cpu"
    assert c["status"] == "ready"
    assert c["trigger_returns"] == {"KOSPI": 0.03, "KOSDAQ": -0.009}
    assert {row["method"] for row in c["markets"]} == {"hist_gradient", "regime_prior"}
    for row in b["markets"] + c["markets"]:
        interval = row["interval"]
        assert interval["level"] == 0.8
        assert 0 < interval["low"] <= interval["high"]
        assert row["expected_index"] == pytest.approx(row["close"] * (1 + row["expected_return"]))
    for row in c["markets"]:
        assert row["asof_date"] == ASOF and row["target_date"] == TARGET
        assert math.isclose(sum(row[key] for key in ("p_up", "p_down", "p_normal")), 1.0)

    assert {path.name for path in automation_state.public_payload_paths(output)} == {
        "model_b_forecast.json", "model_c_forecast.json",
    }
    c["markets"][0]["interval"]["low"] = c["markets"][0]["interval"]["high"] + 1
    (output / "model_c_forecast.json").write_text(json.dumps(c))
    assert not automation_state.public_payload_paths(output)


def test_prepared_bytes_are_verified_before_any_output(tmp_path, monkeypatch):
    calls = stub_forecast(monkeypatch)
    manifest = prepared_inputs(tmp_path, {"KOSPI": 1030, "KOSDAQ": 1000})
    data = json.loads(manifest.read_text())
    (manifest.parent / data["files"]["sqlite"]["path"]).write_bytes(b"invalid")
    output = tmp_path / "publish"
    assert daily.run_prepared(manifest, output, now=NOW)["status"] == "not_ready"
    assert not output.exists()
    assert not calls


def test_partial_or_mismatched_cpu_pair_is_excluded_from_staging(tmp_path, monkeypatch):
    stub_forecast(monkeypatch)
    output = tmp_path / "publish"
    daily.run_prepared(prepared_inputs(tmp_path, {"KOSPI": 1030, "KOSDAQ": 1000}), output, now=NOW, run_id="101")
    b, _ = payloads(output)
    b["publication_id"] = "different"
    (output / "model_b_forecast.json").write_text(json.dumps(b))
    assert not automation_state.public_payload_paths(output)


def test_morning_saturday_and_monday_use_same_completed_anchor_but_update_daily(tmp_path, monkeypatch):
    calls = []
    stub_forecast(monkeypatch)

    def predict(market, closes, *, close_received_at, **kwargs):
        assert close_received_at["2026-11-27"].isoformat() == kwargs["prediction_cutoff"].isoformat()
        calls.append(market)
        close = closes[-1]
        return {"close": close, "expected_index": close * 1.01, "expected_return": .01,
                "p_up": .55, "p_down": .45, "interval": {"level": .8, "low": close * .95, "high": close * 1.05}}

    monkeypatch.setattr(daily, "forecast_morning_market", predict)
    output = tmp_path / "publish"
    for date in ("2026-11-28", "2026-11-30"):
        stamp = date + "T08:04:00+09:00"
        manifest = prepared_inputs(tmp_path / date, {"KOSPI": 1030, "KOSDAQ": 1000}, asof="2026-11-27",
                                   received=stamp, cutoff=stamp, operation_mode="morning")
        assert daily.run_prepared(manifest, output, now=stamp, operation_mode="morning")["status"] == "published"
        b, c = payloads(output)
        assert b["issue_stage"] == "premarket" and c["status"] == "ready"
        assert b["run_date"] == date
        assert (c["asof_date"], c["target_date"]) == ("2026-11-27", "2026-11-30")
        assert all(row["close"] != row["expected_index"] for row in b["markets"])
        assert daily.run_prepared(manifest, output, now=stamp, operation_mode="morning")["status"] == "already_published"
    assert calls == ["KOSPI", "KOSDAQ"] * 2
    assert daily.run_prepared(manifest, output, now="2026-11-30T18:00:00+09:00",
                              operation_mode="morning")["status"] == "outside_morning_window"


def test_missing_morning_input_preserves_same_target_baseline_until_retry(tmp_path, monkeypatch):
    stub_forecast(monkeypatch)
    output = tmp_path / "publish"
    levels = {"KOSPI": 1030.0, "KOSDAQ": 991.0}
    daily.run_prepared(prepared_inputs(tmp_path / "evening", levels), output, now=NOW)
    evening = payloads(output)[0]
    stamp = "2026-11-26T08:04:00+09:00"
    morning = prepared_inputs(tmp_path / "morning", levels, received=stamp, cutoff=stamp,
                              operation_mode="morning")

    def missing(*args, **kwargs):
        raise ValueError("missing US observation")

    monkeypatch.setattr(daily, "forecast_morning_market", missing)
    daily.run_prepared(morning, output, now=stamp, operation_mode="morning")
    fallback = payloads(output)[0]
    assert [row["market"] for row in fallback["markets"]] == ["KOSPI", "KOSDAQ"]
    for row, old in zip(fallback["markets"], evening["markets"], strict=True):
        assert row == {**old, "carried_forward": True}

    def available(market, closes, **kwargs):
        close = closes[-1]
        return {"close": close, "expected_index": close * 1.01, "expected_return": .01,
                "p_up": .55, "p_down": .45, "interval": {"level": .8, "low": close * .95, "high": close * 1.05}}

    monkeypatch.setattr(daily, "forecast_morning_market", available)
    assert daily.run_prepared(morning, output, now=stamp, operation_mode="morning")["status"] == "published"
    refreshed = payloads(output)[0]
    assert all(row["expected_return"] == .01 and "carried_forward" not in row for row in refreshed["markets"])


def test_full_cpu_morning_run(tmp_path):
    from test_bc_morning import BASE_SYMBOLS, observations_for

    from roboquant.index_forecast.us_market_inputs import us_close_schedule

    dates = us_close_schedule("2018-12-26", "2026-11-26")
    us_dates = dates.loc[dates["symbol"].eq("^GSPC"), "session_date"].tolist()
    assert us_dates[-1] == "2026-11-25"
    stamp = "2026-11-27T08:04:00+09:00"
    observations = observations_for(us_dates, (*BASE_SYMBOLS, "KORU"))
    manifest = prepared_inputs(tmp_path, {"KOSPI": 1030, "KOSDAQ": 991}, asof="2026-11-26", received=stamp,
                               cutoff=stamp, operation_mode="morning", observations=observations)
    output = tmp_path / "publish"
    outcome = daily.run_prepared(manifest, output, now=stamp, operation_mode="morning")
    assert outcome["status"] == "published" and outcome["model_b_unavailable"] == {}
    b, c = payloads(output)
    assert b["target_date"] == c["target_date"] == "2026-11-27"
    assert c["status"] == "ready" and len(c["markets"]) == 2
    assert len(b["markets"]) == 2
    for row in b["markets"]:
        assert round(row["expected_index"], 2) != round(row["close"], 2)
        assert row["expected_index"] == pytest.approx(row["close"] * (1 + row["expected_return"]))
