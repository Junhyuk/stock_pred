#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.analyst.importer import import_analyst_sources
from roboquant.data.loaders import load_symbols
from roboquant.db import append_dedup_table, connect_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import analyst report CSV/HTML fixtures.")
    parser.add_argument("--config", default="configs/analyst_sources.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_config_path = (ROOT / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)
    with source_config_path.open("r", encoding="utf-8") as file:
        source_config = yaml.safe_load(file) or {}

    project_config_path = source_config.get("config", "configs/poc.yaml")
    project_config = load_config(ROOT / project_config_path if not Path(project_config_path).is_absolute() else project_config_path)
    ensure_project_dirs(project_config)
    conn = connect_database(get_database_path(project_config))
    symbols = load_symbols(conn)

    reports, failures = import_analyst_sources(source_config, symbols)
    append_dedup_table(conn, "analyst_reports", reports, ["report_id"])
    append_dedup_table(
        conn,
        "collection_failures",
        failures,
        ["collected_at", "step", "source", "target_date", "error_message"],
    )
    print(f"analyst_reports imported rows: {len(reports)}")
    print(f"analyst import failures: {len(failures)}")


if __name__ == "__main__":
    main()
