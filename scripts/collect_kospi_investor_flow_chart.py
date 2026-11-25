#!/usr/bin/env python3
"""Build the KOSPI and KOSDAQ investor chart from daily net buys.

The window starts two calendar years before today in Seoul. Values in
investor_flow are won; the page file stores
daily eok. The page adds those daily values into a cumulative line and draws
a 4-week average of that line. Four weeks means 20 stored trading sessions:
the mean of the cumulative level, not of the daily net buys. The latest box
shows cumulative, today, and the previous session in 조원, which is net won
divided by 1e12 (stored eok divided by 10,000). The series has no forecast weight.

Two ways to build the file:

* Default (runner and local): start from the committed JSON, fetch missing
  XKRX sessions and the last five stored sessions from Naver, and write only
  a validated rolling window. A legacy 13-month snapshot is backfilled once.
  Network or validation failure leaves the previous file untouched. A latest
  session received before
  20:30 KST (KRX after-market closes 20:00) is listed in provisional_dates and
  is replaced at the next run. A stored value that Naver later changes is
  replaced and written to revisions. Nothing is zero-filled: a missing session
  is an error, not a zero.
* --sqlite PATH: rebuild from the local investor_flow table (history only on
  the developer machine; the runner's sqlite never holds it).
"""
from __future__ import annotations

import argparse
import calendar
import importlib.util
import json
import math
import os
import sqlite3
import sys
import time as clock
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

FOREIGN = {"9000", "9001"}
PERSONAL = {"8000"}
INSTITUTION = {"1000", "2000", "3000", "3100", "4000", "5000", "6000", "7000"}
GROUPS = {"foreign": FOREIGN, "personal": PERSONAL, "institution": INSTITUTION}
REQUIRED = {"9000", "8000", "1000"}
MARKETS = ("KOSPI", "KOSDAQ")
WINDOW_LABEL = "2년"
LEGACY_WINDOW_LABEL = "1년 1개월"
AVERAGE_SESSIONS = 20
INVESTOR_FIELDS = {"기관": "institution_eok", "개인": "personal_eok", "외국인": "foreign_eok"}
FIELDS = tuple(INVESTOR_FIELDS.values())
SOURCE_URL = "https://stock.naver.com/market/stock/kr/trend/trader"
ENDPOINT = "https://stock.naver.com/api/domestic/market/trend/daily"
SOURCE_TAG = "naver_investor_trend_unofficial"
USER_AGENT = "stock-pred-research-chart/1.0"
KST = ZoneInfo("Asia/Seoul")
PAGE_SIZE = 200  # Naver rejects pageSize above 200
REFETCH_SESSIONS = 5
MAX_REVISIONS = 3
MAX_FETCH = 600
# KRX after-market ends 20:00 KST (Naver market-status: "애프터마켓 마감").
FINAL_AFTER = time(20, 30)
MIN_ROWS = 20
MAX_ROWS = 640
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data/processed/index_forecast.sqlite"
DEFAULT_OUTPUT = "docs/data/kospi_investor_flow_chart.json"


def trend_url(market: str, bizdate: str, start: int, page_size: int) -> str:
    """`start` is the page number (0 = newest page), not a row offset."""
    return (
        f"{ENDPOINT}?tradeType=KRX&marketType={market}"
        f"&bizdate={bizdate}&startIdx={start}&pageSize={page_size}"
    )


def window_start(today: str) -> str:
    """First calendar day of the window, two calendar years before today."""
    current = datetime.strptime(today, "%Y-%m-%d").date()
    return _add_months(current, -24).isoformat()


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _eok(won: float) -> float:
    return round(won / 100_000_000, 1)


