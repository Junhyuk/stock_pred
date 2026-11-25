import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from roboquant.models import bc_local_pipeline as lp
from roboquant.models import bc_premarket as pm

ROOT = Path(__file__).resolve().parents[1]


def sessions(*days):
    return pd.DataFrame({
        "date": list(days),
        "open_at": [pd.Timestamp(f"{d}T09:00:00+09:00").tz_convert("UTC") for d in days],
        "close_at": [pd.Timestamp(f"{d}T15:30:00+09:00").tz_convert("UTC") for d in days],
    })


WEEK = sessions("2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26")


def load_pipeline():
    spec = importlib.util.spec_from_file_location("run_local_bc_pipeline", ROOT / "scripts" / "run_local_bc_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evening_waits_an_hour_after_the_close():
    early = lp.evening_window(pd.Timestamp("2026-11-24T16:00:00+09:00"), WEEK)
    assert early["asof_date"] == "2026-11-23" and early["target_date"] == "2026-11-24"
    assert early["note"]
    late = lp.evening_window(pd.Timestamp("2026-11-24T22:00:00+09:00"), WEEK)
    assert (late["asof_date"], late["target_date"]) == ("2026-11-24", "2026-11-25")


def test_evening_on_a_weekend_targets_the_next_session():
    plan = lp.evening_window(pd.Timestamp("2026-11-21T22:00:00+09:00"), WEEK)
    assert (plan["asof_date"], plan["target_date"]) == ("2026-11-20", "2026-11-23")


def test_clock_must_be_timezone_aware():
    with pytest.raises(ValueError):
        lp.evening_window(pd.Timestamp("2026-11-24T22:00:00"), WEEK)


def test_premarket_window_is_between_us_close_buffer_and_the_open():
    assert lp.premarket_window(pd.Timestamp("2026-11-25T06:00:00+09:00"), WEEK)["status"] == "not_ready"
    ready = lp.premarket_window(pd.Timestamp("2026-11-25T07:50:00+09:00"), WEEK)
    assert ready["status"] == "ready"
    assert (ready["asof_date"], ready["target_date"]) == ("2026-11-24", "2026-11-25")
    assert ready["prediction_cutoff"] == "2026-11-25T07:50:00+09:00"
    assert lp.premarket_window(pd.Timestamp("2026-11-25T09:00:00+09:00"), WEEK)["status"] == "missed"
    assert lp.premarket_window(pd.Timestamp("2026-11-22T07:50:00+09:00"), WEEK)["status"] == "not_ready"


def test_labels_after_the_asof_session_are_reported():
    assert lp.labels_before(["2026-11-20", "2026-11-23"], "2026-11-24") == []
    assert lp.labels_before(["2026-11-24", "2026-11-25"], "2026-11-24") == ["2026-11-25"]


def good_row(**extra):
    row = {"market": "KOSPI", "asof_date": "2026-11-24", "target_date": "2026-11-25", "close": 5000.0,
           "p_up": 0.4, "p_down": 0.6, "expected_return": 0.01, "expected_index": 5050.0}
    row.update(extra)
    return row


def test_payload_checks_catch_what_the_page_renderer_would_reject():
    payload = {"schema_version": "model_c_forecast_v1", "production_weight": 0.0, "markets": [good_row()]}
    assert lp.check_forecast_payload(payload, "model_c_forecast_v1", require_expected=True) == []
    bad = {"schema_version": "model_c_forecast_v1", "production_weight": 0.5,
           "markets": [good_row(target_date="2026-11-24", p_down=0.5, expected_index=1.0)]}
    problems = " ".join(lp.check_forecast_payload(bad, "model_c_forecast_v1", require_expected=True))
    for needle in ("production_weight", "target_date", "sum to 1", "expected_index"):
        assert needle in problems
    premarket = {**payload, "premarket_markets": [{"market": "KOSPI", "issue_stage": "evening", "production_weight": 0}]}
    assert lp.check_forecast_payload(premarket, "model_c_forecast_v1", require_expected=True)


def test_published_files_pass_the_checks_and_match_the_renderers():
    b = json.loads((ROOT / "docs/data/model_b_forecast.json").read_text())
    c = json.loads((ROOT / "docs/data/model_c_forecast.json").read_text())
    assert lp.check_forecast_payload(b, "model_b_forecast_v1", require_expected=True) == []
    assert lp.check_forecast_payload(c, "model_c_forecast_v1", require_expected=True) == []
    assert "'model_b_forecast_v1'" in (ROOT / "docs/assets/model-b.js").read_text()
    script_c = (ROOT / "docs/assets/model-c.js").read_text()
    assert "'model_c_forecast_v1'" in script_c and "production_weight !== 0" in script_c


def test_model_c_three_way_probabilities_are_checked_without_a_point_forecast():
    row = good_row(p_up=0.1, p_down=0.2, p_normal=0.7)
    del row["expected_return"], row["expected_index"]
    payload = {
        "schema_version": "model_c_forecast_v1",
        "probability_mode": "three_way",
        "production_weight": 0,
        "markets": [row],
    }
    assert lp.check_forecast_payload(payload, "model_c_forecast_v1", require_expected=True) == []
    row["p_normal"] = 0.8
    assert lp.check_forecast_payload(payload, "model_c_forecast_v1", require_expected=True)


def test_ledger_is_hash_chained_append_only_and_refuses_a_second_issue(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    first = lp.append_ledger(ledger, {"variant": "v", "market": "KOSPI", "target_date": "2026-11-25", "value": 0.1})
    second = lp.append_ledger(ledger, {"variant": "v", "market": "KOSDAQ", "target_date": "2026-11-25", "value": 0.2})
    assert first["prev_row_sha256"] is None and second["prev_row_sha256"] == first["row_sha256"]
    assert lp.verify_ledger(ledger) == []
    with pytest.raises(ValueError):
        lp.append_ledger(ledger, {"variant": "v", "market": "KOSPI", "target_date": "2026-11-25", "value": 0.9})
    text = ledger.read_text().replace('"value":0.1', '"value":0.5').replace('"value": 0.1', '"value": 0.5')
    ledger.write_text(text)
    assert lp.verify_ledger(ledger)


def test_backup_restore_and_prune(tmp_path):
    root = tmp_path / "root"
    (root / "models").mkdir(parents=True)
    (root / "models/a.pt").write_bytes(b"original")
    hashes = lp.backup_files(root, tmp_path / "b" / "20261125T220000_evening", ["models/a.pt", "models/missing.pt"])
    assert list(hashes) == ["models/a.pt"]
    (root / "models/a.pt").write_bytes(b"changed")
    lp.restore_files(root, tmp_path / "b" / "20261125T220000_evening", hashes)
    assert (root / "models/a.pt").read_bytes() == b"original"
    for stamp in ("20261126T220000_evening", "20261127T220000_evening"):
        lp.backup_files(root, tmp_path / "b" / stamp, ["models/a.pt"])
    assert lp.prune_backups(tmp_path / "b", keep=2) == ["20261125T220000_evening"]
    with pytest.raises(ValueError):
        lp.prune_backups(tmp_path / "b", keep=0)


def test_staleness_counts_sessions_since_the_last_observation():
    days = ["2026-10-30", "2026-11-02", "2026-11-23", "2026-11-24"]
    out = lp.staleness({"JP_JGB_10Y": "2026-10-30", "KR_KTB_3Y": "2026-11-24", "X": None}, "2026-11-24", days)
    assert out == {"JP_JGB_10Y": 3, "KR_KTB_3Y": 0, "X": None}


def test_premarket_variants_fit_on_finite_rows_only():
    rng = np.random.default_rng(0)
    x = rng.standard_normal((200, 30))
    r = 0.01 * x[:, 26] + 0.001 * rng.standard_normal(200)
    ridge = pm.fit_predict("WB7_bclose_pre_ridgecv_f24us", x, r, x[:2])
    assert len(ridge["value"]) == 2 and ridge["alpha"] in pm.RIDGE_ALPHAS
    prob = pm.fit_predict("K4_c_pre_logit_absw_f24us", x, r, x[:1])["value"][0]
    assert 0.0 < prob < 1.0
    x_bad = x.copy()
    x_bad[0, 0] = np.nan
    with pytest.raises(ValueError):
        pm.fit_predict("WB3_b_pre_logit_f24us", x_bad, r, x[:1])


def test_dry_run_writes_nothing_and_clock_override_needs_dry_run(capsys):
    pipeline = load_pipeline()
    before = sorted(p.name for p in pipeline.RUN_ROOT.glob("*")) if pipeline.RUN_ROOT.exists() else []
    assert pipeline.main(["--stage", "premarket", "--dry-run", "--now", "2026-11-25T07:50:00+09:00"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["plan"]["target_date"] == "2026-11-25"
    after = sorted(p.name for p in pipeline.RUN_ROOT.glob("*")) if pipeline.RUN_ROOT.exists() else []
    assert before == after
    with pytest.raises(SystemExit):
        pipeline.main(["--stage", "evening", "--now", "2026-11-25T22:00:00+09:00"])
    with pytest.raises(SystemExit):
        pipeline.main(["--stage", "evening", "--dry-run", "--markets", "KOSPI"])


def test_no_workflow_references_the_local_pipeline():
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        for name in ("run_local_bc_pipeline", "run_bc_chronological_eval", "bc_premarket", "install_local_bc_timers"):
            assert name not in text, f"{path.name} references {name}"


def test_timer_installer_prints_by_default_and_installs_nothing(tmp_path):
    import subprocess
    import sys

    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, "scripts/install_local_bc_timers.py"], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout
    assert "OnCalendar=Mon..Fri *-*-* 22:00:00" in out and "OnCalendar=Mon..Fri *-*-* 07:40:00" in out
    assert "scripts/run_local_bc_pipeline.py --stage evening" in out
    assert "scripts/run_local_bc_pipeline.py --stage premarket" in out
    assert "__PROJECT_ROOT__" not in out and "__PYTHON__" not in out
    assert not (tmp_path / ".config").exists()
