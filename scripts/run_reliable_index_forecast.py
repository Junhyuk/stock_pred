#!/usr/bin/env python3
"""Generate local, immutable next-session calibration research artifacts."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.index_forecast.reliable_inputs import utc_timestamp
from roboquant.index_forecast.reliable_pipeline import run_reliable_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default="data/processed/index_forecast.sqlite")
    parser.add_argument("--output-dir", default="docs/data")
    parser.add_argument("--config", default="configs/reliable_index_forecast.yaml")
    parser.add_argument("--now", help="Explicit ISO timestamp for retrospective replay; default freezes actual run time")
    parser.add_argument("--target-date", help="Must be the next verified KRX session")
    parser.add_argument("--position-evidence", help="Read-only JSON evidence artifact from the position subproject")
    parser.add_argument("--participant-sqlite", help="Read-only normalized participant subproject SQLite source")
    parser.add_argument("--participant-bridge", help="Versioned verified participant observations JSON; takes precedence over SQLite")
    parser.add_argument("--us-observations", help="Canonical completed-session US v2 CSV")
    parser.add_argument("--news-context", help="Current verified-event context JSON; never a historical feature archive")
    parser.add_argument("--prepared-manifest", help="Verified preparation manifest provenance")
    parser.add_argument("--economic-sqlite", help="Prepared economic snapshot; disables current Git history import")
    parser.add_argument("--fit-state-dir", help="Prepared model state and new anchor capture directory")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    if args.prepared_manifest:
        manifest = json.loads(Path(args.prepared_manifest).read_text())
        config["prepared_inputs"] = {key: manifest[key] for key in ("manifest_hash", "prepared_at", "kst_date", "code_hash")}
    if args.economic_sqlite:
        config["economic_calendar"] = {**config.get("economic_calendar", {}), "sqlite_path": args.economic_sqlite, "history_path": None}
    if args.fit_state_dir:
        config["fit_state_dir"] = args.fit_state_dir
    now: datetime | None = None
    if args.now:
        parsed = utc_timestamp(args.now)
        if parsed is None or str(parsed) == "NaT":
            parser.error("--now must be a valid ISO timestamp")
        now = parsed.to_pydatetime()
    evidence = None
    if args.position_evidence:
        evidence = json.loads(Path(args.position_evidence).read_text(encoding="utf-8"))
    result = run_reliable_pipeline(args.sqlite, args.output_dir, config=config, now=now,
                                    target_date=args.target_date, position_evidence=evidence,
                                    participant_sqlite=args.participant_sqlite, participant_bridge=args.participant_bridge,
                                    us_observations=args.us_observations, news_context=args.news_context)
    print(json.dumps({"status": result["status"], "run_id": result.get("run_id"),
                      "issued_at": result["issued_at"], "production_weight": 0,
                      "markets": [{"market": item["market"], "forecast_id": item["forecast_id"],
                                   "target_date": item["context"]["target_date"],
                                   "status": item["forecast"]["status"],
                                   "gate_reasons": item["forecast"]["gate_reasons"]}
                                  for item in result["markets"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
