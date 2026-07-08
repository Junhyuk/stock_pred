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
from roboquant.data.providers.naver_news import (
    NaverNewsConfigurationError,
    NaverNewsProvider,
    queries_from_config,
)
from roboquant.data.providers.official_sources import (
    OfficialDataConfigurationError,
    collect_bigkinds_issues,
    collect_gdelt_news,
)
from roboquant.db import append_dedup_table, connect_database
from roboquant.global_market.regime import KST


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect official news sources for Top50 dashboard.")
    parser.add_argument("--config", default="configs/official_data_sources.yaml")
    parser.add_argument("--top50-config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--universe-rule", default="prediction_top_market_cap")
    parser.add_argument("--allow-missing-key", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    top50_config = load_config(args.top50_config)
    ensure_project_dirs(config)
    target_date = _parse_date(args.date)
    conn = connect_database(get_database_path(config))
    results = []
    try:
        results.append(_collect_naver(conn, config, top50_config, target_date, args.universe_rule, args.allow_missing_key))
        results.append(_collect_bigkinds(conn, config, target_date, args.allow_missing_key))
        results.append(_collect_gdelt(conn, config, target_date))
        print("; ".join(results))
    finally:
        conn.close()


def _collect_naver(conn, config: dict, top50_config: dict, target_date, universe_rule: str, allow_missing_key: bool) -> str:
    settings = config.get("official_news", {}).get("naver", {})
    if not bool(settings.get("enabled", True)):
        return "naver skipped disabled"
    items = _universe_items(conn, universe_rule)
    if not items:
        return "naver skipped no universe"
    template = str(settings.get("query_template") or top50_config.get("news", {}).get("query_template", "{name} 주가"))
    queries = queries_from_config(items, template)
    try:
        provider = NaverNewsProvider()
    except NaverNewsConfigurationError as exc:
        _record_failure(conn, "collect_official_news", "naver_search_api", exc, target_date)
        if allow_missing_key:
            return f"naver skipped missing key: {exc}"
        raise
    frame = provider.fetch_articles(
        queries,
        query_date=target_date,
        display=int(settings.get("display", 100)),
        max_pages=int(settings.get("max_pages", 3)),
        sort=str(settings.get("sort", "date")),
    )
    append_dedup_table(conn, "news_articles", frame, ["article_id"])
    return f"naver rows={len(frame)} queries={len(queries)}"


def _collect_bigkinds(conn, config: dict, target_date, allow_missing_key: bool) -> str:
    try:
        frame = collect_bigkinds_issues(config=config.get("official_news", {}), target_date=target_date)
    except OfficialDataConfigurationError as exc:
        _record_failure(conn, "collect_official_news", "bigkinds_issue_top10", exc, target_date)
        if allow_missing_key:
            return f"bigkinds skipped: {exc}"
        raise
    append_dedup_table(conn, "bigkinds_issue_daily", frame, ["issue_date", "period", "rank"])
    return f"bigkinds rows={len(frame)}"


def _collect_gdelt(conn, config: dict, target_date) -> str:
    try:
        frame = collect_gdelt_news(config=config.get("official_news", {}), query_date=target_date)
    except Exception as exc:
        _record_failure(conn, "collect_official_news", "gdelt_doc_api", exc, target_date)
        raise
    append_dedup_table(conn, "global_news_events", frame, ["event_id"])
    return f"gdelt rows={len(frame)}"


def _universe_items(conn, universe_rule: str) -> list[dict]:
    try:
        frame = conn.execute(
            """
            SELECT symbol, name
            FROM current_prediction_universe
            WHERE universe_rule = ?
              AND is_enabled = TRUE
            ORDER BY market, prediction_rank, symbol
            """,
            [universe_rule],
        ).fetchdf()
    except Exception:
        return []
    if frame.empty:
        return []
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    return frame.to_dict(orient="records")


def _record_failure(conn, step: str, source: str, error: Exception, target_date) -> None:
    append_dedup_table(
        conn,
        "collection_failures",
        collection_failure_row(step, source, error, target_date=target_date.isoformat()),
        ["collected_at", "step", "source", "error_message"],
    )


def _parse_date(value: str):
    if str(value).strip().lower() == "latest":
        return datetime.now(KST).date()
    return datetime.fromisoformat(str(value)).date()


if __name__ == "__main__":
    main()
