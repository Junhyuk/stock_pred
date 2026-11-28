#!/usr/bin/env python3
"""Refresh official CFD posting status and optionally reviewed public balance aggregates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from shutil import which
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __package__:
    from .collect_cfd_research import (
        KOFIA_INDEX_URL,
        _official_ref,
        discover_disclosures,
        parse_pdf,
    )
    from .collect_frees_is_cfd import fetch_total as fetch_frees_is_total
else:
    from collect_cfd_research import KOFIA_INDEX_URL, _official_ref, discover_disclosures, parse_pdf
    from collect_frees_is_cfd import fetch_total as fetch_frees_is_total

from roboquant.index_forecast.automation_state import atomic_json
from roboquant.index_forecast.market_calendar import exchange_schedule

KST = ZoneInfo("Asia/Seoul")
HISTORY = "cfd_market_history.json"
STATUS = "cfd_status.json"
FREESIS_HISTORY = "cfd_frees_is_history.json"
FREESIS_DEFINITIONS = ("with_margin", "without_margin")
FREESIS_SCOPE_FIELDS = (
    ("buy_krw", "sell_krw", "total_krw"),
    ("kospi_buy_krw", "kospi_sell_krw", "kospi_krw"),
    ("kosdaq_buy_krw", "kosdaq_sell_krw", "kosdaq_krw"),
)
FREESIS_FIELDS = tuple(field for scope in FREESIS_SCOPE_FIELDS for field in scope)
FREESIS_TOTAL_FIELDS = tuple(scope[2] for scope in FREESIS_SCOPE_FIELDS)
FREESIS_SIDE_FIELDS = tuple(field for scope in FREESIS_SCOPE_FIELDS for field in scope[:2])
MARKETS = ("KOSPI", "KOSDAQ")
DOCUMENT_FIELDS = ("posted_date", "received_at", "source_url", "document_sha256")
MARKET_FIELDS = ("date", *DOCUMENT_FIELDS, "value_krw", "valid_count")
MAX_POINTS = 756
MAX_AMOUNT = 2**53 - 1  # Public values are read by JavaScript as numbers.
DISCLOSURE_UNIT_KRW = 10_000
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


def _iso_day(value):
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def _receipt(value):
    try:
        received = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return received if received.utcoffset() is not None else None


def _valid_amount(value):
    return type(value) is int and 0 <= value <= MAX_AMOUNT


def _valid_detail_url(value):
    try:
        return isinstance(value, str) and _official_ref(value, "view") is not None
    except ValueError:
        return False


def _valid_market_point(row):
    if not isinstance(row, dict):
        return False
    day, posted = row.get("date"), row.get("posted_date")
    received = _receipt(row.get("received_at"))
    source = row.get("source_url")
    amount, count = row.get("value_krw"), row.get("valid_count")
    digest = row.get("document_sha256")
    return (
        _iso_day(day) and _iso_day(posted) and received is not None
        and day <= posted <= received.astimezone(KST).date().isoformat()
        and _valid_detail_url(source)
        and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and _valid_amount(amount) and amount % DISCLOSURE_UNIT_KRW == 0
        and _valid_amount(count) and count > 0
    )


def _market_history(path):
    stored = _read(path)
    snapshots = stored.get("markets")
    history = {market: {} for market in MARKETS}
    if (stored.get("schema_version") != "cfd_market_history_v1" or stored.get("unit") != "KRW"
            or not isinstance(snapshots, dict)):
        return history
    for market, points in history.items():
        rows = snapshots.get(market)
        if isinstance(rows, list):
            for row in rows:
                if _valid_market_point(row):
                    points[row["date"]] = {key: row[key] for key in MARKET_FIELDS}
    first, second = (history[market] for market in MARKETS)
    paired = {
        day for day in first.keys() & second.keys()
        if all(first[day][key] == second[day][key] for key in DOCUMENT_FIELDS)
    }
    return {market: {day: points[day] for day in sorted(paired)} for market, points in history.items()}


def _previous_krx(today):
    _, sessions = exchange_schedule("XKRX", today - timedelta(days=14), today - timedelta(days=1))
    return sessions[-1].date().isoformat()


def _public_entry(item):
    return {
        "observation_date": item.observation_date,
        "posted_date": item.posted_date,
        "source_url": item.source_url,
    }


def _prior_declaration(value, today):
    if not isinstance(value, dict):
        return None
    day, posted, source = (value.get(key) for key in ("observation_date", "posted_date", "source_url"))
    if (_iso_day(day) and _iso_day(posted) and day <= posted <= today.isoformat()
            and _valid_detail_url(source)):
        return {"observation_date": day, "posted_date": posted, "source_url": source}
    return None


def _merge_aggregate(history, item, raw, received_at):
    rows = parse_pdf(raw, item.observation_date)
    if any(row.market not in MARKETS or not _valid_amount(row.value_krw)
           or row.value_krw % DISCLOSURE_UNIT_KRW for row in rows):
        raise ValueError("Invalid domestic CFD amount or market")
    blocks = {market: [row for row in rows if row.market == market] for market in MARKETS}
    if not all(blocks.values()):
        raise ValueError("CFD disclosure did not contain both domestic markets")
    digest = hashlib.sha256(raw).hexdigest()
    points = {
        market: {
            "date": item.observation_date,
            "posted_date": item.posted_date,
            "received_at": received_at.isoformat(timespec="seconds"),
            "source_url": item.source_url,
            "document_sha256": digest,
            "value_krw": sum(row.value_krw for row in block),
            "valid_count": len(block),
        }
        for market, block in blocks.items()
    }
    if not all(_valid_market_point(point) for point in points.values()):
        raise ValueError("Invalid domestic CFD document provenance or aggregate")
    previous = {market: history[market].get(item.observation_date, {}) for market in MARKETS}
    snapshot_fields = tuple(key for key in MARKET_FIELDS if key != "received_at")
    if all(previous[market].get(key) == point[key]
           for market, point in points.items() for key in snapshot_fields):
        return False
    for market, point in points.items():
        history[market][item.observation_date] = point
    return True


def _valid_sides(row):
    for buy, sell, total in FREESIS_SCOPE_FIELDS:
        values = (row.get(buy), row.get(sell), row.get(total))
        if not all(_valid_amount(value) for value in values):
            return False
        if values[0] + values[1] != values[2]:
            return False
    return (row["kospi_buy_krw"] + row["kosdaq_buy_krw"] <= row["buy_krw"]
            and row["kospi_sell_krw"] + row["kosdaq_sell_krw"] <= row["sell_krw"])


def _frees_is_point(row, *, allow_legacy=False):
    if not isinstance(row, dict) or not _iso_day(row.get("date")):
        raise ValueError("Invalid FreeSIS observation date")
    if _valid_sides(row):
        fields = FREESIS_FIELDS
    elif (allow_legacy and all(_valid_amount(row.get(field)) for field in FREESIS_TOTAL_FIELDS)
          and not any(field in row for field in FREESIS_SIDE_FIELDS)
          and row["kospi_krw"] + row["kosdaq_krw"] <= row["total_krw"]):
        fields = FREESIS_TOTAL_FIELDS
    else:
        raise ValueError("Invalid or unreconciled FreeSIS KRW balances")
    return {"date": row["date"], **{field: row[field] for field in fields}}


def _frees_is_start(previous, today, expected):
    latest = date.fromisoformat(expected)
    year = latest.year - 2
    anniversary = latest.replace(year=year, day=min(latest.day, monthrange(year, latest.month)[1]))
    baseline = date.fromisoformat(_previous_krx(anniversary + timedelta(days=1)))
    overlap = today - timedelta(days=21)
    _, sessions = exchange_schedule("XKRX", baseline, overlap - timedelta(days=1))
    required = {session.date().isoformat() for session in sessions}
    if required.issubset({day for day, row in previous.items() if _valid_sides(row)}):
        return overlap.isoformat()
    return baseline.isoformat()


def _frees_is_history(stored):
    snapshots = stored.get("series")
    history = {definition: {} for definition in FREESIS_DEFINITIONS}
    if (stored.get("schema_version") != "cfd_frees_is_history_v1" or stored.get("unit") != "KRW"
            or not isinstance(snapshots, dict)):
        return history
    for definition, points in history.items():
        rows = snapshots.get(definition)
        if isinstance(rows, list):
            for row in rows:
                try:
                    point = _frees_is_point(row, allow_legacy=True)
                except ValueError:
                    continue
                points[point["date"]] = point
    return history


def _refresh_frees_is(output_dir, today, expected, fetch_total, as_of):
    target = output_dir / FREESIS_HISTORY
    stored = _read(target)
    previous = _frees_is_history(stored)
    receipts = stored.get("series_received_at")
    receipts = receipts if isinstance(receipts, dict) else {}
    retained_receipts = {
        definition: receipts[definition] for definition in FREESIS_DEFINITIONS
        if previous[definition] and _receipt(receipts.get(definition)) is not None
    }
    merged = {}
    series_status = {}
    latest = {}
    changed = False
    for definition, points in previous.items():
        first = _frees_is_start(points, today, expected)
        try:
            raw = fetch_total(definition, first, expected) if fetch_total else []
            if not isinstance(raw, list) or len(raw) > MAX_POINTS:
                raise ValueError("Invalid FreeSIS series")
            rows = [_frees_is_point(row) for row in raw]
            dates = [row["date"] for row in rows]
            if dates != sorted(set(dates)) or any(not first <= day <= expected for day in dates):
                raise ValueError("Unordered or out-of-range FreeSIS dates")
        except (KeyError, OSError, TypeError, ValueError):
            rows, dates = [], []
        additions = {row["date"]: row for row in rows}
        updated = points | additions
        latest[definition] = max(updated, default=None)
        day = latest[definition]
        if day in additions and (points.get(day) != additions[day] or definition not in retained_receipts):
            retained_receipts[definition] = (as_of or datetime.now(UTC)).isoformat(timespec="seconds")
            changed = True
        changed |= updated != points
        merged[definition] = [updated[day] for day in sorted(updated)[-MAX_POINTS:]]
        series_status[definition] = (
            "ready" if dates and dates[-1] == expected else "stale" if updated else "unavailable"
        )
    if changed:
        atomic_json(
            target,
            {
                "schema_version": "cfd_frees_is_history_v1",
                "source": "금융투자협회 FreeSIS",
                "unit": "KRW",
                "coverage": "all_reported_including_overseas",
                "series": merged,
                "series_received_at": retained_receipts,
            },
        )
    ready = all(state == "ready" for state in series_status.values())
    return "ready" if ready else "stale" if any(latest.values()) else "unavailable", series_status, latest


def refresh(
    output_dir, *, as_of=None, fetch=fetch_official, permit_aggregates=False,
    fetch_total=None, fetch_totals=None,
):
    clock = as_of or datetime.now(UTC)
    if clock.utcoffset() is None:
        raise ValueError("a timezone-aware status clock is required")
    today = clock.astimezone(KST).date()
    expected = _previous_krx(today)
    output_dir = Path(output_dir)
    prior_status = _read(output_dir / STATUS)
    latest = _prior_declaration(prior_status.get("latest"), today)
    try:
        raw = fetch(KOFIA_INDEX_URL)
        declared = discover_disclosures(raw.decode("utf-8"), limit=3)
        declared = [item for item in declared if item.posted_date <= today.isoformat()]
        if not declared:
            raise ValueError("no eligible KOFIA posting found")
        declared.sort(
            key=lambda item: (item.observation_date, item.posted_date, int(item.document_ref)),
            reverse=True,
        )
        reported = _public_entry(declared[0])
        board_status = (
            "source_current" if reported["observation_date"] >= expected else "source_delayed"
        )
        if latest is None or (reported["observation_date"], reported["posted_date"]) >= (
            latest["observation_date"], latest["posted_date"]
        ):
            latest = reported
    except (OSError, ValueError):
        declared = []
        board_status = "source_unavailable"

    error_count = 0
    domestic_latest = None
    data_status = "source_permission_unconfirmed"
    frees_is_status = "source_permission_unconfirmed"
    frees_is_series_status = dict.fromkeys(FREESIS_DEFINITIONS, frees_is_status)
    frees_is_latest = dict.fromkeys(FREESIS_DEFINITIONS)
    if permit_aggregates:
        market_history = _market_history(output_dir / HISTORY)
        current_document_verified = False
        if declared and which("pdftotext") is not None:
            updated = False
            for index, item in enumerate(declared):
                if index and item.observation_date in market_history[MARKETS[0]]:
                    continue
                try:
                    pdf = fetch(item.attachment_url)
                    updated |= _merge_aggregate(
                        market_history, item, pdf, as_of or datetime.now(UTC)
                    )
                    if index == 0:
                        current_document_verified = _public_entry(item) == latest
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
        domestic_latest = max(market_history[MARKETS[0]], default=None)
        data_status = (
            "ready" if current_document_verified and board_status == "source_current"
            else "stale" if domestic_latest else "unavailable"
        )
        if fetch_total is None and fetch_totals is not None:
            paired = cache(fetch_totals)

            def fetch_total(definition, first, last):
                return paired(first, last)[definition]

        frees_is_status, frees_is_series_status, frees_is_latest = _refresh_frees_is(
            output_dir, today, expected, fetch_total, as_of
        )

    status = {
        "schema_version": "cfd_status_v1",
        "checked_at": clock.astimezone(UTC).isoformat(timespec="seconds"),
        "expected_observation_date": expected,
        "status": board_status,
        "data_status": data_status,
        "frees_is_status": frees_is_status,
        "frees_is_series_status": frees_is_series_status,
        "frees_is_latest_observation_dates": frees_is_latest,
        "domestic_latest_observation_date": domestic_latest,
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
    status = refresh(
        args.output_dir,
        permit_aggregates=args.publish_approved_aggregates,
        fetch_total=fetch_frees_is_total if args.publish_approved_aggregates else None,
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
