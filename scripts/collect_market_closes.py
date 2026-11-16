#!/usr/bin/env python3
"""Collect and separately receipt the latest completed Korean index closes."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from roboquant.index_forecast.market_closes import (
    collect_market_closes,
    preserve_previous_closes,
    write_market_closes,
)
from roboquant.index_forecast.reliable_inputs import utc_timestamp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--now', required=True, help='actual invocation time (ISO 8601)')
    args = parser.parse_args()
    now = utc_timestamp(args.now)
    actual = datetime.now(UTC)
    if abs((now.to_pydatetime() - actual).total_seconds()) > 300:
        parser.error('--now must be the actual invocation time (within five minutes)')
    result = collect_market_closes(args.db, now=now, receipt_clock=lambda: datetime.now(UTC))
    result = preserve_previous_closes(result, args.output)
    write_market_closes(result, args.output, published_at=lambda: datetime.now(UTC))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
