#!/usr/bin/env python3
"""Run local Ollama only for uncached, approved news metadata."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path
from roboquant.db import connect_database
from roboquant.signals.local_llm_news import load_local_llm_news_config, summarize_local_llm_news


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create structured local-Ollama news assessments.")
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
        result = summarize_local_llm_news(conn, config, stage=args.stage, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
