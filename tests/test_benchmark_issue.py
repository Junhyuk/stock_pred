import json
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from roboquant.index_forecast.benchmark_context import (
    _as_known,
    add_context,
    issue_window,
    trigger_value,
)
from roboquant.index_forecast.benchmark_ledger import (
    acknowledge,
    forward_scores,
    init_ledger,
    issued_models,
    save_issue,
)
from roboquant.index_forecast.benchmark_publication import valid_benchmark_payload
from roboquant.index_forecast.benchmark_sources import SourceRights, import_benchmark_csv
from scripts import run_benchmark_forecasts as runner


def test_clock_holiday_and_duplicate_target_stage():
    saturday = issue_window("2026-11-28T08:06:00+09:00")
    monday = issue_window("2026-11-30T08:07:00+09:00")
    holiday = issue_window("2026-12-25T08:04:00+09:00")
    assert (saturday["target_date"], saturday["issue_kind"]) == ("2026-11-30", "preliminary")
    assert (monday["asof_date"], monday["target_date"], monday["issue_kind"]) == ("2026-11-27", "2026-11-30", "final")
    assert saturday["allowed"] and monday["allowed"] and holiday["issue_kind"] == "preliminary"
    assert not issue_window("2026-11-30T09:00:00+09:00")["allowed"]
    assert not issue_window("2026-11-29T08:15:00+09:00")["allowed"]


def test_trigger_uses_broad_indices_and_exact_inclusive_boundary_only():
    idx = pd.date_range("2025-04-07", periods=3)
    target = pd.DataFrame({"close": [100, 130, 185]}, index=idx)
    context = pd.DataFrame({"KOSPI": [100, 103, 100], "KOSDAQ": [100, 99, 99]}, index=idx)
    frame = add_context(target, context, "KOSPI200", {"KOSPI": -.03})
    assert pd.isna(frame.broad_trigger.iloc[0])
    assert list(frame.broad_trigger.iloc[1:]) == [1, 1]
    assert np.isnan(trigger_value({"KOSPI": .029999}))
    assert trigger_value({"KOSPI": .01, "KOSDAQ": -.029}) == 0


def test_receipts_are_compared_to_actual_cutoff_and_old_revisions_do_not_displace_newer_days():
    cols = ["observation_date", "value", "published_at", "received_at"]
    rows = pd.DataFrame([
        ["2026-11-25", 5, None, "2026-11-25T18:00:00+09:00"],
        ["2026-11-26", 6, None, "2026-11-27T08:06:00+09:00"],
        ["2026-11-24", 8, None, "2026-11-27T08:07:00+09:00"],
        ["2026-11-27", 12, "2026-11-30T13:00:00+09:00", "2026-11-27T08:07:00+09:00"],
    ], columns=cols)
    cuts = pd.to_datetime(["2026-11-27T08:05:00+09:00", "2026-11-27T08:08:00+09:00"], utc=True)
    assert list(_as_known(rows, cuts, pd.DatetimeIndex(["2026-11-26", "2026-11-27"]), max_age_days=5)) == [5, 6]


def test_missing_source_is_a_real_publishable_status_and_preserves_broad_outputs(tmp_path, monkeypatch):
    for key in ("KRX_OPENAPI_KEY", "KRX_AUTH_KEY", "DATA_GO_KR_SERVICE_KEY"):
        monkeypatch.delenv(key, raising=False)
    private = tmp_path / "private"
    out = tmp_path / "public/benchmark_forecasts.json"
    runner.collect(private, "2026-11-27")
    runner.publish(private, out, now="2026-11-28T08:08:00+09:00")
    data = json.loads(out.read_text())
    assert valid_benchmark_payload(data) and data["c_trigger"]["status"] == "unknown"
    for row in data["markets"].values():
        assert row["reason"] == "credential_missing" and row["history"]["count"] == 0
        assert row["models"]["A"]["status"] == "not_ready" and row["close"] is None


def test_confirmed_final_only_scored_once_and_outcome_not_rewritten():
    with sqlite3.connect(":memory:") as db:
        init_ledger(db)
        row = {"status": "ready", "model_version": "v1", "p_up": .65, "expected_index": 102}
        args = dict(target="2026-11-30", market="KOSPI200", model="B", anchor="2026-11-27", close=100, payload=row)
        for stage, day in (("preliminary", "2026-11-28"), ("final", "2026-11-30")):
            stamp = day+"T08:06:00+09:00"
            save_issue(db, **args, stage=stage, cutoff=stamp, issued=stamp, run_date=day)
        hist = pd.DataFrame([dict(close=101, source="unit", received_at="2026-11-30T16:00:00+09:00",
                                  payload_sha256="original")], index=pd.to_datetime(["2026-11-30"]))
        def scores(version="v1"):
            return forward_scores(db, "KOSPI200", hist, "2026-11-30", {"B": version})["B"]
        assert scores()["n"] == 0
        for stage, day in (("preliminary", "2026-11-28"), ("final", "2026-11-30")):
            sent = issued_models(db, args["target"], "KOSPI200", stage, day)
            receipt = {"target_date": args["target"], "issue_kind": stage, "markets": {"KOSPI200": {"models": sent}}}
            acknowledge(db, receipt)
            acknowledge(db, receipt)
            assert scores()["n"] == int(stage == "final")
        hist.loc[:, "close"] = 96
        assert scores()["hit_rate"] == 1 and scores()["close_mae"] == 1 and scores("v2")["n"] == 0


