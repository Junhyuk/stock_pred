#!/usr/bin/env python3
"""Collect official macro facts under an outer 120-second wall deadline."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-result', type=Path)
    parser.add_argument('--output-dir', type=Path, default=Path('docs/data'))
    parser.add_argument('--private-dir', type=Path, default=Path('data/processed/economic-calendar'))
    args = parser.parse_args()
    if args.worker_result:
        from roboquant.index_forecast.economic_sources import collect_economic_sources
        result = collect_economic_sources(now=datetime.now(UTC), budget_seconds=118, timeout_seconds=10, retries=1)
        args.worker_result.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False))
    else:
        from roboquant.index_forecast.automation_state import run_calendar_collection
        print(json.dumps(run_calendar_collection(args.output_dir, args.private_dir), ensure_ascii=False))


if __name__ == '__main__':
    main()
