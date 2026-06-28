#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run configured daily RoboQuant pipeline steps.")
    parser.add_argument("--config", default="configs/pipeline.yaml")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pipeline_path = (ROOT / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)
    with pipeline_path.open("r", encoding="utf-8") as file:
        pipeline = yaml.safe_load(file) or {}

    log_dir = ROOT / pipeline.get("logs", {}).get("dir", "logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    for step in pipeline.get("steps", []):
        if not step.get("enabled", True):
            continue
        command = _normalize_command(step["command"])
        print(f"\n=== RUN {step['name']}: {' '.join(command)} ===")
        if args.dry_run:
            continue
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"pipeline failed at {step['name']} with code {result.returncode}")


def _normalize_command(command: list[str]) -> list[str]:
    if command and command[0] == "python":
        return [sys.executable, *command[1:]]
    return command


if __name__ == "__main__":
    main()

