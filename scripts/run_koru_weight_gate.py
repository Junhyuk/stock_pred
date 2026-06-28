#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import append_dedup_table, connect_database
from roboquant.koru import build_koru_weight_decision_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply KORU overlay weight gate from ablation metrics.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--baseline-metrics", default=None, help="Optional JSON metrics by horizon.")
    parser.add_argument("--enhanced-metrics", default=None, help="Optional JSON metrics by horizon.")
    parser.add_argument("--date", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    baseline = _load_metrics(args.baseline_metrics)
    enhanced = _load_metrics(args.enhanced_metrics)
    decision_date = date.fromisoformat(args.date) if args.date else None
    rows = build_koru_weight_decision_rows(baseline, enhanced, decision_date=decision_date)
    conn = connect_database(get_database_path(config))
    try:
        append_dedup_table(conn, "koru_weight_decisions", rows, ["decision_date", "horizon"])
    finally:
        conn.close()
    print(rows[["decision_date", "horizon", "decision", "overlay_weight"]].to_string(index=False))


def _load_metrics(path: str | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("metrics JSON must be an object keyed by horizon")
    return {str(key): dict(value or {}) for key, value in payload.items()}


if __name__ == "__main__":
    main()
