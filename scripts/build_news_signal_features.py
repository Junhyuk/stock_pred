#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import append_dedup_table, connect_database
from roboquant.signals.news_signals import build_news_signal_daily


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build daily headline-only news signal features.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--symbols", default=None, help="Comma-separated local stock symbols.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    symbols = [item.strip().zfill(6) for item in args.symbols.split(",") if item.strip()] if args.symbols else None
    conn = connect_database(get_database_path(config))
    try:
        frame = build_news_signal_daily(conn, config, signal_date=args.date, symbols=symbols)
        append_dedup_table(conn, "news_signal_daily", frame, ["signal_date", "scope", "symbol"])
        if frame.empty:
            print("news_signal_daily rows upserted: 0 (no approved stored news available)")
        else:
            print(f"news_signal_daily rows upserted: {len(frame)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
