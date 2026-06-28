#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.providers.x_market_news import (
    XMarketNewsConfigurationError,
    XMarketNewsProvider,
    settings_from_config,
)
from roboquant.db import append_dedup_table, connect_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect X API v2 posts from approved market-news accounts.")
    parser.add_argument("--config", default="configs/x_market_news.yaml")
    parser.add_argument("--allow-missing-key", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _load_dotenv(ROOT / ".env")
    result = run_collection(
        args.config,
        allow_missing_key=args.allow_missing_key,
        dry_run=args.dry_run,
    )
    print(result)


def run_collection(
    config_path: str | Path,
    *,
    allow_missing_key: bool = False,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
) -> str:
    config = load_config(config_path)
    ensure_project_dirs(config)
    settings = settings_from_config(config)
    raw_settings = config.get("x_market_news") or {}
    enabled = bool(raw_settings.get("enabled", True))
    database = get_database_path(config)
    news_config = _merged_news_config(config)

    if dry_run:
        return (
            f"config: {config_path}\n"
            f"database: {database}\n"
            f"enabled: {enabled}\n"
            f"username: {settings.username}\n"
            f"source: {settings.source}\n"
            f"poll_interval_minutes: {raw_settings.get('poll_interval_minutes', 30)}"
        )
    if not enabled:
        return "x_market_news disabled; skipped without fake data."

    conn = connect_database(database)
    try:
        try:
            provider = XMarketNewsProvider(
                env=env,
                timeout=float(raw_settings.get("request_timeout_seconds", 15)),
            )
        except XMarketNewsConfigurationError as exc:
            _record_failure(conn, settings.source, exc)
            if allow_missing_key:
                return "X_BEARER_TOKEN not configured; skipped X market news without fake data."
            raise

        try:
            frame = provider.fetch_posts(settings=settings, config=news_config)
        except Exception as exc:
            _record_failure(conn, settings.source, exc)
            raise

        append_dedup_table(conn, "market_news_feed", frame, ["article_id"])
        return f"market_news_feed rows upserted from X: {len(frame)}"
    finally:
        conn.close()


def _merged_news_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("x_market_news") or {}
    base_path = raw.get("market_news_config")
    if not base_path:
        return config
    base = load_config(str(base_path))
    merged = dict(base)
    for key in ("category_keywords", "text_features"):
        if key in config:
            merged[key] = config[key]
    return merged


def _record_failure(conn, source: str, error: Exception) -> None:
    append_dedup_table(
        conn,
        "collection_failures",
        collection_failure_row(
            step="collect_x_market_news",
            source=source,
            target_date=datetime.now(UTC).date().isoformat(),
            error=error,
        ),
        ["collected_at", "step", "source", "error_message"],
    )


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    main()
