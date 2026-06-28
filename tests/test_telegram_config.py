from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from roboquant.config import load_config


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "collect_telegram_signals.py"
SPEC = importlib.util.spec_from_file_location("collect_telegram_signals", SCRIPT_PATH)
assert SPEC is not None
collector = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


def test_telegram_config_includes_requested_channels() -> None:
    config = load_config("configs/telegram_signals.yaml")
    channels = {item["username"]: item for item in config.get("channels", [])}

    assert channels["sypark_strategy"]["display_name"] == "신영증권 박소연"
    assert channels["sypark_strategy"]["source_weight"] == 0.9
    assert channels["marketfeed"]["display_name"] == "Market News Feed"
    assert channels["marketfeed"]["source_weight"] == 0.8


def test_telegram_collection_skips_without_credentials(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.setattr(collector, "_load_dotenv", lambda path: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["collect_telegram_signals.py", "--config", "configs/telegram_signals.yaml"],
    )

    collector.main()

    output = capsys.readouterr().out
    assert "skipped Telegram collection without fake data" in output
