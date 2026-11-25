#!/usr/bin/env python3
"""Collect independent market closes before attempting historical price refresh."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from refresh_premarket_prices import refresh_premarket_prices

from roboquant.index_forecast.market_closes import (
    collect_market_closes,
    preserve_previous_closes,
    write_market_closes,
)
from roboquant.index_forecast.us_market_inputs import normalize_us_observations


def run(*, sqlite_path='data/processed/index_forecast.sqlite', output_dir='data/processed',
        public_dir='docs/data', morning=False):
    # The independent current-close observation remains publishable if Yahoo
    # historical refresh or the downstream model is unavailable.
    closes = collect_market_closes(sqlite_path, now=datetime.now(UTC), receipt_clock=lambda: datetime.now(UTC))
    closes = preserve_previous_closes(closes, Path(public_dir) / "market_closes.json")
    write_market_closes(closes, Path(public_dir) / 'market_closes.json', published_at=lambda: datetime.now(UTC))
    try:
        options = {"include_morning_etfs": True} if morning else {}
        return refresh_premarket_prices(sqlite_path=sqlite_path, output_dir=output_dir, **options)
    except Exception as error:
        paths = sorted(p for p in Path(output_dir).glob('us_market_observations_v2_*.csv') if p.stem.removeprefix('us_market_observations_v2_')[:8].isdigit())
        if paths:
            us_csv = paths[-1]
        else:
            us_csv = Path(output_dir) / 'us_market_observations_v2_unavailable.csv'
            us_csv.parent.mkdir(parents=True, exist_ok=True)
            normalize_us_observations(pd.DataFrame())['observations'].to_csv(us_csv, index=False)
        return {'sqlite': str(sqlite_path), 'us_csv': str(us_csv),
                'history_refresh_status': 'unavailable', 'reason': type(error).__name__}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--morning", action="store_true", help="include the verified EWY and KORU inputs")
    print(json.dumps(run(morning=parser.parse_args().morning), ensure_ascii=False))