def test_source_permission_and_issued_forecast_share_exact_close(tmp_path, monkeypatch):
    private = tmp_path / "private"
    source = tmp_path / "licensed.csv"
    source.write_text("index_code,official_code,session_date,close\nKOSPI200,1028,2026-11-26,150\nKOSPI200,1028,2026-11-27,152\n")
    import_benchmark_csv(private / "observations.sqlite", source, index_code="KOSPI200", source="fixture",
        source_url="https://example.org/data", rights=SourceRights(True, True, False, "https://example.org/terms", "unit"),
        receipt_clock=lambda: datetime.fromisoformat("2026-11-28T08:03:00+09:00"))
    observed = []
    def mock_model(code, frame, **kwargs):
        observed.append(frame.close.iloc[-1])
        row = dict(status="ready", close=152, expected_index=155, p_up=.6, p_down=.4, model_version="v1",
                   evaluation={"model_version":"v1"}, interval={"level":.8,"low":147,"high":161})
        return {"models": {"A": row, "B": row, "C": {"status":"not_triggered"}}}
    monkeypatch.setattr(runner, "evaluate_and_predict", mock_model)
    output = tmp_path / "docs/benchmark_forecasts.json"
    runner.publish(private, output, now="2026-11-28T08:05:00+09:00")
    runner.publish(private, output, now="2026-11-28T08:10:00+09:00")
    assert observed == [152]
    data = json.loads(output.read_text())
    assert valid_benchmark_payload(data)
    target = data["markets"]["KOSPI200"]
    assert target["models"]["A"]["expected_index"] == 155
    assert "close" not in target["models"]["A"] and target["close"] is None and not target["chart"]
    assert target["models"]["A"]["cutoff"].endswith("23:05:00+00:00")
    assert data["markets"]["KOSDAQ150"]["status"] == "not_ready"


def test_allowlist_excludes_private_and_malformed_inputs():
    from tempfile import TemporaryDirectory

    from roboquant.index_forecast.automation_state import public_payload_paths
    with TemporaryDirectory() as folder:
        d = Path(folder)
        (d / "benchmark_forecasts.json").write_text('{"schema_version": 1,"markets":{}}')
        assert d / "benchmark_forecasts.json" not in public_payload_paths(d)


def test_four_markets_extreme_cpu_path(tmp_path):
    """Timed hosted smoke. All prices are generated fixtures confined to pytest's directory."""
    import time

    from test_bc_cpu_afterclose import prepared_inputs
    from test_bc_morning import BASE_SYMBOLS, observations_for

    from roboquant.index_forecast.reliable_inputs import session_schedule
    from roboquant.index_forecast.us_market_inputs import us_close_schedule
    from scripts import run_bc_cpu_afterclose

    stamp = "2026-11-27T08:04:00+09:00"
    sessions, _ = session_schedule("2020-08-01", "2026-11-26")
    us = us_close_schedule("2018-12-26", "2026-11-26")
    obs = observations_for(us.loc[us.symbol.eq("^GSPC"), "session_date"].tolist(), (*BASE_SYMBOLS, "KORU"))
    broad = tmp_path / "broad"
    private = tmp_path / "private"
    for j, (market, code) in enumerate([("KOSPI200", "1028"), ("KOSDAQ150", "2203")]):
        rng = np.random.default_rng(107 + j)
        prices = pd.DataFrame({"index_code": market, "official_code": code, "session_date": sessions.date,
                              "close": np.round(500 * np.exp(np.cumsum(rng.normal(0, .023, len(sessions)))), 2)})
        csv = tmp_path / f"{market}.csv"
        prices.to_csv(csv, index=False)
        import_benchmark_csv(private / "observations.sqlite", csv, index_code=market, source="timing_fixture",
            source_url="https://example.org/benchmark", rights=SourceRights(True, True, True, "https://example.org/terms", "fixture"),
            receipt_clock=lambda: datetime.fromisoformat(stamp))
    manifest = prepared_inputs(broad, {"KOSPI": 1030, "KOSDAQ": 991}, asof="2026-11-26", received=stamp,
                               cutoff=stamp, operation_mode="morning", observations=obs)
    output = tmp_path / "output"
    start = time.perf_counter()
    broad_result = run_bc_cpu_afterclose.run_prepared(manifest, output, now=stamp, operation_mode="morning")
    assert broad_result["status"] == "published", broad_result
    split = time.perf_counter()
    runner.publish(private, output / "benchmark_forecasts.json", broad_db=broad / "input.sqlite",
                   us_csv=broad / "us.csv", now=stamp)
    done = time.perf_counter()
    assert set(json.loads((output / "model_c_forecast.json").read_text())["trigger_returns"]) == {"KOSPI","KOSDAQ"}
    result = json.loads((output / "benchmark_forecasts.json").read_text())
    assert valid_benchmark_payload(result) and result["c_trigger"]["status"] == "triggered"
    for market, row in result["markets"].items():
        assert {name: v["status"] for name, v in row["models"].items()} == dict.fromkeys("ABC", "ready"), (market, row)
        assert len(row["chart"]["points"]) >= 235
    print(f"CPU_TIMING four markets, Model C triggered: broad B/C={split-start:.2f}s, "
          f"two benchmarks A/B/C cold={done-split:.2f}s, combined={done-start:.2f}s")
    assert done - start < 60
