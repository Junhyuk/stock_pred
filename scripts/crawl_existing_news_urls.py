#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.providers.news_url_crawler import (
    NewsUrlCandidate,
    NewsUrlCrawler,
    candidates_frame,
    url_hash,
)
from roboquant.db import append_dedup_table, connect_database, table_exists


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl metadata and short excerpts from existing stored news URLs.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--market-news-config", default="configs/market_news.yaml")
    parser.add_argument("--source", choices=["all", "market_news_feed", "news_articles", "config"], default="all")
    parser.add_argument("--domain", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--refresh-hours", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--respect-robots", dest="respect_robots", action="store_true", default=True)
    parser.add_argument("--no-respect-robots", dest="respect_robots", action="store_false")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    market_news_config = load_config(args.market_news_config)
    ensure_project_dirs(config)
    crawl_cfg = config.get("news_url_crawl", {}) or load_config("configs/official_data_sources.yaml").get("news_url_crawl", {})
    limit = int(args.limit if args.limit is not None else crawl_cfg.get("limit", 100))
    refresh_hours = int(args.refresh_hours if args.refresh_hours is not None else crawl_cfg.get("refresh_hours", 24))
    conn = connect_database(get_database_path(config))
    try:
        candidates = collect_candidates(conn, market_news_config, source=args.source, domain=args.domain, limit=limit, refresh_hours=refresh_hours)
        if args.dry_run:
            for item in candidates:
                print(f"{item.source_table}\t{item.source_article_id}\t{item.source_url}")
            print(f"candidate_count={len(candidates)}")
            return
        crawler = NewsUrlCrawler(
            respect_robots=bool(args.respect_robots),
            timeout=float(crawl_cfg.get("timeout_seconds", 10)),
            max_bytes=int(crawl_cfg.get("max_bytes", 1_048_576)),
            excerpt_chars=int(crawl_cfg.get("excerpt_chars", 700)),
            sleep_seconds=float(crawl_cfg.get("sleep_seconds", 0.5)),
        )
        rows = [crawler.crawl(candidate) for candidate in candidates]
        frame = candidates_frame(rows)
        append_dedup_table(conn, "news_url_crawl_cache", frame, ["url_hash"])
        print(f"news_url_crawl_cache rows upserted: {len(frame)}")
    except Exception as exc:
        append_dedup_table(
            conn,
            "collection_failures",
            collection_failure_row("crawl_existing_news_urls", "news_url_crawl", exc),
            ["collected_at", "step", "source", "error_message"],
        )
        raise
    finally:
        conn.close()


def collect_candidates(
    conn,
    market_news_config: dict,
    *,
    source: str = "all",
    domain: str | None = None,
    limit: int = 100,
    refresh_hours: int = 24,
) -> list[NewsUrlCandidate]:
    rows: list[NewsUrlCandidate] = []
    if source in {"all", "market_news_feed"}:
        rows.extend(_market_news_candidates(conn))
    if source in {"all", "news_articles"}:
        rows.extend(_news_article_candidates(conn))
    if source in {"all", "config"}:
        rows.extend(_config_candidates(market_news_config))
    rows = _dedupe(rows)
    if domain:
        rows = [row for row in rows if domain.lower() in row.source_url.lower()]
    rows = _drop_recent(conn, rows, refresh_hours=refresh_hours)
    return rows[: max(1, int(limit))]


def _market_news_candidates(conn) -> list[NewsUrlCandidate]:
    if not table_exists(conn, "market_news_feed"):
        return []
    frame = conn.execute(
        """
        SELECT article_id, link
        FROM market_news_feed
        WHERE link IS NOT NULL AND link <> ''
        ORDER BY pub_date DESC NULLS LAST, collected_at DESC NULLS LAST
        """
    ).fetchdf()
    return [
        NewsUrlCandidate("market_news_feed", str(row["article_id"]), str(row["link"]))
        for row in frame.to_dict(orient="records")
    ]


def _news_article_candidates(conn) -> list[NewsUrlCandidate]:
    if not table_exists(conn, "news_articles"):
        return []
    frame = conn.execute(
        """
        SELECT article_id, originallink, link
        FROM news_articles
        WHERE (originallink IS NOT NULL AND originallink <> '')
           OR (link IS NOT NULL AND link <> '')
        ORDER BY pub_date DESC NULLS LAST, collected_at DESC NULLS LAST
        """
    ).fetchdf()
    rows = []
    for row in frame.to_dict(orient="records"):
        article_id = str(row["article_id"])
        for column in ("originallink", "link"):
            value = row.get(column)
            if pd.notna(value) and str(value).strip():
                rows.append(NewsUrlCandidate("news_articles", article_id, str(value)))
    return rows


def _config_candidates(config: dict) -> list[NewsUrlCandidate]:
    rows = []
    for section in ("latest_market_context", "curated_articles"):
        for item in config.get(section, []) or []:
            link = str(item.get("link") or "").strip()
            if link:
                rows.append(NewsUrlCandidate(f"config:{section}", _hash(section, item.get("title"), link), link))
    return rows


def _drop_recent(conn, rows: list[NewsUrlCandidate], *, refresh_hours: int) -> list[NewsUrlCandidate]:
    if not table_exists(conn, "news_url_crawl_cache"):
        return rows
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=max(0, int(refresh_hours)))
    frame = conn.execute(
        """
        SELECT url_hash
        FROM news_url_crawl_cache
        WHERE fetched_at >= ?
        """,
        [cutoff],
    ).fetchdf()
    recent = set(frame["url_hash"].astype(str).tolist())
    return [row for row in rows if url_hash(row.source_url) not in recent]


def _dedupe(rows: list[NewsUrlCandidate]) -> list[NewsUrlCandidate]:
    seen: set[str] = set()
    output = []
    for row in rows:
        key = url_hash(row.source_url)
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def _hash(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()


if __name__ == "__main__":
    main()
