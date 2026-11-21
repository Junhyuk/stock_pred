"""Forward-cohort operations for the premarket US shadow: ledger, dedup, rehearsal, receipts, scoring."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from roboquant.index_forecast import probability_research as pr

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/probability_shadow.yaml"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("run_probability_research_ops", ROOT / "scripts/run_probability_research.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(market="KOSPI", target="2026-11-23", asof="2026-11-20", p=0.6, up=0.55, card=None, **overrides):
    base = {"market": market, "status": "shadow_research", "rehearsal": False, "replay": False,
            "spec_hash": pr.shadow_spec_hash(), "p_up": p,
            "context": {"stage": "premarket_final", "asof_date": asof, "target_date": target,
                        "target_close_at": f"{target}T06:30:00+00:00"},
            "input_receipts": {"received_before_cutoff": True},
            "baselines": {"up_rate": {"p_up": up}},
            "incumbent_postclose_card": card or {"available": False}}
    base.update(overrides)
    return base


def test_config_enables_a_tracked_ledger_outside_docs():
    config = yaml.safe_load(CONFIG.read_text())
    assert config["shadow"]["enabled"] is True and config["shadow"]["production_weight"] == 0.0
    for key in ("ledger", "summary"):
        assert str(config["shadow"][key]).startswith("research/premarket_us_shadow/")
    assert config["forward_confirmation"]["primary_market"] == "KOSPI"
    assert config["forward_confirmation"]["decision_n"] == 252


def test_preregistration_copy_matches_the_frozen_hash():
    import hashlib
    payload = (ROOT / "research/premarket_us_shadow/preregistration.json").read_bytes()
    assert hashlib.sha256(payload).hexdigest() == "42f0d118cf6afa078a3b3150d5bb8f6d3e1d8a0eef50b5220856cfbc2ff89daf"


def test_rehearsal_never_writes_the_cohort_ledger(script):
    with pytest.raises(SystemExit, match="rehearsal"):
        script.main(["shadow", "--rehearsal"])
    official = ROOT / yaml.safe_load(CONFIG.read_text())["shadow"]["ledger"]
    with pytest.raises(SystemExit, match="rehearsal"):
        script.main(["shadow", "--rehearsal", "--ledger", str(official)])
    with pytest.raises(SystemExit, match="actual clock"):
        script.main(["shadow", "--rehearsal", "--ledger", "/tmp/x.jsonl", "--now", "2026-11-23T08:10:00+09:00"])


def test_only_actual_in_window_frozen_spec_rows_are_scoreable(script):
    assert script.is_scoreable(row())
    assert not script.is_scoreable(row(rehearsal=True))
    assert not script.is_scoreable(row(replay=True))
    assert not script.is_scoreable(row(status="blocked"))
    assert not script.is_scoreable(row(spec_hash="0" * 64))
    assert not script.is_scoreable(row(input_receipts={"received_before_cutoff": False}))
    late = row()
    late["context"]["stage"] = "preliminary"
    assert not script.is_scoreable(late)


def test_dedup_skips_only_a_valid_issue_for_the_same_market_and_target(script):
    rows = [row(), row(market="KOSDAQ", status="blocked")]
    assert script.already_issued(rows, "KOSPI", "2026-11-23")
    assert not script.already_issued(rows, "KOSPI", "2026-11-24")
    assert not script.already_issued(rows, "KOSDAQ", "2026-11-23")


def test_incumbent_card_requires_the_same_target(script, tmp_path):
    path = tmp_path / "card.json"
    path.write_text(json.dumps({"issuance_protocol": "after_close_daily_v1", "markets": [
        {"market": "KOSPI", "forecast_id": "f1", "forecast": {"target_date": "2026-11-23", "p_up": 0.58, "status": "rejected"}}]}))
    card = script.incumbent_card(path, "KOSPI", "2026-11-23")
    assert card["available"] and card["p_up"] == pytest.approx(0.58) and card["forecast_id"] == "f1"
    assert script.incumbent_card(path, "KOSPI", "2026-11-24")["reason"] == "incumbent_target_mismatch"
    assert script.incumbent_card(path, "KOSDAQ", "2026-11-23")["reason"] == "incumbent_market_missing"
    assert script.incumbent_card(tmp_path / "missing.json", "KOSPI", "2026-11-23")["available"] is False


def test_up_rate_uses_only_labels_matured_by_the_cutoff(script):
    days = pd.bdate_range("2026-01-05", periods=6).strftime("%Y-%m-%d").tolist()
    frame = pd.DataFrame({"asof_date": days[:-1], "target_date": days[1:],
                          "label_return": [0.01, -0.01, 0.02, 0.03, 0.04]})
    frame["asof_close_at"] = [f"{d}T06:30:00+00:00" for d in frame["asof_date"]]
    frame["target_close_at"] = [f"{d}T06:30:00+00:00" for d in frame["target_date"]]
    frame["label_available_at"] = frame["target_close_at"]
    frame["prediction_cutoff"] = frame["asof_close_at"]
    # After the 3rd label matures (06:30 UTC) and before the 4th: three known labels, two up.
    result = script.up_rate_at_cutoff(frame, days[-1], f"{days[3]}T07:00:00+00:00")
    assert script.up_rate_at_cutoff(frame, days[-1], f"{days[3]}T06:00:00+00:00")["known_label_n"] == 2
    assert result == {"p_up": pytest.approx(2 / 3), "known_label_n": 3}


def test_input_receipts_reject_any_input_seen_after_the_cutoff(script):
    prices = pd.DataFrame({"session_date": ["2026-11-20"], "ingested_at": ["2026-11-22T22:40:00+00:00"]})
    snapshot = {s: {"first_seen_at": "2026-11-22T22:41:00+00:00"} for s in ("^GSPC", "^IXIC", "^SOX", "^VIX")}
    ok = script.input_receipts(prices, "2026-11-20", snapshot, "2026-11-22T23:00:00+00:00")
    assert ok["received_before_cutoff"] is True
    assert script.input_receipts(prices, "2026-11-20", snapshot, "2026-11-22T22:40:30+00:00")["received_before_cutoff"] is False
    partial = dict(list(snapshot.items())[:3])
    assert script.input_receipts(prices, "2026-11-20", partial, "2026-11-22T23:00:00+00:00")["received_before_cutoff"] is False


def _cohort(n, seed=3):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2026-11-20", periods=n + 1).strftime("%Y-%m-%d").tolist()
    closes, rows = {}, []
    level = 100.0
    closes[("KOSPI", days[0])] = level
    for i in range(n):
        up = bool(rng.random() < 0.55)
        level *= 1.01 if up else 0.99
        closes[("KOSPI", days[i + 1])] = level
        p = 0.7 if up else 0.4  # a skilful shadow
        rows.append(row(asof=days[i], target=days[i + 1], p=p, up=0.55,
                        card={"available": True, "p_up": 0.57}))
    return rows, closes


def test_scoring_ignores_unmatured_rehearsal_and_duplicate_rows(script):
    config = yaml.safe_load(CONFIG.read_text())
    rows, closes = _cohort(5)
    extra = [row(asof=rows[0]["context"]["asof_date"], target=rows[0]["context"]["target_date"], p=0.01),  # duplicate
             row(asof="2027-01-04", target="2027-01-05", p=0.9),                                           # unmatured
             row(rehearsal=True, p=0.99)]
    summary = script.score_ledger(rows + extra, closes, config, now="2027-01-10T00:00:00+00:00")
    kospi = summary["markets"]["KOSPI"]
    assert kospi["issued"] == 6 and kospi["matured"] == 5 and kospi["status"] == "accumulating"
    y = np.array([closes[("KOSPI", r["context"]["target_date"])] > closes[("KOSPI", r["context"]["asof_date"])] for r in rows], float)
    p = np.array([r["p_up"] for r in rows])
    assert kospi["shadow"]["brier"] == pytest.approx(np.mean((p - y) ** 2))
    assert kospi["vs_incumbent_postclose"]["n"] == 5
    assert summary["markets"]["KOSDAQ"]["role"] == "monitor_only" and summary["production_weight"] == 0.0


def test_interim_and_decision_states_follow_the_preregistered_rule(script):
    config = yaml.safe_load(CONFIG.read_text())
    config["forward_confirmation"]["bootstrap"] = {"samples": 50, "block_days": 5, "random_state": 42}
    rows, closes = _cohort(60)
    interim = script.score_ledger(rows, closes, config, now="2027-12-31T00:00:00+00:00")["markets"]["KOSPI"]
    assert interim["status"] == "interim_safety_passed_accumulating"
    harmful = [{**r, "p_up": 1 - r["p_up"]} for r in rows]
    assert script.score_ledger(harmful, closes, config, now="2027-12-31T00:00:00+00:00")["markets"]["KOSPI"]["status"] == "stopped_for_harm"
    rows, closes = _cohort(252)
    decision = script.score_ledger(rows, closes, config, now="2028-12-31T00:00:00+00:00")["markets"]["KOSPI"]
    assert set(decision["decision_checks"]) == {"brier_vs_up_rate_upper_below_0", "brier_vs_incumbent_upper_below_0",
                                                "log_loss_not_worse_than_up_rate", "ece10_at_most_0.05"}
    assert decision["status"] in {"passed_pending_independent_review", "failed"}
    assert decision["status"] == ("passed_pending_independent_review" if all(decision["decision_checks"].values()) else "failed")


def test_evaluate_cli_writes_a_summary_outside_docs(script, tmp_path):
    rows, closes = _cohort(3)
    db = tmp_path / "index.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE index_eod (index_code TEXT, session_date TEXT, close REAL)")
        conn.executemany("INSERT INTO index_eod VALUES (?, ?, ?)", [(m, d, c) for (m, d), c in closes.items()])
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    output = tmp_path / "summary.json"
    script.main(["evaluate", "--sqlite", str(db), "--ledger", str(ledger), "--output", str(output),
                 "--now", "2027-01-10T00:00:00+00:00"])
    summary = json.loads(output.read_text())
    assert summary["markets"]["KOSPI"]["matured"] == 3 and summary["ledger_sha256"]
    with pytest.raises(SystemExit):
        script.main(["evaluate", "--sqlite", str(db), "--ledger", str(ledger),
                     "--output", str(ROOT / "docs/data/should_not_exist.json")])


def test_workflow_commits_only_in_live_mode_with_identity_and_ledger_guard():
    workflow = yaml.safe_load((ROOT / ".github/workflows/premarket-us-shadow.yml").read_text())
    job = workflow["jobs"]["shadow"]
    assert workflow["permissions"] == {"contents": "read"} and job["permissions"] == {"contents": "write"}
    assert [c["cron"] for c in workflow[True]["schedule"]] == ["35 22 * * 0-4", "50 22 * * 0-4"]
    steps = {s["name"]: s for s in job["steps"]}
    commit = steps["Commit the forward-cohort ledger"]
    assert "env.MODE == 'live'" in commit["if"]
    script = commit["run"]
    # Identity is configured before any commit or rebase in the retry loop.
    assert script.index("git config user.email") < script.index("git commit") < script.index("git pull --rebase")
    assert "no new ledger rows" in script and "append-only" in script and "docs/" not in script
    shadow = steps["Shadow forecast"]["run"]
    assert "--rehearsal" in shadow and "rehearsal-ledger.jsonl" in shadow
    assert "second=30" in steps["Wait for the actual 08:00 KST cutoff"]["run"]
