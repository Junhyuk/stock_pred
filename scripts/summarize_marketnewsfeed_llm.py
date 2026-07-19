#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import get_database_path, load_config
from roboquant.db import connect_database
from roboquant.signals.marketnewsfeed_llm import summarize_marketnewsfeed_headlines


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize public MarketNewsFeed headlines with local Ollama.")
    parser.add_argument("--config", default="configs/marketnewsfeed_llm.yaml")
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    conn = connect_database(get_database_path(config))
    try:
        result = summarize_marketnewsfeed_headlines(conn, config, limit=args.limit)
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
