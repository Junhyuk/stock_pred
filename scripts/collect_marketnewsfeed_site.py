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
    render_marketnewsfeed_dom,
    rendered_headlines_to_frame,
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
                f"crawl_site_metadata: {settings.crawl_site_metadata}",
                f"crawl_rendered_headlines: {settings.crawl_rendered_headlines}",
                "mode: public_dom_shadow",
            ]
        )
    if not settings.enabled or not (
        settings.crawl_site_metadata or settings.crawl_rendered_headlines
    ):
        return "marketnewsfeed_site disabled; skipped without fake data."

    conn = connect_database(database)
    try:
        provider = MarketNewsFeedSiteProvider(timeout=float(raw.get("request_timeout_seconds", 10)))
        metadata_rows = 0
        try:
            frame = provider.fetch_metadata(settings=settings, config=config)
        except Exception as exc:
            _record_failure(conn, settings.source, exc)
            raise
        if not frame.empty:
            append_dedup_table(conn, "market_news_feed", frame, ["article_id"])
            metadata_rows = len(frame)

        headline_rows = 0
        headline_error = None
        if settings.crawl_rendered_headlines:
            try:
                rendered_html = render_marketnewsfeed_dom(
                    settings.site_url,
                    chrome_path=settings.chrome_path,
                    timeout_seconds=float(raw.get("render_timeout_seconds", 20)),
                )
                headlines = rendered_headlines_to_frame(
                    rendered_html,
                    base_url=settings.site_url,
                ).head(settings.max_headlines)
                if not headlines.empty:
                    append_dedup_table(
                        conn,
                        "marketnewsfeed_headlines",
                        headlines,
                        ["headline_id"],
                    )
                    headline_rows = len(headlines)
            except Exception as exc:
                headline_error = str(exc)
                _record_failure(conn, "marketnewsfeed_site_shadow", exc)
        return json.dumps(
            {
                "status": "ready" if headline_rows or not settings.crawl_rendered_headlines else "partial_ready",
                "source": settings.source,
                "metadata_rows": metadata_rows,
                "headline_rows": headline_rows,
                "headline_error": headline_error,
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
