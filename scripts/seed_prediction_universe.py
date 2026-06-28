#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import get_database_path, load_config
from roboquant.db import connect_database
from roboquant.universe.seed_loader import seed_prediction_universe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the v8 Top50 prediction universe.")
    parser.add_argument("--config", default="configs/universe_top50.yaml")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    universe = config["universe"]
    conn = connect_database(get_database_path(config))
    try:
        result = seed_prediction_universe(
            conn,
            snapshot_date=date.fromisoformat(str(universe["snapshot_date"])),
            universe_rule=str(universe["rule"]),
            provider=str(universe["provider"]),
            force=args.force,
            kospi_target=int(universe.get("kospi_target", 30)),
            kosdaq_target=int(universe.get("kosdaq_target", 20)),
        )
    finally:
        conn.close()

    print(
        "Seeded prediction universe: "
        f"date={result['snapshot_date']}, raw={result['raw_count']}, "
        f"prediction={result['prediction_count']}, "
        f"KOSPI={result['kospi_count']}, KOSDAQ={result['kosdaq_count']}"
    )


if __name__ == "__main__":
    main()
