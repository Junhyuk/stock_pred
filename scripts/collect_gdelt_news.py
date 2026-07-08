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
from roboquant.data.providers.official_sources import collect_gdelt_news
from roboquant.db import append_dedup_table, connect_database
from roboquant.global_market.regime import KST


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect global news metadata from GDELT DOC API.")
    parser.add_argument("--config", default="configs/official_data_sources.yaml")
    parser.add_argument("--date", default="latest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    target_date = _parse_date(args.date)
    conn = connect_database(get_database_path(config))
    try:
        frame = collect_gdelt_news(config=config.get("official_news", {}), query_date=target_date)
        append_dedup_table(conn, "global_news_events", frame, ["event_id"])
        print(f"global_news_events rows: {len(frame)}")
    except Exception as exc:
        append_dedup_table(
            conn,
            "collection_failures",
            collection_failure_row("collect_gdelt_news", "gdelt_doc_api", exc, target_date=target_date.isoformat()),
            ["collected_at", "step", "source", "error_message"],
        )
        raise
    finally:
        conn.close()


def _parse_date(value: str):
    if str(value).strip().lower() == "latest":
        return datetime.now(KST).date()
    return datetime.fromisoformat(str(value)).date()


if __name__ == "__main__":
    main()
