#!/usr/bin/env python3
"""Refresh the public index constituent master snapshots."""
from datetime import datetime
from pathlib import Path

from roboquant.index_forecast.index_constituents import KST, collect, valid_payload


def main() -> int:
    today = datetime.now(KST).date().isoformat()
    payload = collect(Path("docs/data/index_constituents.json"), today)
    if not valid_payload(payload, today=today):
        print("No complete validated constituent snapshot is available.")
        return 1
    for name, market in payload["indices"].items():
        print(f"{name}: {market['count']} constituents; snapshot {market['as_of']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
