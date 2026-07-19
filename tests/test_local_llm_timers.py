from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_local_llm_timer_install_dry_run_renders_all_three_jobs() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/install_local_llm_timers.py", "--install", "--enable", "--dry-run"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "roboquant-local-llm-close.timer" in result.stdout
    assert "roboquant-local-llm-premarket.timer" in result.stdout
    assert "roboquant-tomorrow-market-ml.timer" in result.stdout
    assert "systemctl --user daemon-reload" in result.stdout
