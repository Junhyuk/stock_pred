from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from roboquant.db import connect_database

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "collect_dart_disclosures.py"
SPEC = importlib.util.spec_from_file_location("collect_dart_disclosures", SCRIPT_PATH)
assert SPEC is not None
collector = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


def test_dart_collection_skips_when_universe_is_empty(monkeypatch, capsys, tmp_path) -> None:
    db_path = tmp_path / "collector.duckdb"
    conn = connect_database(db_path)
    conn.close()

    config_path = tmp_path / "official.yaml"
    config_path.write_text(
        f"""
paths:
  database: {db_path}
official_news:
  dart:
    enabled: true
    endpoint: "https://dart.example/list"
    api_key_env: DART_API_KEY
    days: 7
""",
        encoding="utf-8",
    )

    called = {"provider": False}

    def _should_not_call_provider(**kwargs):
        called["provider"] = True
        raise AssertionError("provider should not be called when universe is empty")

    monkeypatch.setattr(collector, "collect_dart_disclosures", _should_not_call_provider)
    monkeypatch.setattr(collector, "_load_dotenv", lambda path: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["collect_dart_disclosures.py", "--config", str(config_path)],
    )

    collector.main()

    output = capsys.readouterr().out
    assert "current_prediction_universe is empty" in output
    assert called["provider"] is False


def test_relation_exists_accepts_views(tmp_path) -> None:
    db_path = tmp_path / "relation.duckdb"
    conn = connect_database(db_path)
    exists = collector._relation_exists(conn, "current_prediction_universe")
    conn.close()

    assert exists is True
