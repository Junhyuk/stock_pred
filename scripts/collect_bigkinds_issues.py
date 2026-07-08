#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.providers.official_sources import (
    OfficialDataConfigurationError,
    collect_bigkinds_issues,
)
from roboquant.db import append_dedup_table, connect_database
from roboquant.global_market.regime import KST


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect BIGKinds issue Top10 from configured official provider.")
    parser.add_argument("--config", default="configs/official_data_sources.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--allow-missing-key", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    target_date = _parse_date(args.date)
    conn = connect_database(get_database_path(config))
    try:
        frame = collect_bigkinds_issues(config=config.get("official_news", {}), target_date=target_date)
        append_dedup_table(conn, "bigkinds_issue_daily", frame, ["issue_date", "period", "rank"])
        print(f"bigkinds_issue_daily rows: {len(frame)}")
    except OfficialDataConfigurationError as exc:
        _record_failure(conn, exc, target_date)
        if args.allow_missing_key:
            print(f"BIGKinds skipped without fake data: {exc}")
            return
        raise
    except Exception as exc:
        _record_failure(conn, exc, target_date)
        raise
    finally:
        conn.close()


def _record_failure(conn, error: Exception, target_date) -> None:
    append_dedup_table(
        conn,
        "collection_failures",
        collection_failure_row("collect_bigkinds_issues", "bigkinds_issue_top10", error, target_date=target_date.isoformat()),
        ["collected_at", "step", "source", "error_message"],
    )


def _parse_date(value: str):
    if str(value).strip().lower() == "latest":
        return datetime.now(KST).date()
    return datetime.fromisoformat(str(value)).date()


if __name__ == "__main__":
    main()
