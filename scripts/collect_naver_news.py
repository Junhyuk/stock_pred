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
    NaverNewsQuery,
    queries_from_config,
)
from roboquant.db import append_dedup_table, connect_database
from roboquant.global_market.regime import KST


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Naver Search API news for focus stocks.")
    parser.add_argument("--config", default="configs/today_update.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--symbols", default=None, help="Comma-separated local stock symbols.")
    parser.add_argument("--universe-rule", default=None, help="Use current prediction universe when focus_stocks are absent.")
    parser.add_argument("--allow-missing-key", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    query_date = _parse_date(args.date)
    news_cfg = config.get("news", {})
    conn = connect_database(get_database_path(config))
    try:
        queries = _resolve_queries(conn, config, args.symbols, args.universe_rule)
        if not queries:
            print("No Naver news queries resolved; skipped without fake data.")
            return
        try:
            provider = NaverNewsProvider()
        except NaverNewsConfigurationError as exc:
            append_dedup_table(
                conn,
                "collection_failures",
                collection_failure_row(
                    step="collect_naver_news",
                    source="naver_search_api",
                    target_date=query_date.isoformat(),
                    error=exc,
                ),
                ["collected_at", "step", "source", "error_message"],
            )
            if args.allow_missing_key:
                print("NAVER_CLIENT_ID/SECRET not configured; skipped Naver news without fake data.")
                return
            raise

        frame = provider.fetch_articles(
            queries,
            query_date=query_date,
            display=int(news_cfg.get("display", 10)),
            sort=str(news_cfg.get("sort", "date")),
        )
        append_dedup_table(conn, "news_articles", frame, ["article_id"])
        print(f"news_articles rows upserted: {len(frame)}")
    except Exception as exc:
        append_dedup_table(
            conn,
            "collection_failures",
            collection_failure_row(
                step="collect_naver_news",
                source="naver_search_api",
                target_date=query_date.isoformat(),
                error=exc,
            ),
            ["collected_at", "step", "source", "error_message"],
        )
        raise
    finally:
        conn.close()


def _resolve_queries(conn, config: dict, override: str | None, universe_rule: str | None) -> list[NaverNewsQuery]:
    focus = config.get("focus_stocks", [])
    by_symbol = {str(item["symbol"]).zfill(6): item for item in focus}
    if override:
        items = [
            by_symbol.get(symbol.strip().zfill(6), _symbol_item(conn, symbol.strip().zfill(6)))
            for symbol in override.split(",")
            if symbol.strip()
        ]
    else:
        items = focus or _universe_items(conn, universe_rule)
    template = str(config.get("news", {}).get("query_template", "{name} 주가"))
    return queries_from_config(items, template)


def _symbol_item(conn, symbol: str) -> dict:
    frame = conn.execute(
        """
        SELECT symbol, name
        FROM symbols
        WHERE symbol = ?
        LIMIT 1
        """,
        [str(symbol).zfill(6)],
    ).fetchdf()
    if frame.empty:
        return {"symbol": str(symbol).zfill(6)}
    return frame.iloc[0].to_dict()


def _universe_items(conn, universe_rule: str | None) -> list[dict]:
    if not universe_rule:
        return []
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


def _parse_date(value: str):
    if str(value).strip().lower() == "latest":
        return datetime.now(KST).date()
    return datetime.fromisoformat(str(value)).date()


if __name__ == "__main__":
    main()
