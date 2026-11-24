#!/usr/bin/env python3
"""Refresh the cutoff-safe U.S. market inputs used by the local market demo."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Step:
    name: str
    command: list[str]
    optional: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the focused U.S. premarket update without a full Top50 retrain.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--global-config", default="configs/global_market.yaml")
    parser.add_argument("--llm-config", default="configs/local_llm_news.yaml")
    parser.add_argument("--cutoff", default="latest")
    parser.add_argument("--target-date", default="latest")
    parser.add_argument("--skip-domestic", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def build_steps(args: argparse.Namespace) -> list[Step]:
    steps = [
        Step(
            "global_premarket",
            [
                "scripts/run_premarket_global_pipeline.py",
                "--config",
                args.global_config,
                "--top50-config",
                args.config,
                "--from-date",
                "2022-01-01",
                "--to-date",
                args.target_date,
                "--cutoff",
                args.cutoff,
                "--prediction-date",
                args.target_date,
            ],
        ),
        Step("collect_market_news", ["scripts/collect_market_news.py", "--config", "configs/market_news.yaml"], optional=True),
        Step("collect_gdelt_news", ["scripts/collect_gdelt_news.py", "--config", "configs/official_data_sources.yaml", "--date", args.target_date], optional=True),
        Step("collect_marketnewsfeed", ["scripts/collect_marketnewsfeed_site.py", "--config", "configs/marketnewsfeed.yaml"], optional=True),
        Step("local_llm_news", ["scripts/run_local_llm_news_pipeline.py", "--config", args.llm_config, "--stage", "premarket"], optional=True),
        Step("news_signal_features", ["scripts/build_news_signal_features.py", "--config", args.config, "--date", args.target_date], optional=True),
        Step("us_sector_linkage", ["scripts/build_us_sector_linkage.py", "--config", args.global_config, "--date", "latest"]),
        Step("koru_korea_linkage", ["scripts/build_koru_korea_linkage.py", "--config", args.config, "--date", "latest"]),
    ]
    if not args.skip_domestic:
        steps.extend(
            [
                Step("feature_matrix", ["scripts/build_feature_matrix.py", "--config", args.config]),
                Step("train_2m", ["scripts/train_models.py", "--config", args.config, "--horizon", "2M"]),
                Step(
                    "predictions_2m",
                    ["scripts/generate_recommendations.py", "--config", args.config, "--date", "latest", "--horizon", "2M"],
                ),
            ]
        )
    steps.extend(
        [
            Step("us_liquidity_shadow", ["scripts/run_us_market_liquidity_shadow.py", "--config", args.config, "--stage", "premarket", "--date", "latest"], optional=True),
            Step("market_outlook", ["scripts/run_tomorrow_market_ml.py", "--config", args.llm_config, "--date", "latest", "--target-date", args.target_date]),
        ]
    )
    return steps


def main() -> None:
    args = parse_args()
    for step in build_steps(args):
        command = [sys.executable, *step.command]
        print(f"{step.name}: $ {' '.join(command)}", flush=True)
        if args.dry_run:
            continue
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode and not step.optional:
            raise SystemExit(result.returncode)
        if result.returncode:
            print(f"{step.name}: optional source failed; keeping missing status", flush=True)


if __name__ == "__main__":
    main()
