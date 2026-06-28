#!/usr/bin/env python3
"""Collect macro/market-wide news from official RSS feeds into DuckDB.

Example cron (every 30 minutes):

    */30 * * * * cd /path/to/stock_pred && .venv/bin/python scripts/collect_market_news.py --config configs/market_news.yaml >> logs/market_news.log 2>&1
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.providers.market_news_feed import (
    MarketNewsFeedProvider,
    MarketNewsFeedSource,
    curated_articles_from_config,
    sources_from_config,
)
from roboquant.db import append_dedup_table, connect_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect official RSS macro/market news feeds.")
    parser.add_argument("--config", default="configs/market_news.yaml")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    sources = sources_from_config(config)
    collection = config.get("collection", {})

    if args.dry_run:
        print(f"config: {Path(args.config)}")
        print(f"database: {get_database_path(config)}")
        print(f"interval_minutes: {collection.get('interval_minutes', 30)}")
        for source in sources:
            status = "enabled" if source.enabled and source.feed_url else "disabled"
            print(
                "feed:",
                source.source,
                f"category={source.default_category}",
                f"status={status}",
                source.feed_url or "(no url)",
            )
        return

    conn = connect_database(get_database_path(config))
    provider = MarketNewsFeedProvider(timeout=float(collection.get("request_timeout_seconds", 15)))
    max_entries = int(collection.get("max_entries_per_feed", 50))
    all_rows = []
    try:
        for source in _enabled_sources(sources):
            try:
                frame = provider.fetch_entries(
                    [source],
                    config=config,
                    max_entries_per_feed=max_entries,
                )
                all_rows.append(frame)
                print(f"OK: {source.source} entries={len(frame)}")
            except Exception as exc:
                _record_failure(conn, source, exc)
                print(f"FAIL: {source.source}: {exc}")

        curated = curated_articles_from_config(config)
        if not curated.empty:
            all_rows.append(curated)
            print(f"OK: curated entries={len(curated)}")

        if all_rows:
            combined = pd.concat(all_rows, ignore_index=True)
        else:
            combined = pd.DataFrame()

        append_dedup_table(conn, "market_news_feed", combined, ["article_id"])
        print(f"market_news_feed rows upserted: {len(combined)}")
    finally:
        conn.close()


def _enabled_sources(sources: list[MarketNewsFeedSource]) -> list[MarketNewsFeedSource]:
    return [
        source
        for source in sources
        if source.enabled and str(source.feed_url or "").strip() and str(source.source or "").strip()
    ]


def _record_failure(conn, source: MarketNewsFeedSource, error: Exception) -> None:
    append_dedup_table(
        conn,
        "collection_failures",
        collection_failure_row(
            step="collect_market_news",
            source=str(source.source),
            target_date=datetime.now(UTC).date().isoformat(),
            error=error,
        ),
        ["collected_at", "step", "source", "error_message"],
    )


if __name__ == "__main__":
    main()
