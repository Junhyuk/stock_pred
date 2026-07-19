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
from roboquant.signals.marketnewsfeed_llm import build_marketnewsfeed_llm_signal_daily


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build cutoff-safe MarketNewsFeed local LLM features.")
    parser.add_argument("--config", default="configs/marketnewsfeed_llm.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    conn = connect_database(get_database_path(config))
    try:
        frame = build_marketnewsfeed_llm_signal_daily(conn, config)
    finally:
        conn.close()
    print(json.dumps({"status": "ready", "rows": int(len(frame))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
