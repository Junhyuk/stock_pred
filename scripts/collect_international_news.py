#!/usr/bin/env python3
"""Collect bounded public feed metadata without fetching publisher article bodies."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml

from roboquant.index_forecast.international_news import collect_news_metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/international_news_feeds.yaml")
    parser.add_argument("--output", default="data/raw/international_news/metadata.json")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text())
    fresh = collect_news_metadata(config)
    path = Path(args.output)
    if path.is_file():
        old = json.loads(path.read_text())
        # Original versions retain their real receipt times; a repeated fetch is
        # a new immutable version, never a rewrite of earlier metadata.
        versions = {e["event_id"]: e for e in old.get("events", [])}
        versions.update({e["event_id"]: e for e in fresh["events"]})
        fresh["events"] = list(versions.values())
        fresh["coverage"]["windows"] = old.get("coverage", {}).get("windows", []) + fresh["coverage"]["windows"]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(fresh, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)
    print(json.dumps({"output": str(path), "metadata_versions": len(fresh["events"]),
                      "coverage_complete": False, "numerical_news_weight": 0}))


if __name__ == "__main__":
    main()
