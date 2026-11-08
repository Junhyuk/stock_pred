#!/usr/bin/env python3
"""Normalize an actually fetched US daily snapshot without trusting midnight labels."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.index_forecast.us_market_inputs import normalize_us_observations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-csv", required=True)
    parser.add_argument("--output", default="data/processed/us_market_observations_v2.csv")
    parser.add_argument("--buffer-minutes", type=int, default=15)
    args = parser.parse_args()
    raw_path, output = Path(args.raw_csv), Path(args.output)
    result = normalize_us_observations(pd.read_csv(raw_path), buffer_minutes=args.buffer_minutes)
    encoded = result["observations"].to_csv(index=False).encode("utf-8")
    if output.exists() and output.read_bytes() != encoded:
        raise ValueError("US snapshot is immutable; choose a new output path for a changed dataset")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded)
    metadata = {**result["report"], "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                "observation_sha256": hashlib.sha256(encoded).hexdigest(), "path": str(output)}
    output.with_suffix(".metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    main()
