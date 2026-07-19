#!/usr/bin/env python3
"""Materialize cutoff-safe local-Ollama market and stock signals."""

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
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build cutoff-safe local-Ollama signal rows.")
    parser.add_argument("--config", default="configs/local_llm_news.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_local_llm_news_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    try:
        frame = build_local_llm_news_signal_daily(conn, config)
        print(json.dumps({"status": "ready", "rows": len(frame)}, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
