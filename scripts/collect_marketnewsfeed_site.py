#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.providers.marketnewsfeed_site import (
    MarketNewsFeedSiteProvider,
    settings_from_config,
)
from roboquant.db import append_dedup_table, connect_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect safe MarketNewsFeed site metadata.")
    parser.add_argument("--config", default="configs/marketnewsfeed.yaml")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_collection(args.config, dry_run=args.dry_run)
    print(result)


def run_collection(config_path: str | Path, *, dry_run: bool = False) -> str:
    config = load_config(config_path)
    ensure_project_dirs(config)
    settings = settings_from_config(config)
    raw = config.get("marketnewsfeed") or {}
    database = get_database_path(config)

    if dry_run:
        return "\n".join(
            [
                f"config: {config_path}",
                f"database: {database}",
                f"enabled: {settings.enabled}",
                f"source: {settings.source}",
                f"site_url: {settings.site_url}",
                f"telegram_channel: {settings.telegram_channel}",
                f"x_username: {settings.x_username}",
                "mode: site_metadata_only",
            ]
        )
    if not settings.enabled or not settings.crawl_site_metadata:
        return "marketnewsfeed_site disabled; skipped without fake data."

    conn = connect_database(database)
    try:
        provider = MarketNewsFeedSiteProvider(timeout=float(raw.get("request_timeout_seconds", 10)))
        try:
            frame = provider.fetch_metadata(settings=settings, config=config)
        except Exception as exc:
            _record_failure(conn, settings.source, exc)
            raise
        append_dedup_table(conn, "market_news_feed", frame, ["article_id"])
        return json.dumps(
            {
                "status": "ready",
                "source": settings.source,
                "rows": len(frame),
                "site_url": settings.site_url,
            },
            ensure_ascii=False,
        )
    finally:
        conn.close()


def _record_failure(conn, source: str, error: Exception) -> None:
    append_dedup_table(
        conn,
        "collection_failures",
        collection_failure_row(
            step="collect_marketnewsfeed_site",
            source=source,
            target_date=datetime.now(UTC).date().isoformat(),
            error=error,
        ),
        ["collected_at", "step", "source", "error_message"],
    )


if __name__ == "__main__":
    main()
