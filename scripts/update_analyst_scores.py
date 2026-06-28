#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.loaders import load_analyst_report_outcomes, load_analyst_reports
from roboquant.db import append_dedup_table, connect_database, replace_table
from roboquant.features.analyst_features import compute_analyst_scores
from roboquant.features.consensus_features import compute_consensus_history


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update analyst reliability scores and consensus history.")
    parser.add_argument("--config", default="configs/poc.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))

    reports = load_analyst_reports(conn)
    outcomes = load_analyst_report_outcomes(conn)
    analyst_config = config.get("analyst", {})
    scores = compute_analyst_scores(
        reports,
        outcomes,
        min_reports=int(analyst_config.get("min_reports_for_score", 5)),
        recent_window_days=int(analyst_config.get("recent_window_days", 365)),
    )
    append_dedup_table(
        conn,
        "analyst_scores",
        scores,
        ["analyst_name", "broker_name", "as_of_date"],
    )
    consensus = compute_consensus_history(reports, scores)
    replace_table(conn, "consensus_history", consensus)
    print(f"analyst_scores rows updated: {len(scores)}")
    print(f"consensus_history rows: {len(consensus)}")


if __name__ == "__main__":
    main()
