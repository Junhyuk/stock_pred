#!/usr/bin/env python3
"""Capture official rates and credit balances and export compact tracking status."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from roboquant.index_forecast.automation_state import atomic_json
from roboquant.index_forecast.official_credit import CreditSourceError, collect_credit_balances
from roboquant.index_forecast.official_yields import collect_kr_yields, collect_us_yields
from roboquant.index_forecast.session_input_store import (
    COLUMNS,
    KOFIA_CREDIT_SOURCE,
    MARGIN,
    append_inputs,
    read_inputs,
)
from scripts.export_margin_credit_chart import SOURCE_URL, build_chart

STATUS_FILE = "market_indicators_status.json"
CHART_FILE = "margin_credit_chart.json"
SCHEMA = "market_indicators_status_v1"
CREDIT_MARKETS = ("GLOBAL", "KOSPI", "KOSDAQ")
WATCH = {
    "credit": tuple((market, MARGIN) for market in CREDIT_MARKETS),
    **{
        f"{country.lower()}_yields": tuple(
            ("GLOBAL", f"{country}_YIELD_{tenor}") for tenor in ("1Y", "3Y", "10Y")
        )
        for country in ("KR", "US")
    },
}
OFFICIAL = {
    "credit": {KOFIA_CREDIT_SOURCE, "fsc_kofia_margin_credit"},
    "kr_yields": {"bok_ecos_817Y002", "kofia_freesis_bond_yield"},
    "us_yields": {"us_treasury_daily_par_yield_curve"},
}
GROUP = {key: group for group, keys in WATCH.items() for key in keys}


def _key(row: dict) -> tuple[str, str]:
    return row["market"], row["series"]


def _official(row: dict) -> bool:
    return row["source"] in OFFICIAL.get(GROUP.get(_key(row)), ()) and row["is_final"] is not False


def _read_seed(seed: Path) -> list[dict]:
    rows = []
    status = seed / STATUS_FILE
    if status.is_file():
        saved = json.loads(status.read_bytes())
        if saved.get("schema_version") == SCHEMA:
            rows.extend(row for row in saved["observations"] if _official(row))
    chart = seed / CHART_FILE
    if chart.is_file():
        saved = json.loads(chart.read_bytes())
        if (
            saved.get("schema_version") == "margin_credit_chart_v1"
            and saved.get("unit") == "KRW"
            and saved.get("source_url") == SOURCE_URL
        ):
            # The public chart supplies a conservative receipt for older
            # points when a runner starts without its private cache.
            for market in ("KOSPI", "KOSDAQ"):
                rows.extend(
                    {
                        "market": market,
                        "series": MARGIN,
                        "observation_date": item["date"],
                        "value": item["balance_krw"],
                        "unit": "KRW",
                        "source": KOFIA_CREDIT_SOURCE,
                        "received_at": saved["snapshot_received_at"],
                        "published_at": None,
                        "is_final": None,
                        "availability_policy": "observed",
                    }
                    for item in saved["markets"][market][-90:]
                )
    return rows


def _source_results(as_of: datetime, ecos_key: str, fsc_key: str) -> tuple[dict, list[dict]]:
    operations = {
        "credit": partial(collect_credit_balances, as_of=as_of, api_key=fsc_key or None),
        "us_yields": partial(collect_us_yields, as_of=as_of),
    }
    status = {}
    rows = []
    if ecos_key:
        operations["kr_yields"] = partial(collect_kr_yields, as_of=as_of, api_key=ecos_key)
    else:
        status["kr_yields"] = {"status": "unavailable", "reason": "missing_credentials"}

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        futures = {group: pool.submit(operation) for group, operation in operations.items()}
        for group, future in futures.items():
            try:
                observed = [
                    row for row in future.result() if _official(row) and GROUP[_key(row)] == group
                ]
            except (CreditSourceError, OSError, ValueError):
                status[group] = {"status": "unavailable", "reason": "source_error"}
                continue
            coverage = {_key(row) for row in observed}
            if coverage == set(WATCH[group]):
                status[group] = {"status": "ready", "reason": None}
            elif observed:
                reason = (
                    "missing_market_split_credentials"
                    if group == "credit" and not fsc_key
                    else "incomplete_scope"
                )
                status[group] = {"status": "partial", "reason": reason}
            else:
                status[group] = {"status": "unavailable", "reason": "no_valid_observation"}
            rows.extend(observed)
    return status, rows


def _latest_observations(sqlite: Path, *, as_of: datetime, obtained: list[dict]) -> list[dict]:
    latest = {}
    for row in read_inputs(sqlite, as_of=as_of).to_dict("records"):
        if not _official(row) or not pd.notna(row["value"]):
            continue
        key = _key(row)
        if key not in latest or (row["observation_date"], row["received_at"]) > (
            latest[key]["observation_date"],
            latest[key]["received_at"],
        ):
            latest[key] = row
    observed = {(_key(row), row["observation_date"], row["value"]) for row in obtained}
    return [
        {
            **row,
            "tracking_status": "seen"
            if (key, row["observation_date"], row["value"]) in observed
            else "retained",
        }
        for key, row in sorted(latest.items())
    ]


def collect_indicators(
    *, sqlite: Path, output: Path, seed: Path, ecos_key: str, fsc_key: str
) -> dict:
    if sqlite.resolve().is_relative_to(output.resolve()) or sqlite.resolve().is_relative_to(
        seed.resolve()
    ):
        raise ValueError("The private SQLite path must be outside the public directories")
    started = datetime.now(UTC)
    source_status, obtained = _source_results(started, ecos_key, fsc_key)
    warm_start = [] if sqlite.is_file() else _read_seed(seed)
    inserted = append_inputs(sqlite, pd.DataFrame(warm_start + obtained, columns=COLUMNS))
    finished = datetime.now(UTC)
    observations = _latest_observations(sqlite, as_of=finished, obtained=obtained)
    report = {
        "schema_version": SCHEMA,
        "checked_at": finished.isoformat(),
        "elapsed_seconds": round((finished - started).total_seconds(), 3),
        "sources": source_status,
        "observations": observations,
        "inserted": inserted,
        "production_weight": 0.0,
    }
    chart = output / CHART_FILE
    credit_markets = {row["market"] for row in observations if row["series"] == MARGIN}
    market_update = any(
        row["series"] == MARGIN and row["market"] in {"KOSPI", "KOSDAQ"} for row in obtained
    )
    if market_update and {"KOSPI", "KOSDAQ"} <= credit_markets:
        atomic_json(chart, build_chart(sqlite, as_of=finished))
    elif not chart.is_file() and (seed / CHART_FILE).is_file():
        output.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed / CHART_FILE, chart)
    atomic_json(output / STATUS_FILE, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--seed-dir", type=Path)
    args = parser.parse_args()
    report = collect_indicators(
        sqlite=args.sqlite,
        output=args.output_dir,
        seed=args.seed_dir or args.output_dir,
        ecos_key=os.environ.get("BOK_ECOS_API_KEY", "").strip(),
        fsc_key=os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip(),
    )
    print(
        json.dumps(
            {key: report[key] for key in ("checked_at", "sources", "inserted", "elapsed_seconds")}
        )
    )


if __name__ == "__main__":
    main()
