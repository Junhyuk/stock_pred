#!/usr/bin/env python3
"""Run the bounded local-Ollama news pipeline without touching production forecasts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path
from roboquant.db import connect_database
from roboquant.signals.local_llm_news import (
    build_local_llm_news_signal_daily,
    load_local_llm_news_config,
    refresh_local_llm_documents,
    summarize_local_llm_news,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bounded local-Ollama approved-news processing.")
    parser.add_argument("--config", default="configs/local_llm_news.yaml")
    parser.add_argument("--stage", choices=("close", "premarket"), required=True)
    parser.add_argument("--limit", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_local_llm_news_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    try:
        documents = refresh_local_llm_documents(conn, config)
        assessments = summarize_local_llm_news(conn, config, stage=args.stage, limit=args.limit)
        signals = build_local_llm_news_signal_daily(conn, config)
        status = assessments.get("status", "failed")
        if status == "ready" and documents.get("status") != "ready":
            status = str(documents.get("status"))
        print(
            json.dumps(
                {
                    "status": status,
                    "documents": documents,
                    "assessments": assessments,
                    "signals": {"rows": len(signals)},
                },
                ensure_ascii=False,
                default=str,
            )
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