def parse_day_won(item: dict) -> dict | None:
    """One Naver day as net won per investor group; None when a required code is absent."""
    if not isinstance(item, dict):
        return None
    bizdate = str(item.get("bizdate") or "")
    if len(bizdate) != 8 or not bizdate.isdigit():
        return None
    totals = {name: 0.0 for name in GROUPS}
    seen = set()
    amounts = item.get("netAmounts")
    if not isinstance(amounts, list):
        return None
    for row in amounts:
        if not isinstance(row, dict):
            continue
        code = str(row.get("investorGubun") or "")
        try:
            value = float(row.get("diffValue"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        seen.add(code)
        for name, codes in GROUPS.items():
            if code in codes:
                totals[name] += value
    if not REQUIRED <= seen:
        return None
    return {"date": f"{bizdate[:4]}-{bizdate[4:6]}-{bizdate[6:]}", **totals}


def parse_day(item: dict) -> dict | None:
    won = parse_day_won(item)
    if won is None:
        return None
    return {
        "date": won["date"],
        "foreign_eok": _eok(won["foreign"]),
        "institution_eok": _eok(won["institution"]),
        "personal_eok": _eok(won["personal"]),
    }


def trailing_average(values: list[float], sessions: int = AVERAGE_SESSIONS) -> list[float | None]:
    """Mean of a cumulative line over the last `sessions` stored points.

    Four weeks means 20 stored trading sessions, not 20 calendar days.
    The average is of the cumulative level, not of the daily net buys.
    Points before `sessions` observations are missing.
    """
    averaged: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < sessions:
            averaged.append(None)
            continue
        window = values[index + 1 - sessions:index + 1]
        averaged.append(sum(window) / sessions)
    return averaged


def select_window(rows: list[dict], today: str) -> list[dict]:
    start = window_start(today)
    chosen = {}
    for row in rows:
        day = row.get("date")
        if isinstance(day, str) and start <= day <= today:
            chosen[day] = row
    return [chosen[day] for day in sorted(chosen)]


def kst_date(now: datetime) -> str:
    return now.astimezone(KST).date().isoformat()


# --- Naver fetch -------------------------------------------------------------

def http_get(url: str, *, timeout: float = 15.0, attempts: int = 3, backoff: float = 2.0,
             opener=urllib.request.urlopen, sleep=clock.sleep) -> bytes:
    """GET with a bounded retry on timeouts, 429 and 5xx. Other 4xx fail at once."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with opener(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code != 429 and exc.code < 500:
                break
        except OSError as exc:  # URLError and TimeoutError are OSError subclasses
            last = exc
        if attempt < attempts:
            sleep(backoff * attempt)
    raise RuntimeError(f"Naver request failed after {attempts} attempts: {url}: {last}")


def fetch_sessions(market: str, asof: str, count: int, *, get=http_get, raw_dir: Path | None = None,
                   delay: float = 1.0, sleep=clock.sleep) -> tuple[list[dict], list[str]]:
    """Newest `count` sessions up to `asof`, as net won per group, plus unusable dates.

    A day without the required investor codes is returned in the skipped list and is
    never turned into zeros.
    """
    if count <= 0:
        return [], []
    bizdate = asof.replace("-", "")
    found: dict[str, dict] = {}
    skipped: list[str] = []
    size = min(PAGE_SIZE, count)
    pages = -(-count // size)
    for page in range(pages):
        # Naver uses page * size as the start offset. Keep the stride constant.
        if page or market != MARKETS[0]:
            sleep(delay)
        body = get(trend_url(market, bizdate, page, size))
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"{market}_{bizdate}_p{page}.json").write_bytes(body)
        try:
            content = json.loads(body)["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"{market} page {page}: unexpected Naver response") from exc
        if not isinstance(content, list):
            raise RuntimeError(f"{market} page {page}: unexpected Naver response")
        for item in content:
            won = parse_day_won(item)
            if won is None:
                bad = str(item.get("bizdate")) if isinstance(item, dict) else "?"
                skipped.append(bad)
                continue
            if won["date"] in found and found[won["date"]] != won:
                raise RuntimeError(f"{market} {won['date']}: conflicting duplicate in Naver pages")
            found[won["date"]] = won
        if len(content) < size:
            break
    return [found[day] for day in sorted(found)[-count:]], sorted(set(skipped))


def to_eok_row(won: dict) -> dict:
    return {
        "date": won["date"],
        "foreign_eok": _eok(won["foreign"]),
        "institution_eok": _eok(won["institution"]),
        "personal_eok": _eok(won["personal"]),
    }


# --- merge and validation ----------------------------------------------------

def _same(left: dict, right: dict) -> bool:
    return all(abs(left[field] - right[field]) < 0.05 for field in FIELDS)


def merge_sessions(stored: list[dict], fetched: list[dict], start: str, end: str,
                   provisional: set[str], now_iso: str,
                   market: str) -> tuple[list[dict], list[dict], list[str], list[str]]:
    """Merge fetched rows into stored rows inside [start, end].

    Returns rows, revisions, added dates, refreshed provisional dates. A stored row is
    replaced only when Naver now reports a different value; that is written to revisions
    unless the row was still provisional. Rows outside the window are dropped, rows are
    never invented.
    """
    rows = {row["date"]: row for row in stored if start <= row["date"] <= end}
    revisions: list[dict] = []
    added: list[str] = []
    refreshed: list[str] = []
    for row in fetched:
        day = row["date"]
        if not start <= day <= end:
            continue
        if day not in rows:
            rows[day] = row
            added.append(day)
        elif not _same(rows[day], row):
            if day in provisional:
                refreshed.append(day)
            else:
                revisions.append({
                    "market": market, "date": day, "detected_at": now_iso,
                    "previous": {field: rows[day][field] for field in FIELDS},
                    "new": {field: row[field] for field in FIELDS},
                })
            rows[day] = row
    return [rows[day] for day in sorted(rows)], revisions, added, refreshed


def xkrx_sessions(start: str, end: str) -> list[str]:
    """XKRX sessions with the repo's corrected holiday list, loaded without the model package."""
    path = ROOT / "src/roboquant/index_forecast/market_calendar.py"
    spec = importlib.util.spec_from_file_location("_flow_market_calendar", path)
    if spec is None or spec.loader is None:
        raise ImportError("shared market calendar unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, labels = module.exchange_schedule("XKRX", start, end)
    return [label.date().isoformat() for label in labels]


def validate_payload(payload: dict, sessions_fn=xkrx_sessions) -> list[str]:
    """Return every problem found; an empty list means the file may be written."""
    errors: list[str] = []
    if payload.get("schema_version") != "kospi_investor_flow_chart_v1" or payload.get("unit") != "억원":
        errors.append("schema_version or unit changed")
    if payload.get("production_weight") != 0 or payload.get("average_sessions") != AVERAGE_SESSIONS:
        errors.append("production_weight must be 0 and average_sessions 20")
    start = payload.get("window_start")
    if not isinstance(start, str):
        return [*errors, "window_start missing"]
    for market in MARKETS:
        rows = (payload.get("markets") or {}).get(market, {}).get("sessions")
        if not isinstance(rows, list) or not MIN_ROWS <= len(rows) <= MAX_ROWS:
            errors.append(f"{market}: session count outside {MIN_ROWS}..{MAX_ROWS}")
            continue
        previous = ""
        for row in rows:
            day = row.get("date") if isinstance(row, dict) else None
            try:
                date.fromisoformat(day)
            except (TypeError, ValueError):
                errors.append(f"{market}: invalid date {day!r}")
                break
            if day <= previous:
                errors.append(f"{market}: {day} is duplicated or out of order")
            if day < start:
                errors.append(f"{market}: {day} is before window_start {start}")
            previous = day
            for field in FIELDS:
                value = row.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    errors.append(f"{market} {day}: {field} is not a finite number")
        if errors:
            continue
        expected = set(sessions_fn(start, rows[-1]["date"]))
        observed = {row["date"] for row in rows}
        missing = sorted(expected - observed)
        if missing:
            errors.append(f"{market}: missing XKRX sessions {missing[:5]}{'...' if len(missing) > 5 else ''}")
        unexpected = sorted(observed - expected)
        if unexpected:
            errors.append(f"{market}: non-XKRX sessions {unexpected[:5]}")
    provisional = payload.get("provisional_dates", [])
    if not isinstance(provisional, list):
        errors.append("provisional_dates must be a list")
    return errors


def guard_against_previous(previous: dict | None, payload: dict) -> list[str]:
    """Stored history only grows: no regression, no silent edits, no dropped in-window rows."""
    if not previous:
        return []
    errors: list[str] = []
    start = payload["window_start"]
    migrating = previous.get("window_label") == LEGACY_WINDOW_LABEL and payload.get("window_label") == WINDOW_LABEL
    if start < previous.get("window_start", "") and not migrating:
        errors.append("window_start moved backwards")
    revised = {(item["market"], item["date"]) for item in payload.get("revisions", [])}
    provisional = set(previous.get("provisional_dates", []))
    for market in MARKETS:
        old = {row["date"]: row for row in previous.get("markets", {}).get(market, {}).get("sessions", [])}
        new = {row["date"]: row for row in payload["markets"][market]["sessions"]}
        if old and max(new) < max(old):
            errors.append(f"{market}: newest session moved backwards")
        changed = 0
        for day, row in old.items():
            if day < start:
                continue
            if day not in new:
                errors.append(f"{market}: stored session {day} was dropped")
            elif not _same(row, new[day]):
                if day in provisional:
                    continue
                if (market, day) not in revised:
                    errors.append(f"{market}: stored session {day} changed without a revision record")
                changed += 1
        if changed > MAX_REVISIONS:
            errors.append(f"{market}: {changed} stored sessions changed; refusing a bulk rewrite")
    return errors


def load_previous(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"seed file is not valid JSON: {path}") from exc
    if payload.get("schema_version") != "kospi_investor_flow_chart_v1":
        raise ValueError(f"seed file has an unknown schema: {path}")
    return payload


def build_payload(markets: dict, *, start: str, received: str, source: str,
                  provisional: list[str], revisions: list[dict]) -> dict:
    return {
        "schema_version": "kospi_investor_flow_chart_v1",
        "unit": "억원",
        "source": source,
        "source_url": SOURCE_URL,
        "source_endpoint": ENDPOINT,
        "snapshot_received_at": received,
        "production_weight": 0.0,
        "window_start": start,
        "window_label": WINDOW_LABEL,
        "average_sessions": AVERAGE_SESSIONS,
        "provisional_dates": provisional,
        "revisions": revisions,
        "markets": {market: {"sessions": rows} for market, rows in markets.items()},
    }


def update_from_naver(previous: dict | None, *, now: datetime, get=http_get, raw_dir: Path | None = None,
                      delay: float = 1.0, sleep=clock.sleep, sessions_fn=xkrx_sessions) -> dict:
    """Merge the newest Naver sessions into the stored file. Returns result with payload and notes."""
    asof = kst_date(now)
    start = window_start(asof)
    expected = sessions_fn(start, asof)
    now_iso = now.astimezone(UTC).isoformat()
    cutoff = datetime.combine(date.fromisoformat(asof), FINAL_AFTER, tzinfo=KST)
    today_is_provisional = now.astimezone(KST) < cutoff
    old_provisional = set(previous.get("provisional_dates", [])) if previous else set()
    stored = {market: (previous["markets"][market]["sessions"] if previous else []) for market in MARKETS}
    markets: dict[str, list[dict]] = {}
    revisions: list[dict] = []
    refreshed_any = False
    summary: dict = {}
    won_rows: dict[str, list[dict]] = {}
    for market in MARKETS:
        rows = [row for row in stored[market] if start <= row["date"] <= asof]
        stored_dates = {row["date"] for row in rows}
        overlap = [day for day in expected if day in stored_dates][-REFETCH_SESSIONS:]
        missing = [day for day in expected if day not in stored_dates]
        earliest = min(overlap + missing, default=start)
        count = sum(day >= earliest for day in expected)
        count = max(REFETCH_SESSIONS, min(count, MAX_FETCH))
        fetched_won, skipped = fetch_sessions(market, asof, count, get=get, raw_dir=raw_dir,
                                              delay=delay, sleep=sleep)
        won_rows[market] = fetched_won
        fetched = [to_eok_row(item) for item in fetched_won if item["date"] <= asof]
        if not fetched:
            raise ValueError(f"{market}: Naver returned no usable sessions through {asof}")
        merged, found, added, refreshed = merge_sessions(
            rows, fetched, start, asof, old_provisional, now_iso, market)
        markets[market] = merged
        revisions.extend(found)
        refreshed_any = refreshed_any or bool(refreshed)
        summary[market] = {"requested": count, "fetched": len(fetched), "added": added,
                           "revised": [item["date"] for item in found], "skipped": skipped,
                           "last": merged[-1]["date"] if merged else None}
    if len(revisions) > MAX_REVISIONS * len(MARKETS):
        raise ValueError(f"{len(revisions)} stored sessions changed; refusing a bulk rewrite")
    last = max((rows[-1]["date"] for rows in markets.values() if rows), default="")
    # Only a session received on its own day before 20:30 KST can still change.
    provisional = [last] if today_is_provisional and last == asof else []
    kept = [item for item in (previous or {}).get("revisions", []) if item["date"] >= start]
    payload = build_payload(markets, start=start, received=now_iso, source=SOURCE_TAG,
                            provisional=provisional, revisions=[*kept, *revisions][-20:])
    changed = bool(not previous or previous.get("window_start") != start
                   or previous.get("window_label") != WINDOW_LABEL
                   or revisions or refreshed_any or provisional != sorted(old_provisional)
                   or any(info["added"] for info in summary.values()))
    errors = validate_payload(payload, lambda first, last: [day for day in expected if first <= day <= last])
    if not errors:
        errors = guard_against_previous(previous, payload)
    return {"payload": payload, "changed": changed, "errors": errors, "summary": summary,
            "won": won_rows, "asof": asof, "provisional": provisional}


# --- local sqlite path (history only on the developer machine) -----------------

def sessions_from_db(conn: sqlite3.Connection, market: str, start: str, end: str) -> list[dict]:
    raw = conn.execute(
        """
        SELECT session_date, investor_type, net_value
        FROM investor_flow
        WHERE market = ? AND session_date >= ? AND session_date <= ?
        ORDER BY session_date
        """,
        (market, start, end),
    ).fetchall()
    grouped: dict[str, dict[str, float]] = {}
    for session_date, investor_type, net_value in raw:
        field = INVESTOR_FIELDS.get(str(investor_type))
        if field is None or net_value is None:
            continue
        grouped.setdefault(str(session_date), {})[field] = _eok(float(net_value))
    sessions = []
    for day in sorted(grouped):
        slot = grouped[day]
        if set(FIELDS) <= set(slot):
            sessions.append({
                "date": day,
                "foreign_eok": slot["foreign_eok"],
                "institution_eok": slot["institution_eok"],
                "personal_eok": slot["personal_eok"],
            })
    return sessions


def _stored_bounds(conn: sqlite3.Connection, market: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT MIN(session_date), MAX(session_date) FROM investor_flow WHERE market = ?",
        (market,),
    ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def collect(received_at: str | None = None, today: str | None = None, db_path: str | Path | None = None) -> dict:
    received = received_at or datetime.now(UTC).isoformat()
    asof = today or kst_date(datetime.now(UTC))
    start = window_start(asof)
    database = Path(db_path) if db_path else DEFAULT_DB
    if not database.exists():
        raise ValueError(f"investor_flow database is missing: {database}")
    conn = sqlite3.connect(database)
    try:
        markets = {}
        for market in MARKETS:
            earliest, latest = _stored_bounds(conn, market)
            if earliest is None or earliest > start:
                raise ValueError(f"{market} stored investor_flow starts {earliest}, after {start}")
            if latest is None or latest < start:
                raise ValueError(f"{market} stored investor_flow ends {latest}, before {start}")
            sessions = sessions_from_db(conn, market, start, asof)
            if len(sessions) < MIN_ROWS:
                raise ValueError(f"{market} stored investor history is too short for the {WINDOW_LABEL} chart")
            markets[market] = sessions
    finally:
        conn.close()
    return build_payload(markets, start=start, received=received, source=f"index_forecast.sqlite:investor_flow({SOURCE_TAG})",
                         provisional=[], revisions=[])


# --- command line ------------------------------------------------------------

def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def summarize(path: Path, payload: dict, **extra) -> dict:
    summary = {"output": str(path), "window_start": payload["window_start"],
               "provisional_dates": payload.get("provisional_dates", []), **extra}
    for market, body in payload["markets"].items():
        sessions = body["sessions"]
        summary[market] = {"sessions": len(sessions), "start": sessions[0]["date"], "end": sessions[-1]["date"]}
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", default=None, help="stored JSON to merge into (default: --output)")
    parser.add_argument("--sqlite", "--database", dest="sqlite", default=None,
                        help="rebuild from a local investor_flow table instead of asking Naver")
    parser.add_argument("--raw-dir", default=None, help="keep the raw Naver responses here")
    parser.add_argument("--now", default=None, help="ISO time with offset, for rehearsals and tests")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between Naver requests")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, get=http_get) -> int:
    args = parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(UTC)
    if now.tzinfo is None:
        print("--now needs an explicit offset", file=sys.stderr)
        return 1
    output = Path(args.output)
    seed = Path(args.seed) if args.seed else output
    try:
        previous = load_previous(seed)
        if args.sqlite:
            payload = collect(received_at=now.astimezone(UTC).isoformat(), today=kst_date(now), db_path=args.sqlite)
            errors = validate_payload(payload, xkrx_sessions)
            if not errors:
                errors = guard_against_previous(previous, payload)
            result = {"payload": payload, "changed": True, "errors": errors, "summary": {}}
        else:
            result = update_from_naver(previous, now=now, get=get, sessions_fn=xkrx_sessions,
                                       raw_dir=Path(args.raw_dir) if args.raw_dir else None,
                                       delay=args.delay)
    except (RuntimeError, ValueError, sqlite3.Error, ImportError, OSError) as exc:
        print(f"unchanged: {exc}", file=sys.stderr)
        return 1
    if result["errors"]:
        print("unchanged: " + "; ".join(result["errors"]), file=sys.stderr)
        return 1
    # An unchanged run keeps the stored file as it is; a rehearsal into another
    # path still receives a copy of it.
    published = result["payload"] if result["changed"] else previous
    if result["changed"] or output != seed or not output.is_file():
        write_json_atomic(output, published)
    print(json.dumps(summarize(output, published, changed=result["changed"], detail=result["summary"]),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
