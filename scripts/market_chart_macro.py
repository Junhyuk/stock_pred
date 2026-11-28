"""Official Japan, euro-area and US economic history for the public charts."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from functools import partial
from urllib.parse import urlencode, urljoin
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

from roboquant.index_forecast.economic_sources import _Links, _month_year, _Tables, _text

Points = dict[str, float]
ReleaseDates = dict[str, tuple[str, str]]
Fetch = Callable[[str], bytes]
TOKYO = ZoneInfo("Asia/Tokyo")
LUXEMBOURG = ZoneInfo("Europe/Luxembourg")
NY = ZoneInfo("America/New_York")
ESTAT = "https://www.e-stat.go.jp"
JP_CPI = ESTAT + "/en/stat-search/file-download?fileKind=1&statInfId=000040482955"
JP_LFS = ESTAT + "/en/stat-search/file-download?fileKind=0&statInfId=000031831358"
JP_CPI_LATEST = "https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html"
JP_LFS_LATEST = "https://www.stat.go.jp/data/roudou/sokuhou/tsuki/index.html"
BOJ = "https://www.boj.or.jp"
BOJ_POLICY = BOJ + "/en/mopo/mpmdeci/"
BOJ_NEGATIVE = BOJ_POLICY + "mpr_2016/k160129a.pdf"
BOJ_FRAMEWORK = BOJ_POLICY + "state_2024/k240319a.htm"
BOJ_BOUNDARY = date(2024, 3, 21)
BIS_JP = "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.JP"
ECB = "https://www.ecb.europa.eu"
ECB_RATE = ECB + "/stats/policy_and_exchange_rates/key_ecb_interest_rates/html/index.en.html"
EUROSTAT = "https://ec.europa.eu/eurostat"
EUROSTAT_SERIES = {
    "ea_inflation": ("prc_hicp_minr", {"freq": "M", "coicop18": "TOTAL", "unit": "RCH_A", "geo": "EA21"}),
    "ea_unemployment": ("une_rt_m", {"freq": "M", "s_adj": "SA", "age": "TOTAL", "unit": "PC_ACT", "sex": "T", "geo": "EA21"}),
}
FRED = "https://fred.stlouisfed.org"
BEA = "https://www.bea.gov"
BEA_ARCHIVE = BEA + "/news/archive?field_related_product_target_id=476"
BEA_CURRENT = BEA + "/news/current-releases"
PCE_CODES = {"us_pce": "PCEPI", "us_core_pce": "PCEPILFE"}
MONTH_NOTE = "가로축은 통계 기준월입니다. 값은 최신 공식 시계열이며 발표 당시 수치와 다를 수 있습니다."
TARGET_NOTE = "2024년 3월 21일부터 적용된 무담보 익일물 콜금리 목표입니다."
EA_NOTE = "현재 유로지역 21개국 구성 기준입니다. 과거 발표와 포함 국가가 다를 수 있습니다."
FETCH_ERRORS = (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, ET.ParseError, BadZipFile)


def _monthly(label: str, unit: str, source: str, source_url: str, note: str = "") -> dict[str, str]:
    return dict(label=label, unit=unit, source=source, source_url=source_url,
                date_basis="reference_month", note=" ".join(filter(None, (MONTH_NOTE, note))))


def _policy(label: str, source: str, source_url: str, note: str) -> dict[str, str]:
    return dict(label=label, unit="percent", source=source, source_url=source_url,
                date_basis="effective_date", note=note)


MACRO_SPECS = {
    "jp_policy_rate": _policy(
        "일본은행 무담보 익일물 콜금리 목표", "일본은행 · BIS", BOJ_POLICY,
        TARGET_NOTE + " 범위로 발표된 기간에는 상한과 하한의 중간값을 표시합니다.",
    ),
    "jp_policy_rate_lower": _policy("일본은행 콜금리 목표 하한", "일본은행 · BIS", BOJ_POLICY, TARGET_NOTE),
    "jp_policy_rate_upper": _policy("일본은행 콜금리 목표 상한", "일본은행 · BIS", BOJ_POLICY, TARGET_NOTE),
    "jp_negative_policy_balances": _policy(
        "일본은행 정책금리 잔액 적용 금리", "일본은행", BOJ_NEGATIVE,
        "일부 당좌예금 잔액에 적용된 마이너스 금리입니다. 이 제도는 2024년 3월 21일 종료되었습니다.",
    ),
    "jp_inflation": _monthly(
        "일본 소비자물가 전년동월비", "percent", "일본 총무성 통계국 · e-Stat",
        ESTAT + "/en/stat-search/files?stat_infid=000040482955", "2025년 기준 전국 종합지수입니다.",
    ),
    "jp_unemployment": _monthly(
        "일본 실업률(계절조정)", "percent", "일본 총무성 통계국 · e-Stat",
        ESTAT + "/en/stat-search/files?stat_infid=000031831358",
    ),
    "jp_employment": _monthly(
        "일본 취업자 전년동월 증감(원계열)", "thousand_persons", "일본 총무성 통계국 · e-Stat",
        ESTAT + "/en/stat-search/files?stat_infid=000031831358",
        "전국 취업자 원계열에서 전년 같은 달을 뺀 값으로, 발표 단위인 만 명을 천 명으로 환산했습니다.",
    ),
    "ea_policy_rate": _policy(
        "유럽중앙은행 예금금리", "유럽중앙은행", ECB_RATE, "ECB의 예금금리 변경 적용일입니다.",
    ),
    "ea_inflation": _monthly(
        "유로지역 소비자물가(HICP) 전년동월비", "percent", "Eurostat",
        EUROSTAT + "/databrowser/view/prc_hicp_minr/default/table?lang=en", EA_NOTE,
    ),
    "ea_unemployment": _monthly(
        "유로지역 실업률(계절조정)", "percent", "Eurostat",
        EUROSTAT + "/databrowser/view/une_rt_m/default/table?lang=en", EA_NOTE,
    ),
    **{
        key: _monthly(label, "percent", "미국 경제분석국 · FRED", FRED + f"/series/{PCE_CODES[key]}",
                      "계절조정 물가지수의 전년동월비입니다.")
        for key, label in (("us_pce", "미국 PCE 물가지수 전년동월비"),
                           ("us_core_pce", "미국 근원 PCE 물가지수 전년동월비(식품·에너지 제외)"))
    },
}


def _value(raw) -> float | None:
    if type(raw) not in (str, int, float):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _day(raw: str) -> date:
    day = date.fromisoformat(raw)
    if day.isoformat() != raw:
        raise ValueError("expected canonical date")
    return day


def _bounded(points: Points, first: date, end: date) -> Points:
    return {day: value for day, value in points.items() if first.isoformat() <= day < end.isoformat()}


def _yoy(points: Points, *, as_percent: bool) -> Points:
    result = {}
    for key, value in points.items():
        day = _day(key)
        prior = points.get(day.replace(year=day.year - 1).isoformat())
        if prior is not None and (prior > 0 or not as_percent):
            result[key] = round((value / prior - 1) * 100, 1) if as_percent else value - prior
    return result


def japan_cpi(raw: bytes, first: date, end: date) -> Points:
    rows = csv.reader(io.StringIO(raw.decode("cp932")))
    next(rows, ())
    header, codes = next(rows, ()), next(rows, ())
    column = header.index("All items")
    if len(codes) <= column or codes[column] != "0001":
        raise ValueError("unexpected Japan CPI series")
    points = {}
    for row in rows:
        if len(row) > column and re.fullmatch(r"\d{6}", row[0]) and (value := _value(row[column])) is not None:
            points[f"{row[0][:4]}-{row[0][4:]}-01"] = value
    return _bounded(points, first, end)


def _workbook_xml(book: ZipFile, path: str) -> ET.Element:
    entry = book.getinfo(path)
    if entry.file_size > 5_000_000:
        raise ValueError("oversized Japan labor survey workbook part")
    return ET.fromstring(book.read(entry))


def _japan_labor_sheet(book: ZipFile, filename: str, strings: list[str],
                       adjustment: str, column: str, measure: str) -> Points:
    points = {}
    year = None
    expected_markers = {"E5": adjustment, f"{column}7": measure, f"{column}9": "Both sexes"}
    markers = {}
    for row in _workbook_xml(book, filename).findall(".//{*}row"):
        cells = {}
        for cell in row.findall("{*}c"):
            location = cell.attrib["r"]
            key = re.sub(r"\d+$", "", location)
            raw = cell.findtext("{*}v")
            if raw is None or key not in {"A", "B", "E", column}:
                continue
            value = strings[int(raw)] if cell.get("t") == "s" else raw
            if location in expected_markers:
                markers[location] = value
            cells[key] = value
        japanese_year = re.match(r"(令和|平成|昭和)\s*(\d+|元)年", cells.get("A", ""))
        if japanese_year:
            era, count = japanese_year.groups()
            year = {"令和": 2018, "平成": 1988, "昭和": 1925}[era] + (1 if count == "元" else int(count))
        elif re.fullmatch(r"(?:19|20)\d{2}", cells.get("A", "")):
            year = int(cells["A"])
        month = re.match(r"(\d{1,2})月", cells.get("B", ""))
        value = _value(cells.get(column))
        if year and month and value is not None:
            points[date(year, int(month[1]), 1).isoformat()] = value
    if any(text not in markers.get(location, "") for location, text in expected_markers.items()):
        raise ValueError("unexpected Japan labor survey workbook layout")
    return points


def japan_labor(raw: bytes, first: date, end: date) -> dict[str, Points]:
    with ZipFile(io.BytesIO(raw)) as book:
        strings = ["".join(node.text or "" for node in item.iterfind(".//{*}t"))
                   for item in _workbook_xml(book, "xl/sharedStrings.xml")]
        rate = _japan_labor_sheet(book, "xl/worksheets/sheet1.xml", strings,
                                 "Seasonally adjusted series", "T", "Unemployment rate")
        employed = _japan_labor_sheet(book, "xl/worksheets/sheet2.xml", strings,
                                     "Original series", "H", "Employed person")
    return {
        "jp_unemployment": _bounded({day: round(value, 1) for day, value in rate.items()}, first, end),
        "jp_employment": _bounded(_yoy({day: value * 10 for day, value in employed.items()},
                                       as_percent=False), first, end),
    }


def eurostat_points(raw: bytes, dimensions: dict[str, str], first: date, end: date) -> Points:
    payload = json.loads(raw)
    categories = payload["dimension"]
    ids = payload["id"]
    sizes = payload["size"]
    if (set(ids) != set(dimensions) | {"time"} or ids[-1] != "time"
            or len(sizes) != len(ids) or any(size != 1 for size in sizes[:-1])
            or any(categories[axis]["category"]["index"] != {code: 0} for axis, code in dimensions.items())):
        raise ValueError("unexpected Eurostat series dimensions")
    positions = categories["time"]["category"]["index"]
    observed = payload["value"]
    if isinstance(observed, list):
        observed = dict(enumerate(observed))
    else:
        observed = {int(position): value for position, value in observed.items()}
    points = {
        _day(f"{month}-01").isoformat(): value
        for month, position in positions.items()
        if (value := _value(observed.get(position))) is not None
    }
    return _bounded(points, first, end)


# The calendar feed begins in 2025. Earlier entries are the dated Eurostat releases.
_EUROSTAT_OLD = {
    "ea_inflation": {
        "2023-11": "2-19122023-ap", "2023-12": "2-17012024-ap",
        "2024-01": "2-22022024-ap", "2024-02": "2-18032024-ap",
        "2024-03": "2-17042024-ap", "2024-04": "2-17052024-ap",
        "2024-05": "2-18062024-ap", "2024-06": "2-17072024-ap",
        "2024-07": "2-20082024-ap", "2024-08": "2-18092024-ap",
        "2024-09": "2-17102024-ap", "2024-10": "2-19112024-ap",
        "2024-11": "2-18122024-ap",
    },
    "ea_unemployment": {
        "2023-11": "3-09012024-ap", "2023-12": "3-01022024-bp",
        "2024-01": "3-01032024-bp", "2024-02": "3-03042024-bp",
        "2024-03": "3-03052024-ap", "2024-04": "3-30052024-ap",
        "2024-05": "3-02072024-bp", "2024-06": "3-01082024-ap",
        "2024-07": "3-30082024-bp", "2024-08": "3-02102024-ap",
        "2024-09": "3-31102024-bp", "2024-10": "3-02122024-ap",
    },
}


def _eurostat_press_url(slug: str) -> str:
    return f"{EUROSTAT}/web/products-euro-indicators/w/{slug}"


def _old_eurostat_releases(cutoff: datetime) -> dict[str, ReleaseDates]:
    return {
        key: {
            ref + "-01": (published.date().isoformat(), _eurostat_press_url(slug))
            for ref, slug in events.items()
            if (published := datetime.strptime(slug.split("-")[1], "%d%m%Y").replace(
                hour=11, tzinfo=LUXEMBOURG,
            )) <= cutoff
        }
        for key, events in _EUROSTAT_OLD.items()
    }


def eurostat_releases(raw: bytes, cutoff: datetime) -> dict[str, ReleaseDates]:
    titles = {"Inflation (HICP)": "ea_inflation", "Unemployment": "ea_unemployment"}
    details = {key: {} for key in titles.values()}
    for row in json.loads(raw):
        if not isinstance(row, dict) or row.get("euroindAuthor") != "estat" or row.get("title") not in titles:
            continue
        ref = _month_year(str(row.get("period", "")))
        try:
            published = datetime.fromisoformat(row["start"])
            day = published.date()
        except (KeyError, TypeError, ValueError):
            continue
        if ref and published.utcoffset() is not None and published <= cutoff and ref < day.isoformat()[:7]:
            key = titles[row["title"]]
            if key == "ea_inflation":
                url = _eurostat_press_url(f"2-{day:%d%m%Y}-ap")
            else:
                # Unemployment press URLs use varying product suffixes. The
                # official calendar links to each affected publication month.
                query = urlencode({"type": "listMonth", "start": int(published.timestamp() * 1000),
                                   "indicator": "Unemployment"})
                url = f"{EUROSTAT}/news/euro-indicators/release-calendar?{query}"
            release = (day.isoformat(), url)
            day_ref = ref + "-01"
            details[key][day_ref] = min(release, details[key].get(day_ref, release))
    return details


def _eurostat_series(key: str, first: date, end: date, fetch: Fetch) -> dict[str, Points]:
    table, dimensions = EUROSTAT_SERIES[key]
    query = urlencode({"lang": "en", **dimensions, "sinceTimePeriod": first.strftime("%Y-%m")})
    url = f"{EUROSTAT}/api/dissemination/statistics/1.0/data/{table}?{query}"
    return {key: eurostat_points(fetch(url), dimensions, first, end)}


def _eurostat_archive(first: date, cutoff: datetime, fetch: Fetch) -> dict[str, ReleaseDates]:
    local = cutoff.astimezone(LUXEMBOURG)
    query = urlencode({
        "start": datetime.combine(first, datetime.min.time(), LUXEMBOURG).isoformat(),
        "end": local.isoformat(timespec="seconds"), "timeZone": "Europe/Luxembourg",
        "theme": 0, "category": 0, "keywords": "", "isEuroindicator": "true",
        "authorInclude": "", "authorExclude": "",
    })
    return eurostat_releases(fetch(f"{EUROSTAT}/o/calendars/eventsJson?{query}"), cutoff)


def pce_points(raw: bytes, first: date, end: date) -> dict[str, Points]:
    rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if not rows.fieldnames or rows.fieldnames[0] != "observation_date" or not set(PCE_CODES.values()) <= set(rows.fieldnames):
        raise ValueError("unexpected FRED PCE series")
    points = {key: {} for key in PCE_CODES}
    for row in rows:
        day = _day(row["observation_date"])
        if day.day != 1:
            raise ValueError("unexpected PCE data frequency")
        for key, code in PCE_CODES.items():
            if (value := _value(row[code])) is not None and value > 0:
                points[key][day.isoformat()] = value
    return {key: _bounded(_yoy(values, as_percent=True), first, end) for key, values in points.items()}


def _bea_refs(label: str) -> list[str]:
    heading = re.fullmatch(r"Personal Income and Outlays, ([A-Za-z]+)(?: and ([A-Za-z]+))? (20\d{2})", label)
    if not heading:
        return []
    month, other, year = heading.groups()
    return [ref for name in filter(None, (month, other)) if (ref := _month_year(f"{name} {year}"))]


def bea_releases(raw: bytes, cutoff: datetime) -> ReleaseDates:
    releases = {}
    for row in re.findall(r"<tr\b[^>]*>.*?</tr>", raw.decode("utf-8"), flags=re.S):
        time_match = re.search(r'<time\b[^>]*\bdatetime="([^"]+)"', row)
        if not time_match:
            continue
        stamp = datetime.fromisoformat(time_match[1])
        if stamp.utcoffset() is None or stamp > cutoff:
            continue
        links = _Links()
        links.feed(row)
        for href, label in links.links:
            if not href.startswith("/news/"):
                continue
            for ref in _bea_refs(label):
                if ref < stamp.date().isoformat()[:7]:
                    day = ref + "-01"
                    release = (stamp.date().isoformat(), urljoin(BEA, href))
                    releases[day] = min(releases.get(day, release), release)
    return releases


def ecb_policy_points(raw: bytes, through: date) -> Points:
    tables = _Tables()
    tables.feed(raw.decode("utf-8"))
    points = {}
    for _, rows in tables.tables:
        if not rows or "Deposit facility" not in rows[0] or "Date (with effect from)" not in rows[0]:
            continue
        for row in rows:
            if len(row) != 6 or not re.fullmatch(r"(?:19|20)\d{2}", row[0]):
                continue
            match = re.match(r"(\d{1,2})\s+([A-Z][a-z]{2})", row[1])
            value = _value(row[2])
            if match and value is not None:
                day = datetime.strptime(f"{row[0]} {match[2]} {match[1]}", "%Y %b %d").date()
                if day <= through:
                    points[day.isoformat()] = value
    changes = {}
    previous = None
    for day, value in sorted(points.items()):
        if value != previous:
            changes[day] = value
            previous = value
    return changes


def _policy_details(links: dict[str, str], points: Points, max_gap: int = 7) -> ReleaseDates:
    releases = {}
    for day in points:
        possible = [decision for decision in links
                    if 0 <= (_day(day) - _day(decision)).days <= max_gap]
        if possible:
            decision = max(possible)
            releases[day] = (decision, links[decision])
    return releases


def _ecb_archive(year: int, fetch: Fetch) -> dict[str, str]:
    source = f"{ECB}/press/govcdec/mopo/{year}/html/index_include.en.html"
    links = _Links()
    links.feed(fetch(source).decode("utf-8"))
    result = {}
    for href, label in links.links:
        match = re.fullmatch(r"/press/pr/date/20\d{2}/html/ecb\.mp(\d{6})~[\da-f]+\.en\.html", href)
        if match and label == "Monetary policy decisions":
            decision = datetime.strptime(match[1], "%y%m%d").date().isoformat()
            result[decision] = urljoin(ECB, href)
    return result


# Verified BOJ effective dates/bounds also keep the chart available during a BIS outage.
BOJ_CHANGES = (
    ("2024-03-21", "2024-03-19", 0.0, 0.1, BOJ_FRAMEWORK),
    ("2024-08-01", "2024-07-31", 0.25, 0.25, BOJ_POLICY + "state_2024/k240731a.htm"),
    ("2025-01-27", "2025-01-24", 0.5, 0.5, BOJ_POLICY + "state_2025/k250124a.htm"),
    ("2025-12-22", "2025-12-19", 0.75, 0.75, BOJ_POLICY + "state_2025/k251219a.htm"),
    ("2026-06-17", "2026-06-16", 1.0, 1.0, BOJ_POLICY + "mpr_2026/k260616a.pdf"),
    ("2026-09-24", "2026-09-18", 1.25, 1.25, BOJ_POLICY + "mpr_2026/k260918a.pdf"),
)


def _boj_archive(year: int, fetch: Fetch) -> dict[str, str]:
    source = f"{BOJ_POLICY}state_{year}/index.htm"
    links = _Links()
    links.feed(fetch(source).decode("utf-8"))
    result = {}
    for href, label in links.links:
        match = re.search(r"/k(\d{6})a\.(?:pdf|htm)$", href)
        if match and label.startswith("Change in the Guideline for Money Market Operations"):
            result[datetime.strptime(match[1], "%y%m%d").date().isoformat()] = urljoin(source, href)
    return result


def _bis_rates(raw: bytes, through: date) -> tuple[Points, str | None]:
    rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    observed = {}
    for row in rows:
        day = _day(row["TIME_PERIOD"])
        if row["FREQ"] != "D" or row["REF_AREA"] != "JP":
            raise ValueError("unexpected BIS series identity")
        value = _value(row["OBS_VALUE"])
        if day <= through and value is not None:
            observed[day] = value
    changes = {}
    ordered = sorted(observed)
    for earlier, day in zip(ordered, ordered[1:], strict=False):
        if observed[day] != observed[earlier]:
            changes[day.isoformat()] = observed[day]
    return changes, ordered[-1].isoformat() if ordered else None


def boj_policy(first: date, through: date, fetch: Fetch) -> tuple[dict[str, Points], dict[str, ReleaseDates], dict[str, str]]:
    middle, lower, upper, details = {}, {}, {}, {}
    verified = {}
    for effective, decision, low, high, url in BOJ_CHANGES:
        if effective <= through.isoformat():
            lower[effective], upper[effective] = low, high
            middle[effective] = (low + high) / 2
            details[effective] = decision, url

    known_through = _day(BOJ_CHANGES[-1][0])
    if through > known_through:
        try:
            query = urlencode({"startPeriod": known_through.isoformat(), "format": "csv", "detail": "dataonly"})
            changes, last_observation = _bis_rates(fetch(f"{BIS_JP}?{query}"), through)
            newer = {day: value for day, value in changes.items() if day > known_through.isoformat()}
            if last_observation and last_observation >= known_through.isoformat():
                verified.update(dict.fromkeys(("jp_policy_rate", "jp_policy_rate_lower", "jp_policy_rate_upper"),
                                              last_observation))
            for day, value in newer.items():
                middle[day] = lower[day] = upper[day] = value
            links = {}
            for year in sorted({_day(day).year for day in newer} | {_day(day).year - 1 for day in newer if day[5:] <= "01-07"}):
                try:
                    links.update(_boj_archive(year, fetch))
                except FETCH_ERRORS:
                    continue
            details.update(_policy_details(links, newer))
        except FETCH_ERRORS:
            pass

    negative_start = date(2016, 2, 16)
    negative_end = min(through, BOJ_BOUNDARY - timedelta(days=1))
    previous = {}
    past_details = {}
    if max(first, negative_start) <= negative_end:
        previous = {negative_start.isoformat(): -0.1, negative_end.isoformat(): -0.1}
        past_details[negative_start.isoformat()] = ("2016-01-29", BOJ_NEGATIVE)
        verified["jp_negative_policy_balances"] = negative_end.isoformat()
        if negative_end == BOJ_BOUNDARY - timedelta(days=1):
            past_details[negative_end.isoformat()] = ("2024-03-19", BOJ_FRAMEWORK)
    return (
        {"jp_policy_rate": middle, "jp_policy_rate_lower": lower, "jp_policy_rate_upper": upper,
         "jp_negative_policy_balances": previous},
        {"jp_policy_rate": details, "jp_policy_rate_lower": details, "jp_policy_rate_upper": details,
         "jp_negative_policy_balances": past_details},
        verified,
    )


def _jp_summary_release(url: str, through: date, fetch: Fetch) -> ReleaseDates:
    content = _text(fetch(url))
    ref = _month_year(content)
    match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日公表", content)
    if not ref or not match:
        return {}
    published = date(*(int(part) for part in match.groups()))
    return {ref + "-01": (published.isoformat(), url)} if ref < published.isoformat()[:7] and published <= through else {}


def _estat_monthly_releases(raw: bytes, table_label: str, through: date) -> ReleaseDates:
    releases: ReleaseDates = {}
    for entry in re.findall(r"<article\b[^>]*>.*?</article>", raw.decode("utf-8"), re.S):
        ref = re.search(r"Survey date(?:\s|&nbsp;)*</span>\s*(20\d{2})([A-Za-z]{3,9})\.?\s*<", entry)
        stamp = re.search(r"Update date(?:\s|&nbsp;)*</span>\s*(20\d{2}-\d{2}-\d{2})", entry)
        if not ref or not stamp:
            continue
        month = datetime.strptime(ref[2][:3], "%b").month
        day = date(int(ref[1]), month, 1)
        published = _day(stamp[1])
        if not day < published <= through:
            continue
        links = _Links()
        links.feed(entry)
        for href, label in links.links:
            infid = re.search(r"[?&]stat_infid=(\d+)(?:&|$)", href)
            if infid and href.startswith("/en/stat-search/files?") and table_label in label:
                release = published.isoformat(), f"{ESTAT}/en/stat-search/files?stat_infid={infid[1]}"
                key = day.isoformat()
                releases[key] = min(release, releases.get(key, release))
    return releases


_CPI_QUERY = "Seasonally Adjusted Consumer Price Index for Japan"
_JP_CPI_ARCHIVES = (
    ({"tstat": "000001150147", "tclass1": "000001150149", "cycle": 1}, _CPI_QUERY),
    ({"tstat": "000001243876"}, "16-1 " + _CPI_QUERY),
)
_JP_LFS_ARCHIVES = (
    ({"tclass1": "000001226833", "tclass2": "000001226834"}, "1 Population aged 15 years old"),
    ({"tclass1": "000001226584", "tclass2": "000001226585"}, "I-1 Population aged 15 years old"),
)


def _estat_archive(kind: str, params: dict, label: str, through: date, fetch: Fetch, *, page: int = 1) -> ReleaseDates:
    cpi = kind == "cpi"
    query = urlencode({
        "layout": "dataset", "page": page, "toukei": "00200573" if cpi else "00200531",
        **({} if cpi else {"tstat": "000001226583", "cycle": 1}),
        **params,
        "query": _CPI_QUERY if cpi else "Population aged 15 years old and over by labour force status",
    })
    return _estat_monthly_releases(fetch(f"{ESTAT}/en/stat-search/files?{query}"), label, through)


def _job_results(jobs: dict[str, Callable]) -> dict:
    results = {}
    with ThreadPoolExecutor(max_workers=8) as workers:
        tasks = {key: workers.submit(job) for key, job in jobs.items()}
        for key, result in tasks.items():
            try:
                results[key] = result.result()
            except FETCH_ERRORS:
                continue
    return results


def macro_history(cutoff: datetime, first: date, cached: dict[str, Points],
                  fetch: Fetch) -> tuple[dict[str, Points], dict[str, ReleaseDates], dict[str, str]]:
    """Fetch raw observations and independently verified release/decision dates."""
    japan = cutoff.astimezone(TOKYO).date()
    europe = cutoff.astimezone(LUXEMBOURG).date()
    us = cutoff.astimezone(NY).date()
    start_month = first.replace(day=1)
    japan_month, europe_month, us_month = (d.replace(day=1) for d in (japan, europe, us))
    last_ecb = max(cached.get("ea_policy_rate", {}), default="")
    jobs = {
        "jp_inflation": lambda: japan_cpi(fetch(JP_CPI), start_month, japan_month),
        "jp_labor": lambda: japan_labor(fetch(JP_LFS), start_month, japan_month),
        "jp_policy": partial(boj_policy, first, japan, fetch),
        "ea_policy": lambda: ecb_policy_points(fetch(ECB_RATE), europe),
        "eurostat_archive": partial(_eurostat_archive, first, cutoff, fetch),
        "pce": lambda: pce_points(
            fetch(FRED + "/graph/fredgraph.csv?id=PCEPI,PCEPILFE"), start_month, us_month,
        ),
        "bea_current": lambda: bea_releases(fetch(BEA_CURRENT), cutoff),
        "bea_first": lambda: bea_releases(fetch(BEA_ARCHIVE), cutoff),
        "jp_inflation_current": partial(_jp_summary_release, JP_CPI_LATEST, japan, fetch),
        "jp_labor_current": partial(_jp_summary_release, JP_LFS_LATEST, japan, fetch),
    }
    jobs.update({
        key: partial(_eurostat_series, key, start_month, europe_month, fetch)
        for key in EUROSTAT_SERIES
    })
    # Cached public observations retain their archived metadata. Revisit old pages on cold starts.
    if len(cached.get("us_pce", {})) < 30:
        jobs["bea_older"] = lambda: bea_releases(fetch(BEA_ARCHIVE + "&page=1"), cutoff)
    for kind, chart_key, archives in (
        ("cpi", "jp_inflation", _JP_CPI_ARCHIVES), ("lfs", "jp_labor", _JP_LFS_ARCHIVES),
    ):
        cache_key = "jp_unemployment" if kind == "lfs" else chart_key
        for index, (params, label) in enumerate(archives):
            if index == 0 and len(cached.get(cache_key, {})) >= 30:
                continue
            jobs[f"{chart_key}_archive_{index}"] = partial(_estat_archive, kind, params, label, japan, fetch)
            if len(cached.get(cache_key, {})) < 30:
                jobs[f"{chart_key}_archive_old_{index}"] = partial(
                    _estat_archive, kind, params, label, japan, fetch, page=2,
                )
    gathered = _job_results(jobs)
    fresh = {"jp_inflation": gathered.get("jp_inflation", {})}
    for key in ("jp_labor", *EUROSTAT_SERIES, "pce"):
        fresh.update(gathered.get(key, {}))
    boj_points, releases, verified = gathered.get("jp_policy", ({}, {}, {}))
    fresh.update(boj_points)

    ea_rates = gathered.get("ea_policy", {})
    if ea_rates:
        fresh["ea_policy_rate"] = ea_rates
        if max(ea_rates) >= last_ecb:
            verified["ea_policy_rate"] = europe.isoformat()
        anchor = max((day for day in ea_rates if day < first.isoformat()), default=first.isoformat())
        active = {day: value for day, value in ea_rates.items()
                  if day >= anchor and (not last_ecb or day >= last_ecb)}
        ecb_days = [_day(day) for day in active]
        ecb_years = {day.year for day in ecb_days} | {day.year - 1 for day in ecb_days if day.month == 1 and day.day <= 7}
        links = {}
        for source in _job_results({year: partial(_ecb_archive, year, fetch) for year in ecb_years}).values():
            links.update(source)
        releases["ea_policy_rate"] = _policy_details(links, active)

    releases.update(_old_eurostat_releases(cutoff))
    for key, history in gathered.get("eurostat_archive", {}).items():
        releases[key].update(history)
    pce_releases = {}
    cpi_releases = {}
    labor_releases = {}
    for key, records in gathered.items():
        if key.startswith("bea_"):
            pce_releases.update({day: min(record, pce_releases.get(day, record)) for day, record in records.items()})
        elif key.startswith(("jp_inflation_archive_", "jp_inflation_current")):
            cpi_releases.update({day: min(record, cpi_releases.get(day, record)) for day, record in records.items()})
        elif key.startswith(("jp_labor_archive_", "jp_labor_current")):
            labor_releases.update({day: min(record, labor_releases.get(day, record)) for day, record in records.items()})
    releases.update(us_pce=pce_releases, us_core_pce=pce_releases, jp_inflation=cpi_releases,
                    jp_employment=labor_releases, jp_unemployment=labor_releases)
    return fresh, releases, verified
