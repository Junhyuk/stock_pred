from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

from roboquant.data.collectors.market_credit_balance import (
    MissingCreditBalanceConfig,
    fetch_market_credit_balance,
    normalize_market_credit_balance,
)
from roboquant.db import connect_database


def test_credit_balance_normalizes_fixture_and_computes_deltas() -> None:
    frame = normalize_market_credit_balance(
        {
            "response": {
                "body": {
                    "items": {
                        "item": [
                            {"basDt": "20260624", "mrktCtg": "KOSPI", "crdLoanBal": "1000"},
                            {"basDt": "20260625", "mrktCtg": "KOSPI", "crdLoanBal": "1010"},
                            {"basDt": "20260626", "mrktCtg": "KOSDAQ", "crdLoanBal": "510", "전일대비": "10"},
                        ]
                    }
                }
            }
        }
    )

    assert set(frame["market"]) == {"KOSPI", "KOSDAQ"}
    kospi_latest = frame[(frame["market"] == "KOSPI") & (frame["date"] == date(2026, 6, 25))].iloc[0]
    assert kospi_latest["credit_loan_delta_1d_krw"] == 10
    kosdaq_latest = frame[frame["market"] == "KOSDAQ"].iloc[0]
    assert kosdaq_latest["credit_loan_delta_1d_krw"] == 10


def test_credit_balance_requires_official_key_or_endpoint() -> None:
    try:
        fetch_market_credit_balance("2026-06-26", {"market_credit_balance": {}}, env={})
    except MissingCreditBalanceConfig as exc:
        assert "DATA_GO_KR_SERVICE_KEY" in str(exc)
    else:
        raise AssertionError("expected MissingCreditBalanceConfig")


def test_credit_balance_cli_records_failure_without_fake_data(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "credit.duckdb"
    config_path.write_text(
        f"""
paths:
  database: {db_path}
  report_dir: {tmp_path / "reports"}
  model_dir: {tmp_path / "models"}
market_credit_balance:
  enabled: true
  source: data_go_kr_kofia
  endpoint: ""
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/collect_market_credit_balance.py",
            "--config",
            str(config_path),
            "--date",
            "2026-06-26",
            "--allow-missing-key",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        text=True,
        capture_output=True,
    )
    conn = connect_database(db_path, read_only=True, initialize_schema=False)
    try:
        failures = conn.execute(
            "SELECT step, source, error_message FROM collection_failures"
        ).fetchall()
        rows = conn.execute("SELECT COUNT(*) FROM market_credit_balance_daily").fetchone()[0]
    finally:
        conn.close()

    assert "skipped market credit balance without fake data" in result.stdout
    assert rows == 0
    assert failures and failures[0][0] == "collect_market_credit_balance"
    assert failures[0][1] == "data_go_kr_kofia"
