#!/usr/bin/env python3
"""Normalize approved, already-collected news metadata for local Ollama."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path
from roboquant.db import connect_database
from roboquant.signals.local_llm_news import load_local_llm_news_config, refresh_local_llm_documents


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize approved news metadata for local Ollama.")
    parser.add_argument("--config", default="configs/local_llm_news.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_local_llm_news_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    try:
        print(json.dumps(refresh_local_llm_documents(conn, config), ensure_ascii=False, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
