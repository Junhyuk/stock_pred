#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run global premarket collection and regime pipeline.")
    parser.add_argument("--config", default="configs/global_market.yaml")
    parser.add_argument("--from-date", default="2022-01-01")
    parser.add_argument("--to-date", default="latest")
    parser.add_argument("--cutoff", default="latest")
    parser.add_argument("--prediction-date", default="latest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    commands = [
        [
            sys.executable,
            "scripts/collect_global_market_daily.py",
            "--config",
            args.config,
            "--from-date",
            args.from_date,
            "--to-date",
            args.to_date,
        ],
        [
            sys.executable,
            "scripts/collect_premarket_snapshot.py",
            "--config",
            args.config,
            "--cutoff",
            args.cutoff,
        ],
        [
            sys.executable,
            "scripts/build_market_regime.py",
            "--config",
            args.config,
            "--prediction-date",
            args.prediction_date,
            "--cutoff",
            args.cutoff,
        ],
    ]
    for command in commands:
        print("$ " + " ".join(command))
        subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
