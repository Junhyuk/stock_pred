#!/usr/bin/env python3
"""Refresh official CFD posting status and optionally reviewed public balance aggregates."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import which
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __package__:
    from .collect_cfd_research import KOFIA_INDEX_URL, discover_disclosures, parse_pdf
else:
    from collect_cfd_research import KOFIA_INDEX_URL, discover_disclosures, parse_pdf

from roboquant.index_forecast.automation_state import atomic_json
from roboquant.index_forecast.market_calendar import exchange_schedule

KST = ZoneInfo("Asia/Seoul")
HISTORY = "cfd_market_history.json"
STATUS = "cfd_status.json"
MARKETS = ("KOSPI", "KOSDAQ")
MAX_POINTS = 756
FREESIS_BASE = "https://freesis.kofia.or.kr/stat/FreeSIS.do?parentDivId=MSIS30060000000000"
OFFICIAL_LINKS = {
    "domestic_names": KOFIA_INDEX_URL,
    "total_with_margin": FREESIS_BASE
    + "&serviceId=STATDRV0100000320&divisionId=MSIS30060010000000",
    "total_without_margin": FREESIS_BASE
    + "&serviceId=STATDRV0100000310&divisionId=MSIS30060020000000",
}


def fetch_official(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=6) as response:
        payload = response.read((10 << 20) + 1)
    if len(payload) > 10 << 20:
        raise ValueError("official response exceeds allowed size")
    return payload


def _read(path):
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _history(path):
    stored = _read(path)
    if stored.get("schema_version") != "cfd_market_history_v1":
        return {market: {} for market in MARKETS}
    return {
        market: {row["date"]: row for row in stored.get("markets", {}).get(market, [])}
        for market in MARKETS
    }


def _previous_krx(today):
    _, sessions = exchange_schedule("XKRX", today - timedelta(days=14), today - timedelta(days=1))
    return sessions[-1].date().isoformat()


def _public_entry(item):
    return {
        "observation_date": item.observation_date,
        "posted_date": item.posted_date,
        "source_url": item.source_url,
    }


def _merge_aggregate(history, item, raw, received_at):
    rows = parse_pdf(raw, item.observation_date)
    blocks = {market: [row for row in rows if row.market == market] for market in MARKETS}
    if not all(blocks.values()):
        raise ValueError("CFD disclosure did not contain both domestic markets")
    digest = hashlib.sha256(raw).hexdigest()
    changed = False
    for market, block in blocks.items():
        previous = history[market].get(item.observation_date)
        if previous and previous["document_sha256"] == digest:
            continue
        history[market][item.observation_date] = {
            "date": item.observation_date,
            "posted_date": item.posted_date,
            "received_at": received_at.isoformat(timespec="seconds"),
            "source_url": item.source_url,
            "document_sha256": digest,
            "value_krw": sum(row.value_krw for row in block),
            "valid_count": len(block),
        }
        changed = True
    return changed


def _refresh_frees_is(output_dir, today, expected, fetch_totals):
    target = output_dir / "cfd_frees_is_history.json"
    previous = _read(target).get("series", {})
    try:
        snapshots = fetch_totals((today - timedelta(days=21)).isoformat(), expected)
        merged = {}
        for definition in ("with_margin", "without_margin"):
            points = {row["date"]: row for row in previous.get(definition, [])}
            points.update({row["date"]: row for row in snapshots[definition]})
            merged[definition] = [points[day] for day in sorted(points)[-MAX_POINTS:]]
        if snapshots["with_margin"] and merged != previous:
            atomic_json(
                target,
                {
                    "schema_version": "cfd_frees_is_history_v1",
                    "source": "금융투자협회 FreeSIS",
                    "unit": "KRW",
                    "coverage": "all_reported_including_overseas",
                    "series": merged,
                },
            )
        return (
            "ready"
            if all(snapshots[key] and snapshots[key][-1]["date"] == expected
                   for key in ("with_margin", "without_margin"))
            else ("stale" if any(merged.values()) else "unavailable")
        )
    except (KeyError, OSError, TypeError, ValueError):
        return "stale" if any(previous.values()) else "unavailable"


def refresh(
    output_dir, *, as_of=None, fetch=fetch_official, permit_aggregates=False, fetch_totals=None
):
    clock = as_of or datetime.now(UTC)
    if clock.utcoffset() is None:
        raise ValueError("a timezone-aware status clock is required")
    today = clock.astimezone(KST).date()
    expected = _previous_krx(today)
    output_dir = Path(output_dir)
    prior_status = _read(output_dir / STATUS)
    latest = prior_status.get("latest")
    try:
        raw = fetch(KOFIA_INDEX_URL)
        declared = discover_disclosures(raw.decode("utf-8"), limit=3)
        declared = [item for item in declared if item.posted_date <= today.isoformat()]
        if not declared:
            raise ValueError("no eligible KOFIA posting found")
        latest = _public_entry(declared[0])
        board_status = (
            "source_current" if latest["observation_date"] >= expected else "source_delayed"
        )
    except (URLError, OSError, ValueError, UnicodeError):
        declared = []
        board_status = "source_unavailable"

    error_count = 0
    market_history = _history(output_dir / HISTORY)
    data_status = "source_permission_unconfirmed"
    if permit_aggregates:
        if not declared:
            data_status = "stale" if any(market_history.values()) else "unavailable"
        elif which("pdftotext") is None:
            data_status = "pdf_parser_unavailable"
        else:
            updated = False
            for item in declared:
                seen = all(item.observation_date in market_history[market] for market in MARKETS)
                if seen and item != declared[0]:
                    continue
                try:
                    pdf = fetch(item.attachment_url)
                    updated |= _merge_aggregate(
                        market_history, item, pdf, as_of or datetime.now(UTC)
                    )
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    error_count += 1
            if updated:
                atomic_json(
                    output_dir / HISTORY,
                    {
                        "schema_version": "cfd_market_history_v1",
                        "source": {
                            "label": "금융투자협회 종목별 CFD 잔고동향",
                            "url": KOFIA_INDEX_URL,
                        },
                        "metric": "domestic_disclosed_cfd_balance",
                        "coverage": "disclosed_only",
                        "unit": "KRW",
                        "markets": {
                            market: [rows[day] for day in sorted(rows)[-MAX_POINTS:]]
                            for market, rows in market_history.items()
                        },
                    },
                )
            latest_covered = all(
                latest["observation_date"] in rows for rows in market_history.values()
            )
            if error_count or not latest_covered or board_status != "source_current":
                data_status = "stale" if any(market_history.values()) else "unavailable"
            else:
                data_status = "ready"

    fallback = (
        _refresh_frees_is(output_dir, today, expected, fetch_totals)
        if permit_aggregates and fetch_totals
        else "source_permission_unconfirmed"
    )
    status = {
        "schema_version": "cfd_status_v1",
        "checked_at": clock.astimezone(UTC).isoformat(timespec="seconds"),
        "expected_observation_date": expected,
        "status": board_status,
        "data_status": data_status,
        "frees_is_status": fallback,
        "latest": latest,
        "sources": OFFICIAL_LINKS,
        "pdf_failures": error_count,
    }
    atomic_json(output_dir / STATUS, status)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs/data")
    parser.add_argument(
        "--publish-approved-aggregates",
        action="store_true",
        help="Use only for source-reviewed recurring access and noncommercial aggregate publication.",
    )
    args = parser.parse_args()
    fetch_totals = None
    if args.publish_approved_aggregates:
        from collect_frees_is_cfd import fetch_totals

    status = refresh(
        args.output_dir,
        permit_aggregates=args.publish_approved_aggregates,
        fetch_totals=fetch_totals,
    )
    print(
        json.dumps(
            {
                "status": status["status"],
                "data_status": status["data_status"],
                "latest": status["latest"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
