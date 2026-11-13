"""Bounded collection and point-in-time parsing of official macro sources.

The registry deliberately distinguishes keyless official releases from APIs
that need a registration key. A failed or unsupported parser is recorded as
unavailable instead of being treated as a successful empty response.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import re
import tempfile
import time as monotonic_time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.message import Message
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

_MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1)}
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class _FetchResponse:
    status: int
    headers: Message | dict[str, str]
    body: bytes


class _Text(HTMLParser):
    _BLOCKS = {"br", "div", "h1", "h2", "h3", "h4", "li", "p", "td", "th", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(f" {data.strip()} ")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCKS:
            self.parts.append("\n")


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            label = " ".join(" ".join(self._text).split())
            self.links.append((self._href, label))
            self._href = None
            self._text = []


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[tuple[int | None, list[list[str]]]] = []
        self.outside: list[str] = []
        self.year: int | None = None
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.cell: list[str] = []
        self.in_table = False
        self.in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            years = re.findall(r"\b(20\d{2})\b", " ".join(self.outside))
            self.year = int(years[-1]) if years else None
            self.in_table = True
            self.rows = []
        elif self.in_table and tag == "tr":
            self.row = []
        elif self.in_table and tag in {"td", "th"}:
            self.in_cell = True
            self.cell = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell.append(data.strip())
        elif not self.in_table and data.strip():
            self.outside.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if self.in_table and tag in {"td", "th"} and self.in_cell:
            self.row.append(" ".join(part for part in self.cell if part))
            self.in_cell = False
            self.cell = []
        elif self.in_table and tag == "tr":
            if self.row:
                self.rows.append(self.row)
            self.row = []
        elif tag == "table" and self.in_table:
            self.tables.append((self.year, self.rows))
            self.in_table = False
            self.rows = []


def _text(body: bytes) -> str:
    parser = _Text()
    head = body[:1024].decode("ascii", errors="ignore")
    charset = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", head, re.I)
    encoding = charset.group(1) if charset else "utf-8"
    parser.feed(body.decode(encoding, errors="replace"))
    raw = "".join(parser.parts)
    raw = re.sub(r"[ \t\f\v]+", " ", raw)
    return re.sub(r" *\n *", "\n", raw).strip()


def _header(headers: Message | dict[str, str], key: str) -> str | None:
    try:
        value = headers.get(key)
    except AttributeError:
        return None
    return str(value) if value is not None else None


def _follow_url(body: bytes, base_url: str, label_pattern: str) -> str | None:
    links = _Links()
    head = body[:1024].decode("ascii", errors="ignore")
    charset = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", head, re.I)
    links.feed(body.decode(charset.group(1) if charset else "utf-8", errors="replace"))
    pattern = re.compile(label_pattern, re.I)
    for href, label in links.links:
        if label and pattern.search(label):
            if href.startswith("javascript:"):
                wrapped = re.search(r"(?:addSearchParam|location\.href)\(['\"]([^'\"]+)['\"]\)", href)
                if wrapped is None:
                    continue
                href = wrapped.group(1)
            return urljoin(base_url, href)
    return None


def _default_fetch(url: str, timeout_seconds: float) -> _FetchResponse:
    request = Request(url, headers={"User-Agent": "roboquant-economic-source/1.0"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError("response_too_large")
            return _FetchResponse(response.status, response.headers, body)
    except HTTPError as error:
        body = error.read(_MAX_RESPONSE_BYTES + 1)
        return _FetchResponse(error.code, error.headers, body[:_MAX_RESPONSE_BYTES])


def _isolated_fetch_worker(channel: Any, url: str, timeout_seconds: float,
                           body_path: str) -> None:
    """Run the default HTTP client out of process so slow streaming can be killed."""
    try:
        response = _default_fetch(url, timeout_seconds)
        with open(body_path, "wb") as stream:
            stream.write(response.body)
        channel.send(("ok", response.status, dict(response.headers.items())))
    except BaseException as error:
        channel.send(("error", type(error).__name__, str(error)))
    finally:
        channel.close()


def _wall_bounded_fetch(url: str, timeout_seconds: float) -> _FetchResponse:
    """Enforce a wall-clock limit around urllib, including trickle responses."""
    started = monotonic_time.monotonic()
    descriptor, body_path = tempfile.mkstemp(prefix="roboquant-source-")
    os.close(descriptor)
    parent = child = process = None
    try:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=_isolated_fetch_worker,
                                  args=(child, url, timeout_seconds, body_path), daemon=False)
        process.start()
        child.close()
        child = None
        remaining = max(0.0, timeout_seconds - (monotonic_time.monotonic() - started))
        process.join(remaining)
        if process.is_alive():
            process.terminate()
            process.join(0.1)
            if process.is_alive():
                process.kill()
                process.join()
            raise TimeoutError("source_wall_timeout")
        if not parent.poll():
            raise OSError("isolated_fetch_failed")
        payload = parent.recv()
        if payload[0] == "ok":
            _, status, headers = payload
            with open(body_path, "rb") as stream:
                body = stream.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError("response_too_large")
            return _FetchResponse(int(status), headers, body)
        error_type, message = payload[1], payload[2]
        if error_type in {"TimeoutError", "URLError"} and "timed out" in message.lower():
            raise TimeoutError("source_timeout")
        raise OSError(f"{error_type}:{message}")
    finally:
        if process is not None and process.is_alive():
            process.terminate()
            process.join(0.1)
            if process.is_alive():
                process.kill()
                process.join()
        if child is not None:
            child.close()
        if parent is not None:
            parent.close()
        try:
            os.unlink(body_path)
        except FileNotFoundError:
            pass


def _month_year(text: str) -> str | None:
    match = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\b", text, re.I)
    if match:
        return f"{int(match.group(2)):04d}-{_MONTHS[match.group(1).lower()]:02d}"
    japanese = re.search(r"(20\d{2})年\s*(?:（[^）]*）|\([^)]*\))?\s*(\d{1,2})月分", text)
    if japanese:
        return f"{int(japanese.group(1)):04d}-{int(japanese.group(2)):02d}"
    japanese = re.search(r"(20\d{2})年\s*(\d{1,2})月", text)
    if japanese:
        return f"{int(japanese.group(1)):04d}-{int(japanese.group(2)):02d}"
    return None


def _actual_time(text: str, timezone: str = "America/New_York") -> str | None:
    patterns = (
        r"(?:embargoed until|release at|released at)\s+(\d{1,2}:\d{2})\s*(a\.m\.|p\.m\.|AM|PM)?\s*(?:\((?:ET|EST|EDT)\)|(?:ET|EST|EDT))?[, ]+\w+day[,]?\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(20\d{2})",
        r"(?:embargoed until|release at|released at)\s+(\d{1,2}:\d{2})\s*(a\.m\.|p\.m\.|AM|PM)?\s*(?:\((?:ET|EST|EDT)\)|(?:ET|EST|EDT))?[, ]+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(20\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match is None:
            continue
        clock, meridiem = match.group(1), (match.group(2) or "").lower()
        month, day_text, year = match.group(3), match.group(4), match.group(5)
        hour, minute = (int(part) for part in clock.split(":"))
        if meridiem.startswith("p") and hour < 12:
            hour += 12
        if meridiem.startswith("a") and hour == 12:
            hour = 0
        try:
            stamp = datetime(int(year), _MONTHS[month.lower()], int(day_text), hour, minute,
                             tzinfo=ZoneInfo(timezone))
        except ValueError:
            return None
        return stamp.isoformat()
    return None


def _release(source: dict[str, Any], now: datetime, body_hash: str, measure: str,
             value: float, unit: str, reference_period: str | None, *,
             actual_release_at: str | None = None, actual_release_date: str | None = None,
             vintage: str = "current_snapshot", estimate: bool | None = None,
             seasonal_adjustment: str = "unknown", revision_of: str | None = None,
             actual_release_timezone: str | None = None) -> dict[str, Any]:
    actual_release_at_utc = None
    if actual_release_at:
        actual_release_at_utc = datetime.fromisoformat(actual_release_at).astimezone(UTC).isoformat()
    measure_definition = source.get("measure_definitions", {}).get(measure)
    if measure_definition is None:
        definition_base = source.get("definition_version")
        if definition_base:
            measure_definition = f"{definition_base}:{measure}"
    return {
        "event_id": f"{source['source_id']}:{measure}:{reference_period or 'unknown'}",
        "country": source["country"],
        "measure": measure,
        "value": float(value),
        "unit": unit,
        "reference_period": reference_period,
        "actual_release_at": actual_release_at,
        "actual_release_at_utc": actual_release_at_utc,
        "actual_release_date": actual_release_date,
        "actual_release_timezone": actual_release_timezone,
        "first_seen_at": now.isoformat(),
        "vintage": vintage,
        "original_publication_verified": bool(source.get("original_publication_verified", False)),
        "numeric_eligibility": (
            "observed_current_receipt_v1" if measure_definition else "not_ready_unknown_definition"
        ),
        "definition_version": measure_definition,
        "estimate": estimate,
        "seasonal_adjustment": seasonal_adjustment,
        "source_id": source["source_id"],
        "source_url": source["source_url"],
        "source_hash": body_hash,
        "revision_of": revision_of,
    }


def _event(source: dict[str, Any], now: datetime, body_hash: str, event_type: str,
           title: str, scheduled_date: str | None, *, timezone: str,
           scheduled_time: str | None = None, status: str = "tentative",
           reference_period: str | None = None, precision: str | None = None) -> dict[str, Any]:
    stamp = None
    stamp_utc = None
    if scheduled_date and scheduled_time:
        hour, minute = (int(part) for part in scheduled_time.split(":"))
        stamp = datetime.fromisoformat(scheduled_date).replace(
            hour=hour, minute=minute, tzinfo=ZoneInfo(timezone)).isoformat()
        stamp_utc = datetime.fromisoformat(stamp).astimezone(UTC).isoformat()
    identifier = (source.get("event_uid") or reference_period or title)
    return {
        "event_id": f"{source['source_id']}:{event_type}:{identifier}",
        "event_id_basis": "source_uid" if source.get("event_uid") else (
            "source_type_reference_period" if reference_period else "ambiguous_title_only"
        ),
        "country": source["country"],
        "event_type": event_type,
        "title": title,
        "scheduled_at": stamp,
        "scheduled_at_utc": stamp_utc,
        "scheduled_date": scheduled_date,
        "timezone": timezone,
        "time_precision": precision or ("datetime" if stamp else "date" if scheduled_date else "tba"),
        "status": status,
        "published_at": None,
        "first_seen_at": now.isoformat(),
        "reference_period": reference_period,
        "source_id": source["source_id"],
        "source_url": source["source_url"],
        "source_hash": body_hash,
    }


def _numeric(match: re.Match[str], index: int = 1) -> float:
    return float(match.group(index).replace(",", ""))


def _parse_bls_cpi(source: dict[str, Any], now: datetime, body_hash: str,
                   text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    ref = _month_year(re.search(r"CONSUMER PRICE INDEX\s*[-–]\s*([^.<]+)", text, re.I).group(1)
                      if re.search(r"CONSUMER PRICE INDEX\s*[-–]\s*([^.<]+)", text, re.I) else text)
    actual_at = _actual_time(text)
    releases: list[dict] = []
    unavailable: list[str] = []
    headline = re.search(r"CPI-U\) increased\s+([\d.]+)\s+percent on a seasonally adjusted basis in\s+\w+", text, re.I)
    core = re.search(r"all items less food and energy rose\s+([\d.]+)\s+percent in\s+\w+", text, re.I)
    if headline:
        releases.append(_release(source, now, body_hash, "cpi_headline_mom", _numeric(headline),
                                 "percent", ref, actual_release_at=actual_at,
                                 vintage="first_release",
                                 seasonal_adjustment="SA",
                                 actual_release_timezone="America/New_York"))
    if core:
        releases.append(_release(source, now, body_hash, "cpi_core_mom", _numeric(core),
                                 "percent", ref, actual_release_at=actual_at,
                                 vintage="first_release",
                                 seasonal_adjustment="SA",
                                 actual_release_timezone="America/New_York"))
    if "cannot publish 12-month percent changes" in text.lower():
        unavailable.append("cpi_headline_yoy:12-month_base_period_unavailable")
    if not releases:
        unavailable.append("headline_and_core_monthly_values_not_found")
    return [], releases, unavailable, None


def _parse_bls_employment(source: dict[str, Any], now: datetime, body_hash: str,
                          text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    ref = _month_year(re.search(r"THE EMPLOYMENT SITUATION\s*[-–]\s*([^.<]+)", text, re.I).group(1)
                      if re.search(r"THE EMPLOYMENT SITUATION\s*[-–]\s*([^.<]+)", text, re.I) else text)
    actual_at = _actual_time(text)
    releases: list[dict] = []
    change = re.search(r"Total nonfarm payroll employment\s+(rose|increased|fell|decreased)\s+by\s+([\d,]+)\s+in\s+\w+", text, re.I)
    if change:
        value = _numeric(change, 2) * (-1 if change.group(1).lower() in {"fell", "decreased"} else 1)
        releases.append(_release(source, now, body_hash, "nfp_monthly_change", value / 1000,
                                 "thousand_persons", ref, actual_release_at=actual_at,
                                 vintage="first_release",
                                 seasonal_adjustment="SA",
                                 actual_release_timezone="America/New_York"))
    rate = re.search(r"unemployment rate\s+(?:changed little at|was|stood at|is)\s+([\d.]+)\s*percent", text, re.I)
    if rate:
        releases.append(_release(source, now, body_hash, "unemployment_rate", _numeric(rate),
                                 "percent", ref, actual_release_at=actual_at,
                                 vintage="first_release",
                                 seasonal_adjustment="SA",
                                 actual_release_timezone="America/New_York"))
    unavailable = [] if releases else ["nfp_or_unemployment_value_not_found"]
    return [], releases, unavailable, None


def _parse_bls_series(source: dict[str, Any], now: datetime, body_hash: str,
                      body: bytes) -> tuple[list[dict], list[dict], list[str], str | None]:
    payload = json.loads(body)
    if payload.get("status") != "REQUEST_SUCCEEDED":
        messages = payload.get("message", [])
        return [], [], [], f"bls_api_{payload.get('status', 'unknown')}:{'|'.join(messages)}"
    series_list = payload.get("Results", {}).get("series", [])
    if len(series_list) != 1:
        return [], [], [], "bls_api_expected_one_series"
    series = series_list[0]
    expected_series_id = source.get("series_id")
    if expected_series_id and series.get("seriesID") != expected_series_id:
        return [], [], [], "bls_api_series_id_mismatch"
    observations: dict[tuple[int, int], float] = {}
    for row in series.get("data", []):
        period_match = re.fullmatch(r"M(0[1-9]|1[0-2])", str(row.get("period", "")))
        if period_match is None:
            continue
        try:
            key = (int(row["year"]), int(period_match.group(1)))
            observations[key] = float(row["value"])
        except (KeyError, ValueError, TypeError):
            continue
    if not observations:
        return [], [], [], "bls_api_monthly_observations_missing"
    latest_period = max(observations)
    latest_value = observations[latest_period]
    reference_period = f"{latest_period[0]:04d}-{latest_period[1]:02d}"
    releases: list[dict] = []
    unavailable: list[str] = []
    for measure in source.get("measures", []):
        name = measure["measure"]
        transform = measure["transform"]
        if transform == "level":
            value = latest_value
        elif transform == "difference_mom":
            previous_period = (latest_period[0] - 1, 12) if latest_period[1] == 1 else (
                latest_period[0], latest_period[1] - 1)
            if previous_period not in observations:
                unavailable.append(f"{name}:no_matching_previous_month")
                continue
            value = latest_value - observations[previous_period]
        elif transform in {"pct_change_mom", "pct_change_yoy"}:
            if transform == "pct_change_mom":
                previous_period = (latest_period[0] - 1, 12) if latest_period[1] == 1 else (
                    latest_period[0], latest_period[1] - 1)
            else:
                previous_period = (latest_period[0] - 1, latest_period[1])
            if previous_period not in observations:
                unavailable.append(f"{name}:no_matching_reference_month")
                continue
            value = round((latest_value / observations[previous_period] - 1) * 100, 1)
        else:
            unavailable.append(f"{name}:unknown_transform:{transform}")
            continue
        definition = measure.get("definition_version")
        if definition:
            source.setdefault("measure_definitions", {})[name] = definition
        releases.append(_release(source, now, body_hash, name, value,
                                 measure.get("unit", "unknown"), reference_period,
                                 vintage="current_snapshot",
                                 seasonal_adjustment=source.get("seasonal_adjustment", "unknown")))
        if measure.get("derivation"):
            releases[-1]["derivation"] = measure["derivation"]
    return [], releases, unavailable, None if releases else "bls_api_no_configured_measure_parsed"


def _parse_eurostat_api(source: dict[str, Any], now: datetime, body_hash: str,
                        body: bytes) -> tuple[list[dict], list[dict], list[str], str | None]:
    payload = json.loads(body)
    dimensions = payload.get("dimension", {})
    ids = payload.get("id", [])
    sizes = payload.get("size", [])
    if len(ids) != len(sizes) or "time" not in ids:
        return [], [], [], "eurostat_jsonstat_dimensions_missing"
    time_index = dimensions.get("time", {}).get("category", {}).get("index", {})
    if not isinstance(time_index, dict) or not time_index:
        return [], [], [], "eurostat_time_dimension_missing"
    observations = payload.get("value", {})
    time_dimension_index = ids.index("time")
    for dimension_id, dimension_size in zip(ids, sizes, strict=True):
        if dimension_id != "time":
            if int(dimension_size) == 0:
                return [], [], [], f"eurostat_no_data_for_dimension:{dimension_id}"
            if int(dimension_size) != 1:
                return [], [], [], f"eurostat_unfiltered_dimension:{dimension_id}"
    value = None
    latest_period = max(time_index)
    for candidate_period, time_position in sorted(
            time_index.items(), key=lambda row: row[0], reverse=True):
        coordinates = [0] * len(ids)
        coordinates[time_dimension_index] = int(time_position)
        value_index = 0
        stride = 1
        for dimension_size, coordinate in zip(reversed(sizes), reversed(coordinates), strict=True):
            value_index += coordinate * stride
            stride *= int(dimension_size)
        candidate = (observations.get(str(value_index)) if isinstance(observations, dict)
                     else observations[value_index] if isinstance(observations, list)
                     and value_index < len(observations) else None)
        if candidate is not None:
            latest_period = candidate_period
            value = candidate
            break
    if value is None:
        return [], [], [f"{source.get('measure', 'measure')}:no_observation_for_{latest_period}"], "no_matching_observations"
    period_match = re.fullmatch(r"(20\d{2})[-M](\d{2})", latest_period)
    reference_period = (f"{period_match.group(1)}-{period_match.group(2)}"
                        if period_match else latest_period)
    measure = source["measure"]
    release = _release(source, now, body_hash, measure, float(value), source.get("unit", "unknown"),
                       reference_period, vintage="current_snapshot",
                       seasonal_adjustment=source.get("seasonal_adjustment", "unknown"))
    release["observation_updated_at"] = payload.get("updated")
    return [], [release], [], None


def _parse_bea_pce(source: dict[str, Any], now: datetime, body_hash: str,
                   text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    ref = _month_year(text)
    releases: list[dict] = []
    actual_release_date = None
    dated_release = re.search(r"(?:Current release|Published|Release date)\s*:?\s*(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(20\d{2})", text, re.I)
    if dated_release:
        actual_release_date = date(int(dated_release.group(3)),
                                   _MONTHS[dated_release.group(1).lower()],
                                   int(dated_release.group(2))).isoformat()

    patterns = (
        ("pce_headline_mom", r"PCE price index for [A-Za-z]+ increased\s+([\d.]+)\s+percent(?! from one year ago)"),
        ("core_pce_mom", r"Excluding food and energy, the PCE price index increased\s+([\d.]+)\s+percent(?! from one year ago)"),
        ("pce_headline_yoy", r"same month one year ago, the PCE price index for [A-Za-z]+ increased\s+([\d.]+)\s+percent"),
        ("core_pce_yoy", r"Excluding food and energy, the PCE price index increased\s+([\d.]+)\s+percent from one year ago"),
    )
    for measure, pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            releases.append(_release(source, now, body_hash, measure, _numeric(match),
                                     "percent", ref, actual_release_date=actual_release_date,
                                     vintage="first_release", seasonal_adjustment="SA"))
    unavailable = [] if releases else ["pce_price_index_values_not_found"]
    if actual_release_date:
        unavailable.append(f"actual_release_date:{actual_release_date};time_unknown")
    return [], releases, unavailable, None


def _parse_fed_policy(source: dict[str, Any], now: datetime, body_hash: str,
                      body: bytes) -> tuple[list[dict], list[dict], list[str], str | None]:
    text = _text(body)
    match = re.search(
        r"target range[^.\n]*?\b(?:is|of)\s+(\d+(?:-\d+/\d+)?|\d+/\d+|[\d.]+)\s*(?:percent|%)?\s+to\s+(\d+(?:-\d+/\d+)?|\d+/\d+|[\d.]+)",
        text, re.I)
    tables = _Tables()
    head = body[:1024].decode("ascii", errors="ignore")
    charset = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", head, re.I)
    tables.feed(body.decode(charset.group(1) if charset else "utf-8", errors="replace"))
    levels = []
    for year, rows in tables.tables:
        if year is None or year > now.year or not rows:
            continue
        header = [cell.strip().lower() for cell in rows[0]]
        try:
            date_column = header.index("date")
            level_column = header.index("level (%)")
        except ValueError:
            continue
        for row in rows[1:]:
            if len(row) <= max(date_column, level_column):
                continue
            row_date = re.fullmatch(
                r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})",
                row[date_column].strip(), re.I)
            rate_range = re.fullmatch(r"\s*([\d.]+)\s*[-–]\s*([\d.]+)\s*", row[level_column])
            if row_date is None or rate_range is None:
                continue
            meeting_date = date(year, _MONTHS[row_date.group(1).lower()], int(row_date.group(2)))
            if meeting_date <= now.date():
                levels.append((meeting_date, float(rate_range.group(1)), float(rate_range.group(2))))
    if not levels:
        if match:
            return [], [], ["policy_rate_reference_date_not_found"], "policy_rate_reference_date_not_found"
        return [], [], ["policy_rate_lower_upper_not_found"], "target_range_not_found"
    meeting_date, lower, upper = max(levels, key=lambda row: row[0])
    rate_reference_date = meeting_date.isoformat()
    releases = []
    for measure, value in (("policy_rate_lower", lower), ("policy_rate_upper", upper),
                           ("policy_rate", (lower + upper) / 2)):
        releases.append(_release(source, now, body_hash, measure, value, "percent", rate_reference_date,
                                 vintage="current_snapshot", seasonal_adjustment="not_applicable"))
        if measure == "policy_rate":
            releases[-1]["derivation"] = "arithmetic_midpoint_of_lower_upper"
    return [], releases, [], None


def _parse_bok_rate(source: dict[str, Any], now: datetime, body_hash: str,
                    text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    match = re.search(r"Base Rate[^.]{0,120}?\b(?:unchanged at|at|to)\s+([\d.]+)\s*%", text, re.I)
    date_match = re.search(r"\b(20\d{2})\.(\d{1,2})\.(\d{1,2})\b", text)
    period = None
    if date_match:
        period = date(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))).strftime("%Y-%m")
    releases = []
    if match:
        releases.append(_release(source, now, body_hash, "policy_rate", _numeric(match), "percent",
                                 period, vintage="first_release", seasonal_adjustment="not_applicable"))
    return [], releases, [] if releases else ["base_rate_not_found"], None


def _parse_mods_cpi(source: dict[str, Any], now: datetime, body_hash: str,
                    text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    ref = _month_year(re.search(r"Consumer Price Index in ([^.]+)", text, re.I).group(1)
                      if re.search(r"Consumer Price Index in ([^.]+)", text, re.I) else text)
    actual_date = re.search(r"Date\s+(20\d{2}-\d{2}-\d{2})", text)
    releases = []
    core_at = re.search(r"index excluding food and energy", text, re.I)
    headline_text = text[:core_at.start()] if core_at else text
    core_text = text[core_at.start():] if core_at else ""
    patterns = (
        ("cpi_headline_mom", r"index increased\s+([\d.]+)\s+percent from the preceding month", headline_text),
        ("cpi_headline_yoy", r"rose\s+([\d.]+)\s+percent from the same month of the previous year", headline_text),
        ("cpi_core_mom", r"index increased\s+([\d.]+)\s+percent from the preceding month", core_text),
        ("cpi_core_yoy", r"rose\s+([\d.]+)\s+percent from the same month of the previous year", core_text),
    )
    for measure, pattern, section in patterns:
        match = re.search(pattern, section, re.I)
        if match:
            releases.append(_release(source, now, body_hash, measure, _numeric(match), "percent",
                                     ref, actual_release_date=actual_date.group(1) if actual_date else None,
                                     vintage="first_release", seasonal_adjustment="NSA"))
    unavailable = [] if releases else ["headline_and_core_cpi_values_not_found"]
    if actual_date:
        unavailable.append(f"actual_release_date:{actual_date.group(1)};time_unknown")
    return [], releases, unavailable, None


def _parse_mods_employment(source: dict[str, Any], now: datetime, body_hash: str,
                           text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    ref = _month_year(re.search(r"Survey in ([^.]+)", text, re.I).group(1)
                      if re.search(r"Survey in ([^.]+)", text, re.I) else text)
    actual_date = re.search(r"Date\s+(20\d{2}-\d{2}-\d{2})", text)
    releases = []
    match = re.search(r"number of employed persons[\s\S]{0,180}?went up\s+([\d,]+)\s+thousand", text, re.I)
    if match:
        releases.append(_release(source, now, body_hash, "employment_yoy_change",
                                 _numeric(match), "thousand_persons", ref,
                                 actual_release_date=actual_date.group(1) if actual_date else None,
                                 vintage="first_release", seasonal_adjustment="NSA"))
    rate = re.search(r"unemployment rate marked\s+([\d.]+)\s*%", text, re.I)
    if rate:
        releases.append(_release(source, now, body_hash, "unemployment_rate", _numeric(rate),
                                 "percent", ref,
                                 actual_release_date=actual_date.group(1) if actual_date else None,
                                 vintage="first_release", seasonal_adjustment="NSA"))
    unavailable = [] if releases else ["employment_and_unemployment_values_not_found"]
    if actual_date:
        unavailable.append(f"actual_release_date:{actual_date.group(1)};time_unknown")
    return [], releases, unavailable, None


def _parse_jp_cpi(source: dict[str, Any], now: datetime, body_hash: str,
                  text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    ref = _month_year(text)
    releases = []
    patterns = (
        ("cpi_headline_yoy", r"総合指数\s*は[\s\S]*?前年同月比は?\s*([\d.]+)[％%]"),
        ("cpi_core_yoy", r"生鮮食品を除く総合指数\s*は[\s\S]*?前年同月比は?\s*([\d.]+)[％%]"),
    )
    for measure, pattern in patterns:
        match = re.search(pattern, text)
        if match:
            releases.append(_release(source, now, body_hash, measure, _numeric(match), "percent",
                                     ref, seasonal_adjustment="NSA"))
    date_match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日公表", text)
    for row in releases:
        row["vintage"] = "current_snapshot"
        row["actual_release_date"] = (
            date(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))).isoformat()
            if date_match else None
        )
    unavailable = [] if releases else ["national_cpi_rates_not_found"]
    if date_match:
        unavailable.append(f"actual_release_date:{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d};time_unknown")
    return [], releases, unavailable, None


def _parse_jp_employment(source: dict[str, Any], now: datetime, body_hash: str,
                         text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    ref = _month_year(text)
    releases = []
    rate = re.search(r"完全失業率（季節調整値）は\s*([\d.]+)％", text)
    if rate:
        releases.append(_release(source, now, body_hash, "unemployment_rate", _numeric(rate),
                                 "percent", ref, vintage="current_snapshot", seasonal_adjustment="SA"))
    employment = re.search(r"就業者数は\s*([\d,.]+)万人。?\s*前年同月に比べ\s*([\d,.]+)万人の(増加|減少)", text)
    if employment:
        value = float(employment.group(2).replace(",", "")) * 10
        if employment.group(3) == "減少":
            value *= -1
        releases.append(_release(source, now, body_hash, "employment_yoy_change", value,
                                 "thousand_persons", ref, vintage="current_snapshot",
                                 seasonal_adjustment="NSA"))
    unavailable = [] if releases else ["employment_and_seasonally_adjusted_unemployment_not_found"]
    date_match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日公表", text)
    if date_match:
        release_date = date(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))).isoformat()
        for row in releases:
            row["actual_release_date"] = release_date
        unavailable.append(f"actual_release_date:{release_date};time_unknown")
    return [], releases, unavailable, None


def _schedule_month(text: str) -> int | None:
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b",
        text, re.I)
    return _MONTHS[match.group(1).lower()] if match else None


def _parse_jp_schedule(source: dict[str, Any], now: datetime, body_hash: str,
                       body: bytes) -> tuple[list[dict], list[dict], list[str], str | None]:
    tables = _Tables()
    head = body[:1024].decode("ascii", errors="ignore")
    charset = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", head, re.I)
    tables.feed(body.decode(charset.group(1) if charset else "utf-8", errors="replace"))
    event_type = "inflation" if source.get("schedule_kind") == "cpi" else "employment"
    title = "Japan national CPI release" if event_type == "inflation" else "Japan Labour Force Survey release"
    events: list[dict] = []
    last_reference_year: int | None = None
    last_reference_month: int | None = None
    allowed_years = {now.year, now.year + 1}
    for _table_year, rows in tables.tables:
        for row in rows:
            if len(row) < 2 or "survey month" in row[0].lower() or "reference month" in row[0].lower():
                continue
            reference_month = _schedule_month(row[0])
            release_month = _schedule_month(row[1])
            if reference_month is None or release_month is None:
                continue
            ref_year_match = re.search(r"\b(20\d{2})\b", row[0])
            if ref_year_match:
                reference_year = int(ref_year_match.group(1))
            else:
                reference_year = last_reference_year or now.year
                if last_reference_month is not None and reference_month < last_reference_month:
                    reference_year += 1
            last_reference_year = reference_year
            last_reference_month = reference_month

            day_match = re.search(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,\s*(20\d{2}))?", row[1], re.I)
            if day_match is None:
                continue
            day = int(day_match.group(1))
            explicit_release_year = day_match.group(2)
            release_year = int(explicit_release_year) if explicit_release_year else reference_year
            if explicit_release_year is None and release_month < reference_month:
                release_year += 1
            if release_year not in allowed_years:
                continue
            scheduled_date = date(release_year, release_month, day).isoformat()
            reference_period = f"{reference_year:04d}-{reference_month:02d}"
            events.append(_event(source, now, body_hash, event_type, title, scheduled_date,
                                 timezone="Asia/Tokyo", status="confirmed",
                                 reference_period=reference_period, precision="date"))
    return events, [], [], None if events else f"no_{now.year}_or_{now.year + 1}_release_rows_found"


def _parse_boj_schedule(source: dict[str, Any], now: datetime, body_hash: str,
                        body: bytes) -> tuple[list[dict], list[dict], list[str], str | None]:
    tables = _Tables()
    head = body[:1024].decode("ascii", errors="ignore")
    charset = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", head, re.I)
    tables.feed(body.decode(charset.group(1) if charset else "utf-8", errors="replace"))
    allowed_years = {now.year, now.year + 1}
    events: list[dict] = []
    for year, rows in tables.tables:
        if year not in allowed_years or not any("date of mpm" in " ".join(row).lower() for row in rows[:3]):
            continue
        meeting_ordinal = 0
        for row in rows:
            if not row or re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d", row[0], re.I) is None:
                continue
            match = re.search(
                r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s*(\d{1,2})\s*\([^)]*\)\s*,?\s*(\d{1,2})",
                row[0], re.I)
            if match is None:
                continue
            month = _MONTHS[next(name for name in _MONTHS if name.startswith(match.group(1)[:3].lower()))]
            decision_day = int(match.group(3))
            scheduled_date = date(year, month, decision_day).isoformat()
            meeting_ordinal += 1
            events.append(_event(source, now, body_hash, "policy_rate",
                                 "Bank of Japan monetary policy meeting decision", scheduled_date,
                                 timezone="Asia/Tokyo", status="confirmed",
                                 reference_period=f"{year}-MPM-{meeting_ordinal:02d}", precision="date"))
    return events, [], [], None if events else f"no_{now.year}_or_{now.year + 1}_meeting_rows_found"


def _parse_ecb_schedule(source: dict[str, Any], now: datetime, body_hash: str,
                        text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    allowed_years = {now.year, now.year + 1}
    events = []
    pattern = r"\b(\d{2})/(\d{2})/(20\d{2})\s+([^\n]+)"
    for day_text, month_text, year_text, label in re.findall(pattern, text):
        if int(year_text) not in allowed_years:
            continue
        if not re.search(r"monetary policy meeting.*day\s*2", label, re.I):
            continue
        scheduled = date(int(year_text), int(month_text), int(day_text))
        reference_period = scheduled.strftime("%Y-%m")
        events.append(_event(source, now, body_hash, "policy_rate",
                             "ECB monetary policy decision", scheduled.isoformat(),
                             timezone="Europe/Luxembourg", status="confirmed",
                             reference_period=reference_period, precision="date"))
    return events, [], [], None if events else f"no_{now.year}_or_{now.year + 1}_policy_days_found"


def _parse_ecb_policy(source: dict[str, Any], now: datetime, body_hash: str,
                      text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    key_rates = re.search(r"Key ECB interest rates([\s\S]{0,500}?)(?=Deposit facility)", text, re.I)
    date_match = (re.search(
        r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\b",
        key_rates.group(1), re.I) if key_rates else None)
    ref = None
    if date_match:
        ref = f"{int(date_match.group(3)):04d}-{_MONTHS[date_match.group(2).lower()]:02d}"
    rate = re.search(r"Key ECB interest rates[\s\S]{0,1200}?Deposit facility\s+([\d.]+)\s*%", text, re.I)
    releases = []
    if rate:
        releases.append(_release(source, now, body_hash, "policy_rate", _numeric(rate), "percent",
                                 ref, vintage="current_snapshot",
                                 seasonal_adjustment="not_applicable"))
    return [], releases, [] if releases else ["deposit_facility_rate_not_found;actual_time_unknown"], None


def _parse_eurostat_release(source: dict[str, Any], now: datetime, body_hash: str,
                            text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    ref = _month_year(text)
    releases = []
    if source["parser"] == "eurostat_hicp_release":
        headline = re.search(r"annual inflation is expected to be\s+([\d.]+)\s*% in", text, re.I)
        if headline is None:
            headline = re.search(r"All-items HICP\s+1000\.0\s+(?:[-\d.]+e?\s+){6}([\d.]+)(e?)", text, re.I)
        all_items = re.search(
            r"All-items HICP\s+1000\.0\s+(?:[-\d.]+e?\s+){6}([\d.]+)(e?)\s+([\d.]+)(e?)",
            text, re.I)
        core = re.search(
            r"energy, food, alcohol\s*&\s*tobacco\s+[\d.]+\s+(?:[-\d.]+e?\s+){6}([\d.]+)(e?)\s+([\d.]+)(e?)",
            text, re.I)
        if headline:
            releases.append(_release(source, now, body_hash, "cpi_headline_yoy", _numeric(headline),
                                     "percent", ref, vintage="first_release", estimate=True,
                                     seasonal_adjustment="NSA"))
        if all_items:
            releases.append(_release(source, now, body_hash, "cpi_headline_mom",
                                     float(all_items.group(3)), "percent", ref,
                                     vintage="first_release", estimate=all_items.group(4) == "e",
                                     seasonal_adjustment="NSA"))
        if core:
            releases.append(_release(source, now, body_hash, "cpi_core_yoy",
                                     float(core.group(1)), "percent", ref,
                                     vintage="first_release", estimate=core.group(2) == "e",
                                     seasonal_adjustment="NSA"))
            releases.append(_release(source, now, body_hash, "cpi_core_mom",
                                     float(core.group(3)), "percent", ref,
                                     vintage="first_release", estimate=core.group(4) == "e",
                                     seasonal_adjustment="NSA"))
    else:
        rate = re.search(r"euro area seasonally adjusted unemployment rate was\s+([\d.]+)%", text, re.I)
        if rate:
            releases.append(_release(source, now, body_hash, "unemployment_rate", _numeric(rate),
                                     "percent", ref, vintage="first_release", seasonal_adjustment="SA"))
    return [], releases, [] if releases else ["expected_eurostat_measure_not_found"], None


def _event_from_schedule(source: dict[str, Any], now: datetime, body_hash: str,
                         event_type: str, title: str, year: int, month_name: str,
                         day: int, timezone: str, hour: int | None,
                         minute: int | None = None, status: str = "tentative",
                         reference_period: str | None = None) -> dict[str, Any]:
    scheduled_date = date(year, _MONTHS[month_name.lower()], day).isoformat()
    scheduled_time = None if hour is None else f"{hour:02d}:{(minute or 0):02d}"
    return _event(source, now, body_hash, event_type, title, scheduled_date,
                  timezone=timezone, scheduled_time=scheduled_time, status=status,
                  reference_period=reference_period)


def _parse_bls_schedule(source: dict[str, Any], now: datetime, body_hash: str,
                         text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    events = []
    pattern = rf"(?:{'|'.join(_WEEKDAYS)})[,]?\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{{1,2}}),\s*(20\d{{2}})\s+(\d{{1,2}}):(\d{{2}})\s*(AM|PM)\s+(.*?)(?=\s*(?:{'|'.join(_WEEKDAYS)})[,]?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{{1,2}},\s*20\d{{2}}|NOTE:|Last Modified|$)"
    for match in re.finditer(pattern, text, re.I | re.S):
        month_name, day_text, year_text, hour_text, minute_text, meridiem, title = match.groups()
        title = title.strip()
        lowered = title.lower()
        if "employment situation" in lowered:
            kind = "employment"
        elif "consumer price index" in lowered:
            kind = "inflation"
        else:
            continue
        hour = int(hour_text) % 12 + (12 if meridiem.upper() == "PM" else 0)
        ref = _month_year(title)
        events.append(_event_from_schedule(source, now, body_hash, kind, title,
                     int(year_text), month_name, int(day_text), "America/New_York",
                     hour, int(minute_text), status="confirmed", reference_period=ref))
    return events, [], [] if events else [], None if events else "no_supported_2027_release_rows_found"


def _parse_fed_schedule(source: dict[str, Any], now: datetime, body_hash: str,
                        text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    events = []
    pattern = r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s*[-–]\s*(\d{1,2})(?:,\s*(20\d{2}))?\b"
    allowed_years = {now.year, now.year + 1}
    headings = list(re.finditer(r"(?m)^\s*(20\d{2})\s+FOMC Meetings\s*$", text))
    section_meetings: dict[int, list[tuple[str, int]]] = {}
    for heading_index, heading in enumerate(headings):
        year = int(heading.group(1))
        if year not in allowed_years:
            continue
        end = headings[heading_index + 1].start() if heading_index + 1 < len(headings) else len(text)
        section = text[heading.end():end]
        for match in re.finditer(pattern, section, re.I):
            month_name, _start_day, end_day, explicit_year = match.groups()
            if explicit_year is not None and int(explicit_year) != year:
                continue
            intervening_years = re.findall(r"(?m)^\s*(20\d{2})\s*$", section[:match.start()])
            if intervening_years and int(intervening_years[-1]) != year:
                continue
            section_meetings.setdefault(year, []).append((month_name, int(end_day)))
    for year, meetings in section_meetings.items():
        for ordinal, (month_name, end_day) in enumerate(meetings, 1):
            # These are meeting dates. The official calendar does not establish the statement clock time.
            reference = f"{year}-FOMC-{ordinal:02d}"
            events.append(_event_from_schedule(source, now, body_hash, "policy_rate",
                         "FOMC policy decision", year, month_name, end_day,
                         "America/New_York", None,
                         status="tentative" if year > now.year else "confirmed",
                         reference_period=reference))
    return events, [], [], None if events else f"fomc_dates_not_found_for_{now.year}_or_{now.year + 1}"


def _parse_bea_schedule(source: dict[str, Any], now: datetime, body_hash: str,
                        text: str) -> tuple[list[dict], list[dict], list[str], str | None]:
    events = []
    pattern = r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s+(\d{1,2}):(\d{2})\s*(AM|PM)[\s\S]{0,180}?Personal Income and Outlays,\s*(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})"
    for match in re.finditer(pattern, text, re.I):
        month_name, day, hour, minute, meridiem, ref_month, ref_year = match.groups()
        hour_value = int(hour) % 12 + (12 if meridiem.upper() == "PM" else 0)
        reference_year = int(ref_year)
        reference_month = _MONTHS[ref_month.lower()]
        release_month = _MONTHS[month_name.lower()]
        reference_period = f"{reference_year:04d}-{reference_month:02d}"
        release_year = reference_year
        if release_month < reference_month:
            release_year += 1
        if release_year not in {now.year, now.year + 1}:
            continue
        events.append(_event_from_schedule(source, now, body_hash, "pce",
                     "Personal Income and Outlays (PCE)", release_year, month_name, int(day),
                     "America/New_York", hour_value, int(minute), status="confirmed",
                     reference_period=reference_period))
    return events, [], [], None if events else f"pce_release_dates_not_found_for_{now.year}_or_{now.year + 1}"


_PARSERS = {
    "bls_cpi": _parse_bls_cpi,
    "bls_employment": _parse_bls_employment,
    "bls_series": _parse_bls_series,
    "eurostat_api": _parse_eurostat_api,
    "bea_pce": _parse_bea_pce,
    "fed_policy": _parse_fed_policy,
    "bok_rate": _parse_bok_rate,
    "mods_cpi": _parse_mods_cpi,
    "mods_employment": _parse_mods_employment,
    "jp_cpi": _parse_jp_cpi,
    "jp_employment": _parse_jp_employment,
    "ecb_policy": _parse_ecb_policy,
    "jp_cpi_schedule": _parse_jp_schedule,
    "jp_lfs_schedule": _parse_jp_schedule,
    "boj_schedule": _parse_boj_schedule,
    "ecb_schedule": _parse_ecb_schedule,
    "eurostat_hicp_release": _parse_eurostat_release,
    "eurostat_unemployment_release": _parse_eurostat_release,
    "bls_schedule": _parse_bls_schedule,
    "fed_schedule": _parse_fed_schedule,
    "bea_schedule": _parse_bea_schedule,
}


def _registry(registry: Any, now: datetime) -> list[dict[str, Any]]:
    if registry is None:
        from pathlib import Path

        import yaml

        path = Path(__file__).resolve().parents[3] / "configs" / "economic_calendar.yaml"
        with path.open(encoding="utf-8") as stream:
            registry = yaml.safe_load(stream)
    required_measures = {}
    if isinstance(registry, dict):
        required_measures = registry.get("required_measures", {})
        sources = registry.get("sources", [])
    else:
        sources = registry
    expanded = []
    for item in sources:
        source = dict(item)
        source["_required_measures"] = required_measures
        year_spec = source.get("calendar_year")
        if year_spec == "current":
            source["calendar_year"] = now.year
        elif year_spec == "next":
            source["calendar_year"] = now.year + 1
        url_year = source.get("url_year")
        if url_year == "current":
            url_year = now.year
        elif url_year == "next":
            url_year = now.year + 1
        if url_year is not None:
            source["source_url"] = source["source_url"].format(
                year=url_year, start_year=now.year - 2, end_year=now.year)
        expanded.append(source)
    return expanded


def _parse(source: dict[str, Any], now: datetime, body_hash: str,
           body: bytes) -> tuple[list[dict], list[dict], list[str], str | None]:
    parser_name = source.get("parser", "none")
    parser = _PARSERS.get(parser_name)
    if parser is None:
        reason = source.get("unavailable_reason", "manual_import_required") if parser_name == "manual_import" else "parser_not_implemented"
        return [], [], [], reason
    if parser_name in {"bls_series", "eurostat_api", "jp_cpi_schedule", "jp_lfs_schedule", "boj_schedule", "fed_policy"}:
        try:
            return parser(source, now, body_hash, body)
        except (ValueError, TypeError, KeyError, OverflowError, json.JSONDecodeError) as error:
            return [], [], [f"parse_error:{type(error).__name__}"], "parse_error"
    text = _text(body)
    try:
        return parser(source, now, body_hash, text)
    except (ValueError, TypeError, KeyError, OverflowError, IndexError) as error:
        return [], [], [f"parse_error:{type(error).__name__}"], "parse_error"


def collect_economic_sources(*, now: datetime, budget_seconds: float = 120,
                             timeout_seconds: float = 10, retries: int = 1,
                             fetch: Any = None, registry: Any = None,
                             clock: Any = None) -> dict:
    """Fetch registered official sources within a hard wall-clock budget.

    An injected ``fetch(url, timeout_seconds)`` may return bytes or an object
    with ``status``, ``headers`` and ``body`` attributes. ``now`` is the trusted
    first-seen clock, generally captured by the caller in UTC.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if budget_seconds < 0 or timeout_seconds <= 0 or retries < 0:
        raise ValueError("budget, timeout and retry limits must be nonnegative")
    start = monotonic_time.monotonic()
    deadline = start + budget_seconds
    clock_fn = clock or (lambda: datetime.now(UTC))
    start_iso = now.astimezone(UTC).isoformat()
    fetch_fn = fetch or _default_fetch
    events: list[dict] = []
    releases: list[dict] = []
    receipts: list[dict] = []
    budget_exhausted = False

    for spec in _registry(registry, now):
        base = {
            "source_id": spec.get("source_id"),
            "country": spec.get("country"),
            "source_url": spec.get("source_url"),
            "expected_measures": list(spec.get("expected_measures", [])),
            "access": spec.get("access", "public_no_key"),
            "format": spec.get("format", "unknown"),
            "license_note": spec.get("license_note", "Official public source; follow its attribution and reuse terms."),
            "first_seen_at": None,
            "checked_at": None,
            "source_hash": None,
            "http_status": None,
            "content_type": None,
            "response_bytes": 0,
            "elapsed_seconds": 0.0,
            "attempt_count": 0,
            "request_count": 0,
            "events_count": 0,
            "releases_count": 0,
            "unavailable_measures": [],
            "status": "unavailable",
            "reason": None,
        }
        if spec.get("access") == "api_key_required":
            checked_at = clock_fn().astimezone(UTC).isoformat()
            receipts.append({**base, "checked_at": checked_at, "reason": "api_key_required",
                             "key_env": spec.get("key_env"),
                             "structured_import": spec.get("structured_import")})
            continue
        if monotonic_time.monotonic() >= deadline:
            budget_exhausted = True
            receipts.append({**base, "reason": "budget_exhausted"})
            continue
        request_started = monotonic_time.monotonic()
        response = None
        failure_reason = None
        resolved_url = spec["source_url"]
        discovery_hash = None
        for attempt in range(retries + 1):
            remaining = deadline - monotonic_time.monotonic()
            if remaining <= 0:
                budget_exhausted = True
                failure_reason = "budget_exhausted"
                break
            base["attempt_count"] = attempt + 1
            try:
                request_timeout = min(timeout_seconds, remaining)
                response = (_wall_bounded_fetch(spec["source_url"], request_timeout)
                            if fetch is None else fetch_fn(spec["source_url"], request_timeout))
                base["request_count"] += 1
                if isinstance(response, (bytes, bytearray)):
                    response = _FetchResponse(200, {"Content-Type": spec.get("content_type", "text/plain")}, bytes(response))
                elif not hasattr(response, "body"):
                    raise TypeError("fetch must return bytes or a response with body")
                if int(response.status) == 200 and spec.get("follow_link_text"):
                    listing_body = bytes(response.body)
                    listing_hash = hashlib.sha256(listing_body).hexdigest()
                    resolved_url = _follow_url(listing_body, spec["source_url"], spec["follow_link_text"])
                    if resolved_url is None:
                        failure_reason = "latest_release_link_not_found"
                        discovery_hash = listing_hash
                        break
                    remaining = deadline - monotonic_time.monotonic()
                    if remaining <= 0:
                        budget_exhausted = True
                        failure_reason = "budget_exhausted"
                        break
                    follow_timeout = min(timeout_seconds, remaining)
                    response = None
                    response = (_wall_bounded_fetch(resolved_url, follow_timeout)
                                if fetch is None else fetch_fn(resolved_url, follow_timeout))
                    base["request_count"] += 1
                    if isinstance(response, (bytes, bytearray)):
                        response = _FetchResponse(200, {"Content-Type": spec.get("content_type", "text/plain")}, bytes(response))
                    elif not hasattr(response, "body"):
                        raise TypeError("follow fetch must return bytes or a response with body")
                    discovery_hash = listing_hash
                break
            except (TimeoutError, URLError, OSError) as error:
                failure_reason = "timeout" if isinstance(error, TimeoutError) else "network_error"
                if attempt >= retries:
                    break
            except Exception as error:  # injected source failures should not break daily publication
                failure_reason = f"fetch_error:{type(error).__name__}"
                break
        elapsed = monotonic_time.monotonic() - request_started
        base["elapsed_seconds"] = round(elapsed, 6)
        checked_at = clock_fn().astimezone(UTC).isoformat()
        base["checked_at"] = checked_at
        if response is None:
            receipts.append({**base, "reason": failure_reason or "fetch_failed"})
            continue

        body = bytes(response.body)
        if len(body) > _MAX_RESPONSE_BYTES:
            receipts.append({**base, "reason": "response_too_large",
                             "response_bytes": len(body)})
            continue
        received_at = checked_at
        base["first_seen_at"] = received_at
        body_hash = hashlib.sha256(body).hexdigest()
        base.update(source_hash=body_hash, http_status=int(response.status),
                    content_type=_header(response.headers, "Content-Type"),
                    response_bytes=len(body), resolved_source_url=resolved_url,
                    discovery_source_hash=discovery_hash)
        if int(response.status) != 200:
            receipts.append({**base, "reason": failure_reason or f"http_status_{response.status}"})
            continue
        if failure_reason == "latest_release_link_not_found":
            receipts.append({**base, "reason": failure_reason})
            continue

        parse_source = {**spec, "source_url": resolved_url}
        parsed_events, parsed_releases, unavailable, parse_reason = _parse(
            parse_source, datetime.fromisoformat(received_at), body_hash, body)
        stale_reference = None
        reference_lag = None
        max_lag = spec.get("max_reference_lag_months")
        if max_lag is not None and parsed_releases:
            reference = parsed_releases[-1].get("reference_period") or ""
            match = re.fullmatch(r"(20\d{2})-(\d{2})", reference)
            if match:
                reference_lag = (now.year - int(match.group(1))) * 12 + now.month - int(match.group(2))
                if reference_lag > int(max_lag):
                    stale_reference = reference
                    unavailable = [f"{row['measure']}:stale_reference_period:{reference}"
                                   for row in parsed_releases]
                    parse_reason = f"stale_reference_period:{reference}"
                    parsed_releases = []
            else:
                stale_reference = reference or "unknown"
                unavailable = [f"{row['measure']}:invalid_reference_period"
                               for row in parsed_releases]
                parse_reason = "invalid_reference_period"
                parsed_releases = []
        events.extend(parsed_events)
        releases.extend(parsed_releases)
        reason = parse_reason
        if parsed_events or parsed_releases:
            status = "partial" if unavailable or parse_reason else "ok"
        else:
            status = "unavailable"
            reason = reason or (unavailable[0] if unavailable else "no_expected_events_or_releases_parsed")
        receipts.append({**base, "status": status, "reason": reason,
                         "events_count": len(parsed_events), "releases_count": len(parsed_releases),
                         "latest_reference_period": (parsed_releases[-1].get("reference_period")
                                                      if parsed_releases else stale_reference),
                         "reference_lag_months": reference_lag,
                         "unavailable_measures": unavailable})

    finished_at = clock_fn().astimezone(UTC).isoformat()
    required = next((spec.get("_required_measures", {}) for spec in _registry(registry, now)), {})
    coverage = []
    for country, measure_names in required.items():
        for measure in measure_names:
            candidates = [row for row in receipts
                          if row.get("country") == country and measure in row.get("expected_measures", [])]
            matching = [row for row in releases
                        if row.get("country") == country and row.get("measure") == measure]
            if matching:
                latest = max(matching, key=lambda row: (row.get("reference_period") or "",
                                                        row.get("first_seen_at") or ""))
                status = ("observed_current_receipt_v1"
                          if latest.get("numeric_eligibility") == "observed_current_receipt_v1"
                          else "not_ready_unknown_definition")
                coverage.append({
                    "country": country, "measure": measure, "status": status,
                    "source_ids": sorted({row["source_id"] for row in matching}),
                    "reference_period": latest.get("reference_period"),
                    "unit": latest.get("unit"),
                    "seasonal_adjustment": latest.get("seasonal_adjustment"),
                    "actual_release_at": latest.get("actual_release_at"),
                    "first_seen_at": latest.get("first_seen_at"),
                    "vintage": latest.get("vintage"),
                    "definition_version": latest.get("definition_version"),
                    "numeric_eligibility": latest.get("numeric_eligibility"),
                    "original_publication_verified": latest.get("original_publication_verified", False),
                    "verified_first_release_ready": bool(
                        latest.get("vintage") == "first_release"
                        and latest.get("original_publication_verified")
                    ),
                })
            else:
                reasons = []
                for receipt in candidates:
                    reasons.extend(receipt.get("unavailable_measures", []))
                    if receipt.get("reason"):
                        reasons.append(receipt["reason"])
                coverage.append({
                    "country": country, "measure": measure,
                    "status": "unavailable" if candidates else "not_configured",
                    "source_ids": sorted({row["source_id"] for row in candidates}),
                    "source_statuses": sorted({row["status"] for row in candidates}),
                    "reason": ";".join(dict.fromkeys(reasons)) or "no_source_receipt_for_measure",
                    "verified_first_release_ready": False,
                })
    return {
        "started_at": start_iso,
        "finished_at": finished_at,
        "budget_seconds": float(budget_seconds),
        "budget_exhausted": budget_exhausted or monotonic_time.monotonic() > deadline,
        "events": events,
        "releases": releases,
        "sources": receipts,
        "coverage": coverage,
    }
