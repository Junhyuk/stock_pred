"""Bounded official US and Korean government-yield snapshot collection.

Observation dates are capped by ``as_of`` in each market's timezone. Receipt
is the actual download completion time; callers must apply their own later
receipt cutoff. Source history is a current snapshot, not archived vintages.
"""

from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

US_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value={year}"
)
ECOS_URL = (
    "https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/100/817Y002/D/{start}/{end}/{code}"
)
ECOS_ITEMS = {
    "010190000": (1, "국고채(1년)"),
    "010200000": (3, "국고채(3년)"),
    "010210000": (10, "국고채(10년)"),
}
MAX_BYTES = 2_000_000
TIMEOUT_SECONDS = 8
LIMIT = 10


def _download(url: str) -> tuple[bytes, str]:
    try:
        with urlopen(
            Request(url, headers={"User-Agent": "stock-pred-official-data/1.0"}),
            timeout=TIMEOUT_SECONDS,
        ) as response:
            raw = response.read(MAX_BYTES + 1)
        received = datetime.now(UTC).isoformat()
        if len(raw) > MAX_BYTES:
            raise ValueError("Official data response is too large")
        return raw, received
    except Exception:
        # The ECOS URL embeds a credential. Never propagate URL or response text.
        raise ValueError("Official yield request failed") from None


def _day(as_of: datetime, zone: str) -> date:
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise ValueError("as_of must be a timezone-aware datetime")
    return as_of.astimezone(ZoneInfo(zone)).date()


def _record(series: str, day: date, value: float, source: str, received: str) -> dict:
    return {
        "market": "GLOBAL",
        "series": series,
        "observation_date": day.isoformat(),
        "value": value,
        "unit": "percent",
        "source": source,
        "received_at": received,
        "published_at": None,
        "is_final": True,
        "availability_policy": "historical_dated_yield",
    }


def _parse_treasury(
    content: bytes, received: str, last_day: date, *, maturities: tuple[int, ...] = (1, 3, 10),
) -> dict:
    ns = {
        "a": "http://www.w3.org/2005/Atom",
        "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
        "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
    }
    try:
        root = ElementTree.fromstring(content)
        observations = {}
        for entry in root.findall("a:entry", ns):
            p = entry.find(".//m:properties", ns)
            if p is None:
                raise ValueError
            day = date.fromisoformat(p.findtext("d:NEW_DATE", namespaces=ns)[:10])
            if day > last_day:
                continue
            values = {}
            for maturity in maturities:
                value = float(p.findtext(f"d:BC_{maturity}YEAR", namespaces=ns))
                if not math.isfinite(value):
                    raise ValueError
                values[maturity] = value
            if day in observations:
                raise ValueError
            observations[day] = (values, received)
        return observations
    except (TypeError, ValueError, ElementTree.ParseError):
        raise ValueError("Invalid US Treasury yield response") from None


def collect_us_yields(*, as_of: datetime) -> list[dict]:
    day = _day(as_of, "America/New_York")
    raw, received = _download(US_URL.format(year=day.year))
    observations = _parse_treasury(raw, received, day)
    # January often has fewer than ten observations in the current-year feed.
    if day.month == 1 and len(observations) < LIMIT:
        raw, received = _download(US_URL.format(year=day.year - 1))
        previous = _parse_treasury(raw, received, day)
        if observations.keys() & previous.keys():
            raise ValueError("Duplicate Treasury yield observation date")
        observations.update(previous)
    if not observations:
        raise ValueError("No US Treasury daily yields available")
    rows = []
    for obs in sorted(observations)[-LIMIT:]:
        values, receipt = observations[obs]
        rows.extend(
            _record(
                f"US_YIELD_{maturity}Y",
                obs,
                values[maturity],
                "us_treasury_daily_par_yield_curve",
                receipt,
            )
            for maturity in (1, 3, 10)
        )
    return rows


def _parse_ecos(content: bytes, code: str, received: str, first: date, last: date) -> dict:
    try:
        payload = json.loads(content)["StatisticSearch"]
        total = int(payload["list_total_count"])
        raw_rows = payload["row"]
        if not isinstance(raw_rows, list) or not 0 < total == len(raw_rows) <= 100:
            raise ValueError
        maturity, name = ECOS_ITEMS[code]
        observations = {}
        for row in raw_rows:
            if (
                row["STAT_CODE"] != "817Y002"
                or row["ITEM_CODE1"] != code
                or row["ITEM_NAME1"] != name
                or row["UNIT_NAME"] != "연%"
            ):
                raise ValueError
            day = datetime.strptime(row["TIME"], "%Y%m%d").date()
            if day > last:
                continue
            if day < first or day in observations:
                raise ValueError
            value = float(row["DATA_VALUE"])
            if not math.isfinite(value):
                raise ValueError
            observations[day] = _record(
                f"KR_YIELD_{maturity}Y", day, value, "bok_ecos_817Y002", received
            )
        return observations
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise ValueError("Invalid Korean ECOS yield response") from None


def collect_kr_yields(*, as_of: datetime, api_key: str) -> list[dict]:
    # ECOS may expose today's morning quote before the daily closing value.
    # Exclude the current Korean date because the API has no finality flag.
    last = _day(as_of, "Asia/Seoul") - timedelta(days=1)
    if (
        not isinstance(api_key, str)
        or not re.fullmatch(r"[A-Za-z0-9]+", api_key)
        or api_key.lower() == "sample"
    ):
        raise ValueError("An authorized ECOS API key is required")
    first = last - timedelta(days=40)

    def get(code: str) -> dict:
        url = ECOS_URL.format(
            key=api_key, start=first.strftime("%Y%m%d"), end=last.strftime("%Y%m%d"), code=code
        )
        try:
            raw, received = _download(url)
            return _parse_ecos(raw, code, received, first, last)
        except Exception:
            raise ValueError("Korean ECOS yield request failed or returned invalid data") from None

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(get, ECOS_ITEMS))
    date_sets = [set(result) for result in results]
    if not date_sets[0] or any(days != date_sets[0] for days in date_sets[1:]):
        raise ValueError("Korean ECOS yield dates do not match across maturities")
    return [result[day] for day in sorted(date_sets[0])[-LIMIT:] for result in results]
