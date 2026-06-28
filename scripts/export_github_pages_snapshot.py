#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.reports.github_pages_export import export_docs_bundle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export GitHub Pages static snapshot into docs/.")
    parser.add_argument("--config", default="configs/poc.yaml")
    parser.add_argument("--today-config", default="configs/today_update.yaml")
    parser.add_argument("--output", default="docs")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = export_docs_bundle(
        root=ROOT,
        output_dir=ROOT / args.output,
        config_path=ROOT / args.config,
        today_config_path=ROOT / args.today_config,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
