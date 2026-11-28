#!/usr/bin/env python3
"""Collect observed series for the public market chart, with a private receipt cache."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sqlite3
import sys
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from html import unescape
from itertools import pairwise
from pathlib import Path
from statistics import stdev
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from roboquant.index_forecast.automation_state import atomic_json
from roboquant.index_forecast.economic_sources import _Links, _month_year, _Tables
from roboquant.index_forecast.market_calendar import exchange_schedule
from roboquant.index_forecast.official_yields import ECOS_ITEMS, US_URL, _parse_treasury
from scripts.export_overseas_market_prices import _validate_split_series
from scripts.market_chart_credit import CREDIT_SPECS, credit_history
from scripts.market_chart_macro import MACRO_SPECS, macro_history

SCHEMA = "market_chart_history_v1"
OUTPUT = "market-chart-history.json"
DAYS = 370
TENORS = (1, 3, 5, 10)
KR_TENORS = {**{code: years for code, (years, _) in ECOS_ITEMS.items()}, "010200001": 5}
SEOUL = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
ECOS_HOME = "https://ecos.bok.or.kr/"
BOK_RATE = "https://www.bok.or.kr/portal/singl/baseRate/list.do?menuNo=200656"
FRED = "https://fred.stlouisfed.org"
TREASURY = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates"
JGB = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
JGB_HISTORICAL = JGB + "historical/jgbcme_all.csv"
JGB_CURRENT = JGB + "jgbcme.csv"
CBOE_CSV = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
BLS = "https://data.bls.gov/timeseries/"
NAVER = "https://m.stock.naver.com/domestic/index/"
MONTHLY_NOTE = "가로축은 통계 기준월입니다. 값은 최신 공식 시계열이며, 표시된 발표일은 공식 보도자료 게시 날짜입니다."
ROLL_NOTE = "연속 선물의 일별 종가로, 계약이 바뀌는 날에는 가격 차이가 반영될 수 있습니다."
NETWORK_ERRORS = (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError,
                  ElementTree.ParseError)
Points = dict[str, float]
ReleaseDates = dict[str, tuple[str, str]]
Fetch = Callable[[str], bytes]


@dataclass(frozen=True)
class Series:
    label: str
    unit: str
    source: str
    source_url: str
    date_basis: str = "observation_date"
    note: str | None = None

    def metadata(self) -> dict[str, str]:
        fields = {"label": self.label, "unit": self.unit, "source": self.source,
                  "source_url": self.source_url, "date_basis": self.date_basis}
        if self.note:
            fields["note"] = self.note
        return fields


YAHOO = {
    # symbol, label, display unit, provider currency, provider instrument type
    "sp500": ("^GSPC", "S&P 500", "index_points", "USD", "INDEX"),
    "nasdaq": ("^IXIC", "나스닥 종합", "index_points", "USD", "INDEX"),
    "sox": ("^SOX", "필라델피아 반도체", "index_points", "USD", "INDEX"),
    "ewy": ("EWY", "iShares 한국 ETF", "USD", "USD", "ETF"),
    "koru": ("KORU", "Direxion 한국 3배 ETF", "USD", "USD", "ETF"),
    "usdkrw": ("KRW=X", "원/달러", "KRW", "KRW", "CURRENCY"),
    "dxy": ("DX-Y.NYB", "달러 인덱스", "index_points", "USD", "INDEX"),
    "wti": ("CL=F", "WTI 원유 선물", "USD/bbl", "USD", "FUTURE"),
    "brent": ("BZ=F", "브렌트유 선물", "USD/bbl", "USD", "FUTURE"),
    "gold": ("GC=F", "금 선물", "USD/oz", "USD", "FUTURE"),
    "copper": ("HG=F", "구리 선물", "USD/lb", "USD", "FUTURE"),
    "es_futures": ("ES=F", "S&P 500 E-mini 선물", "index_points", "USD", "FUTURE"),
    "nq_futures": ("NQ=F", "나스닥 100 E-mini 선물", "index_points", "USD", "FUTURE"),
    "btc": ("BTC-USD", "비트코인", "USD", "USD", "CRYPTOCURRENCY"),
    "eth": ("ETH-USD", "이더리움", "USD", "USD", "CRYPTOCURRENCY"),
}
WIDE_YAHOO = frozenset({"usdkrw", "dxy", "wti", "brent", "btc", "eth"})
CRYPTO_NOTE = "UTC 기준으로 하루가 끝난 뒤 집계하는 일별 종가이며 주말을 포함합니다."
US_ECON = {
    "us_inflation": "CUUR0000SA0", "us_unemployment": "LNS14000000",
    "us_employment": "CES0000000001",
}
KR_ECON = {
    "kr_inflation": ("901Y009", "0", None, "2020=100"),
    "kr_unemployment": ("901Y027", "I61BC", "I28A", "%"),
    "kr_employment": ("901Y027", "I61BA", "I28A", "천명"),
}
RELEASE_MATCH = {
    "kr_inflation": ("KR", "cpi_headline_yoy", "NSA", "kr_mods_cpi_latest_release"),
    "kr_unemployment": ("KR", "unemployment_rate", "NSA", "kr_mods_employment_latest_release"),
    "kr_employment": ("KR", "employment_yoy_change", "NSA", "kr_mods_employment_latest_release"),
    "us_inflation": ("US", "cpi_headline_yoy", "NSA", "us_bls_cpi_current_press_release"),
    "us_unemployment": ("US", "unemployment_rate", "SA", "us_bls_unemployment_latest"),
    "us_employment": ("US", "total_nonfarm_employment", "SA", "us_bls_nonfarm_employment_latest"),
}
ECON_ARCHIVES = {
    "bls_cpi": ("us_inflation",),
    "bls_empsit": ("us_unemployment", "us_employment", "us_employment_change"),
    "mods_cpi": ("kr_inflation",),
    "mods_employment": ("kr_unemployment", "kr_employment"),
}
MODS_ARCHIVES = {
    "mods_cpi": ("a20101000000", "11751", "Consumer Price Index in "),
    "mods_employment": ("a20105020000", "11732", "Economically Active Population Survey in "),
}
def yahoo_spec(key: str) -> Series:
    symbol, label, unit, _, kind = YAHOO[key]
    notes = {"FUTURE": ROLL_NOTE, "CRYPTOCURRENCY": CRYPTO_NOTE}
    return Series(label, unit, "Yahoo Finance", f"https://finance.yahoo.com/quote/{quote(symbol, safe='')}/history/",
                  "observation_date" if kind == "CRYPTOCURRENCY" else "session_date", notes.get(kind))


SPECS = {
    **{
        key: yahoo_spec(key) for key in YAHOO
    },
    **{
        key: Series(label, "index_points", "네이버페이 증권", NAVER + key.upper(), "session_date")
        for key, label in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ"))
    },
    "vix": Series("Cboe VIX", "index_points", "Cboe", CBOE_CSV, "session_date"),
    **{
        f"{key}_return": Series(f"{key.upper()} 일간 수익률", "percent", "국내 지수 종가",
                                NAVER + key.upper(), "session_date", "직전 거래일 종가 대비 등락률입니다.")
        for key in ("kospi", "kosdaq")
    },
    **{
        f"{key}_volatility": Series(f"{key.upper()} 20거래일 실현 변동성", "percent", "국내 지수 종가",
                                    NAVER + key.upper(), "session_date",
                                    "최근 연속 20거래일 일간 수익률의 표본 표준편차를 연율화했습니다(√252).")
        for key in ("kospi", "kosdaq")
    },
    "kospi200_basis": Series("KOSPI200 선물-현물", "index_points", "네이버페이 증권",
                             NAVER + "FUT", "session_date",
                             "같은 거래일의 선물 종가에서 현물 종가를 뺀 값입니다. " + ROLL_NOTE),
    **{
        f"{country}_{years}y": Series(f"{label} {years}년", "percent", source, url)
        for country, label, source, url in (
            ("kr", "한국 국고채", "한국은행 ECOS", ECOS_HOME),
            ("us", "미국 국채", "미국 재무부", TREASURY),
            ("jp", "일본 국채", "일본 재무성", JGB + "index.htm"),
        )
        for years in TENORS
    },
    **{
        key: Series(
            label, unit, source, url, "reference_month",
            MONTHLY_NOTE + (" 2025년 10월 비교 지수가 없어 미국의 2026년 10월 전년동월비는 발표되지 않았습니다."
                            if key == "us_inflation" else ""),
        )
        for key, label, unit, source, url in (
            ("kr_inflation", "한국 소비자물가 전년동월비", "percent", "국가데이터처 · 한국은행 ECOS", ECOS_HOME),
            ("kr_unemployment", "한국 실업률(원계열)", "percent", "국가데이터처 · 한국은행 ECOS", ECOS_HOME),
            ("kr_employment", "한국 취업자 전년동월 증감(원계열)", "thousand_persons", "국가데이터처 · 한국은행 ECOS", ECOS_HOME),
            ("us_inflation", "미국 소비자물가 전년동월비", "percent", "미국 노동통계국", BLS + US_ECON["us_inflation"]),
            ("us_unemployment", "미국 실업률(계절조정)", "percent", "미국 노동통계국", BLS + US_ECON["us_unemployment"]),
            ("us_employment", "미국 비농업 고용(계절조정)", "thousand_persons", "미국 노동통계국", BLS + US_ECON["us_employment"]),
            ("us_employment_change", "미국 비농업 고용 전월 증감(계절조정)", "thousand_persons",
             "미국 노동통계국", BLS + US_ECON["us_employment"]),
        )
    },
    "kr_policy_rate": Series("한국은행 기준금리", "percent", "한국은행", BOK_RATE,
                             "effective_date", "한국은행이 기록한 기준금리 변경일입니다."),
    "us_policy_rate": Series("미국 연방기금금리 목표 범위 중간값", "percent", "연방준비제도 · FRED",
                             FRED + "/series/DFEDTARU", "effective_date",
                             "연준 목표 범위 상한과 하한의 산술 평균입니다."),
    "us_policy_rate_upper": Series("미국 연방기금금리 목표 상한", "percent", "연방준비제도 · FRED",
                                   FRED + "/series/DFEDTARU", "effective_date"),
    "us_policy_rate_lower": Series("미국 연방기금금리 목표 하한", "percent", "연방준비제도 · FRED",
                                   FRED + "/series/DFEDTARL", "effective_date"),
    **{key: Series(**fields) for key, fields in CREDIT_SPECS.items()},
    **{key: Series(**fields) for key, fields in MACRO_SPECS.items()},
}
RATE_SOURCES = {
    "kr": {"bok_ecos_817Y002", "bok_ecos_817Y002_sample"},
    "us": {"us_treasury_daily_par_yield_curve"},
    "jp": {"mof_jgb_historical"},
}
RATE_PREFIXES = {"kr": "KR_KTB", "us": "US_TREASURY", "jp": "JP_JGB"}
YIELD_KEYS = {country: tuple(f"{country}_{years}y" for years in TENORS) for country in RATE_SOURCES}
WIDE_KEYS = WIDE_YAHOO | CREDIT_SPECS.keys() | {key for keys in YIELD_KEYS.values() for key in keys}
LOCAL_MARKET = {
    "sp500": ("US_SPX", "yfinance_poc"),
    "nasdaq": ("US_NASDAQ", "yfinance_poc"),
    "sox": ("US_SOX", "yfinance_poc"),
    "ewy": ("EWY", "yfinance_poc"),
    "usdkrw": ("YAHOO_USDKRW", "yahoo_chart_unofficial"),
    "dxy": ("YAHOO_DXY", "yahoo_chart_unofficial"),
    "wti": ("YAHOO_WTI", "yahoo_chart_unofficial"),
    "btc": ("YAHOO_BTC", "yahoo_chart_unofficial"),
    "eth": ("YAHOO_ETH", "yahoo_chart_unofficial"),
}
CACHE_TABLE = """CREATE TABLE IF NOT EXISTS market_chart_observation (
    series TEXT NOT NULL, observation_date TEXT NOT NULL, value REAL NOT NULL,
    source TEXT NOT NULL, source_url TEXT NOT NULL, received_at TEXT NOT NULL,
    PRIMARY KEY (series, observation_date, received_at)
) WITHOUT ROWID"""


def download(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
    with urlopen(req, timeout=5) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("oversize response")
    return raw


def canonical_day(raw) -> str | None:
    try:
        day = date.fromisoformat(raw).isoformat()
    except (ValueError, TypeError):
        return None
    return day if day == raw else None


def years_ago(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # February 29 in a non-leap year.
        return day.replace(year=day.year - years, day=28)


def number(raw) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def add_point(points: Points, day: str | None, value: float | None) -> None:
    if day is None or value is None:
        return
    if day in points and points[day] != value:
        raise ValueError("conflicting observations for one date")
    points[day] = value


def yahoo_points(key: str, payload: dict, cutoff: datetime) -> Points:
    symbol, _, _, currency, instrument = YAHOO[key]
    chart = payload["chart"]
    if chart["error"] or not chart["result"]:
        raise ValueError("missing Yahoo series")
    result = chart["result"][0]
    meta = result["meta"]
    if (meta["symbol"], meta["currency"], meta["instrumentType"], meta["dataGranularity"]) != (
        symbol, currency, instrument, "1d"
    ):
        raise ValueError("unexpected Yahoo instrument identity")
    zone = ZoneInfo(meta["exchangeTimezoneName"])
    today = cutoff.astimezone(zone).date()
    crypto = instrument == "CRYPTOCURRENCY"
    if crypto and zone.key != "UTC":
        raise ValueError("unexpected Yahoo crypto day boundary")
    regular_end = meta.get("currentTradingPeriod", {}).get("regular", {}).get("end")
    closed_today = regular_end is not None and (
        datetime.fromtimestamp(regular_end, zone).date() == today
        and datetime.fromtimestamp(regular_end, UTC) + timedelta(minutes=15) <= cutoff
    )
    points = {}
    for stamp, raw in zip(result["timestamp"], result["indicators"]["quote"][0]["close"], strict=True):
        day = datetime.fromtimestamp(stamp, zone).date()
        value = number(raw)
        valid_close = value is not None and (value > 0 or key in {"wti", "brent"})
        if (crypto or day.weekday() < 5) and (day < today or (not crypto and day == today and closed_today)) and valid_close:
            add_point(points, day.isoformat(), value)
    if instrument == "ETF":
        _validate_split_series(sorted(points.items()), result.get("events", {}).get("splits", {}))
    return {day: round(value, 4) for day, value in points.items()}


def fetch_yahoo(key: str, cutoff: datetime, fetch: Fetch, *, refresh_from: date | None = None) -> Points:
    symbol = quote(YAHOO[key][0], safe="")
    query = "range=5y" if key in WIDE_YAHOO else "range=2y"
    if refresh_from:
        # Include the neighboring day to cover Yahoo's various exchange time zones.
        lower = datetime.combine(refresh_from - timedelta(days=1), time(), UTC)
        query = f"period1={int(lower.timestamp())}&period2={int(cutoff.timestamp()) + 1}"
    for host in ("query1", "query2"):
        try:
            points = yahoo_points(key, json.loads(fetch(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?{query}&interval=1d&events=div%2Csplits"
            )), cutoff)
            if points:
                return points
        except NETWORK_ERRORS + (ZeroDivisionError,):
            continue
    return {}


def naver_points(symbol: str, raw: bytes, through: date) -> Points:
    chart = ElementTree.fromstring(raw.decode("euc-kr")).find("./chartdata")
    if chart is None or chart.get("symbol") != symbol or chart.get("timeframe") != "day":
        raise ValueError("unexpected Naver chart identity")
    points = {}
    for item in chart.findall("item"):
        stamp, _, _, _, raw_close, _ = item.get("data", "").split("|")
        day = datetime.strptime(stamp, "%Y%m%d").date()
        value = number(raw_close)
        if day <= through and value and value > 0:
            add_point(points, day.isoformat(), value)
    return points


def fetch_naver(symbol: str, through: date, fetch: Fetch) -> Points:
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={symbol}&timeframe=day&count=300&requestType=0"
    return naver_points(symbol, fetch(url), through)


def fetch_vix(through: date, fetch: Fetch) -> Points:
    rows = csv.DictReader(io.StringIO(fetch(CBOE_CSV).decode("utf-8-sig")))
    points = {}
    for row in rows:
        day = datetime.strptime(row["DATE"], "%m/%d/%Y").date()
        if day <= through:
            add_point(points, day.isoformat(), number(row["CLOSE"]))
    return points


def fetch_us_yields(year: int, through: date, fetch: Fetch) -> dict[str, Points]:
    observations = _parse_treasury(fetch(US_URL.format(year=year)), "", through, maturities=TENORS)
    return {
        f"us_{years}y": {day.isoformat(): values[years] for day, (values, _) in observations.items()}
        for years in TENORS
    }


def last_jgb_day(cutoff: datetime) -> date:
    # MOF releases the previous business day's yields at 09:30 JST.
    tokyo = cutoff.astimezone(TOKYO)
    release_through = tokyo.date() - timedelta(days=tokyo.time() < time(9, 30))
    _, sessions = exchange_schedule("XTKS", release_through - timedelta(days=21), release_through)
    return sessions[-2].date()


def japan_points(raw: bytes, first: date, through: date) -> dict[str, Points]:
    lines = io.StringIO(raw.decode("cp932"))
    if "(Unit : %)" not in next(lines, ""):
        raise ValueError("unexpected MOF JGB unit")
    rows = csv.DictReader(lines)
    columns = {f"{tenor}Y": f"jp_{tenor}y" for tenor in TENORS}
    if not {"Date", *columns} <= set(rows.fieldnames or ()):
        raise ValueError("missing MOF JGB tenor")
    points: dict[str, Points] = {key: {} for key in columns.values()}
    for row in rows:
        stamp = (row["Date"] or "").strip()
        if not re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", stamp):
            continue
        day = datetime.strptime(stamp, "%Y/%m/%d").date()
        if first <= day <= through:
            for column, key in columns.items():
                add_point(points[key], day.isoformat(), number(row[column]))
    return points


def fetch_ecos(
    table: str, item: str, second: str | None, unit: str, cycle: str, first: date, last: date,
    api_key: str, fetch: Fetch,
) -> Points:
    if first > last:
        return {}
    date_format = "%Y%m%d" if cycle == "D" else "%Y%m"
    page_size = 10 if api_key == "sample" else 500
    maximum = (last - first).days + 1 if cycle == "D" else (last.year - first.year) * 12 + last.month - first.month + 1
    codes = f"{item}/{second}" if second else item
    points = {}
    # The sample API accepts ten consecutive observations on any valid page.
    endpoint = f"{ECOS_HOME}api/StatisticSearch/{api_key}/json/kr"
    total = None
    for start in range(1, maximum + 1, page_size):
        end = start + page_size - 1
        url = f"{endpoint}/{start}/{end}/{table}/{cycle}/{first.strftime(date_format)}/{last.strftime(date_format)}/{codes}"
        payload = json.loads(fetch(url))
        if start == 1 and payload.get("RESULT", {}).get("CODE") == "INFO-200":
            return {}
        data = payload["StatisticSearch"]
        rows = data["row"]
        offered = int(data["list_total_count"])
        if total is None:
            total = offered
        if not start <= offered <= maximum or offered != total or len(rows) != min(page_size, total - start + 1):
            raise ValueError("incomplete ECOS page")
        for row in rows:
            if (row["STAT_CODE"], row["ITEM_CODE1"], row.get("ITEM_CODE2"), row["UNIT_NAME"]) != (
                table, item, second, unit
            ):
                raise ValueError("unexpected ECOS series identity")
            day = datetime.strptime(row["TIME"], date_format).date()
            if first <= day <= last:
                add_point(points, day.isoformat(), number(row["DATA_VALUE"]))
        if end >= total:
            return points
    raise ValueError("ECOS pagination limit reached")


def fetch_bls(series: str, first: date, last: date, fetch: Fetch) -> Points:
    url = (
        f"https://api.bls.gov/publicAPI/v2/timeseries/data/{series}"
        f"?startyear={first.year}&endyear={last.year}"
    )
    body = json.loads(fetch(url))
    result = body["Results"]["series"][0]
    if body["status"] != "REQUEST_SUCCEEDED" or result["seriesID"] != series:
        raise ValueError("unexpected BLS series identity")
    points = {}
    for row in result["data"]:
        if re.fullmatch(r"M(?:0[1-9]|1[0-2])", row["period"]):
            day = f"{row['year']}-{row['period'][1:]}-01"
            if first.isoformat() <= day <= last.isoformat():
                add_point(points, canonical_day(day), number(row["value"]))
    return points


def bls_release_dates(kind: str, raw: bytes, through: date) -> ReleaseDates:
    links = _Links()
    links.feed(raw.decode("utf-8"))
    result = {}
    for href, label in links.links:
        match = re.fullmatch(rf"/news\.release/archives/{kind}_(\d{{8}})\.htm", href)
        if match and (period := _month_year(label)):
            release = datetime.strptime(match.group(1), "%m%d%Y").date().isoformat()
            if (day := period + "-01") < release <= through.isoformat():
                result[day] = (release, "https://www.bls.gov" + href)
    return result


def fetch_mods_releases(kind: str, page: int, through: date, fetch: Fetch) -> ReleaseDates:
    menu, board, title = MODS_ARCHIVES[kind]
    origin = "https://mods.go.kr"
    url = f"{origin}/board.es?mid={menu}&bid={board}&act=list&nPage={page}"
    html = fetch(url).decode("utf-8")
    result = {}
    for entry in html.split('class="board_link"')[1:]:
        href = re.match(r"""\s+href="javascript:addSearchParam\('(/board\.es\?[^']+)'\);""", entry)
        label = entry.split("</a>", 1)[0]
        published = re.search(r"<strong>Date</strong>\s*<span>(\d{4}-\d{2}-\d{2})</span>", entry)
        matches_title = title in label or (kind == "mods_cpi" and "Cousumer Price Index in " in label)
        if href and matches_title and published and (period := _month_year(label)):
            release = canonical_day(published.group(1))
            if release and (day := period + "-01") < release <= through.isoformat():
                result[day] = (release, urljoin(origin, unescape(href.group(1))))
    return result


def fetch_bok_rate(through: date, fetch: Fetch) -> Points:
    tables = _Tables()
    tables.feed(fetch(BOK_RATE).decode("utf-8"))
    for _, table in tables.tables:
        if not table or "변경일자" not in " ".join(table[0]):
            continue
        points = {}
        for row in table:
            if len(row) != 3:
                continue
            match = re.fullmatch(r"(\d{1,2})월\s*(\d{1,2})일", row[1])
            if len(row[0]) == 4 and row[0].isdigit() and match:
                day = date(int(row[0]), int(match.group(1)), int(match.group(2)))
                if day <= through:
                    add_point(points, day.isoformat(), number(row[2]))
        if points:
            return points
    raise ValueError("missing BOK rate change table")


def fetch_fed_rate(series: str, first: date, through: date, fetch: Fetch) -> Points:
    url = f"{FRED}/graph/fredgraph.csv?id={series}&cosd={first.isoformat()}"
    rows = csv.DictReader(io.StringIO(fetch(url).decode("utf-8-sig")))
    if not rows.fieldnames or series not in rows.fieldnames:
        raise ValueError("unexpected FRED series identity")
    points = {}
    for row in rows:
        day = canonical_day(row[rows.fieldnames[0]])
        if day and day <= through.isoformat():
            add_point(points, day, number(row[series]))
    return points


def year_over_year(points: Points, *, as_percent: bool) -> Points:
    result = {}
    for day, current in points.items():
        prior = points.get(f"{int(day[:4]) - 1:04d}{day[4:]}")
        if prior is not None and (prior > 0 or not as_percent):
            result[day] = round((current / prior - 1) * 100 if as_percent else current - prior, 1)
    return result


def month_over_month(points: Points) -> Points:
    result = {}
    for day, current in points.items():
        previous = (date.fromisoformat(day) - timedelta(days=1)).replace(day=1).isoformat()
        if previous in points:
            result[day] = round(current - points[previous], 6)
    return result


def domestic_derived(close: Points, sessions: list[str]) -> tuple[Points, Points]:
    returns, volatility = {}, {}
    daily = deque(maxlen=20)
    for previous, day in pairwise(sessions):
        if previous not in close or day not in close:
            daily.clear()
            continue
        value = (close[day] / close[previous] - 1) * 100
        daily.append(value)
        returns[day] = round(value, 6)
        if len(daily) == daily.maxlen:
            volatility[day] = round(stdev(daily) * math.sqrt(252), 6)
    return returns, volatility


def basis_points(spot: Points, futures: Points) -> Points:
    return {day: round(futures[day] - spot[day], 2) for day in sorted(spot.keys() & futures.keys())}


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def known_at(raw, cutoff: datetime) -> bool:
    try:
        stamp = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return False
    return stamp.utcoffset() is not None and stamp <= cutoff


def release_metadata(history: Path, series: dict[str, Points], cutoff: datetime) -> dict:
    saved = read_json(history)
    if saved.get("schema_version") != "economic_history_v1":
        return {}
    by_definition = {spec: key for key, spec in RELEASE_MATCH.items()}
    matches = {}
    for row in saved.get("release_versions", ()):
        if not isinstance(row, dict):
            continue
        key = by_definition.get(tuple(row.get(field) for field in (
            "country", "measure", "seasonal_adjustment", "source_id"
        )))
        if key not in series or row.get("unit") != SPECS[key].unit:
            continue
        day = canonical_day(f"{row.get('reference_period')}-01")
        release = canonical_day(row.get("actual_release_date"))
        if release is None and known_at(row.get("actual_release_at"), cutoff):
            release = canonical_day(row["actual_release_at"][:10])
        url = row.get("source_url")
        actual = number(row.get("value"))
        if (day not in series[key] or actual is None or abs(series[key][day] - actual) > 1e-6
                or not known_at(row.get("available_at"), cutoff) or not release
                or not day < release <= cutoff.date().isoformat()
                or not isinstance(url, str) or not url.startswith("https://")):
            continue
        existing = matches.setdefault(key, {})
        if day not in existing or release < existing[day][0]:
            existing[day] = (release, url)
    return matches


def retained(path: Path, through: date) -> dict[str, Points]:
    old = read_json(path)
    if old.get("schema_version") != SCHEMA or not isinstance(old.get("series"), dict):
        return {}
    result = {}
    for key, spec in SPECS.items():
        row = old["series"].get(key)
        if (not isinstance(row, dict) or row.get("unit") != spec.unit
                or row.get("date_basis") != spec.date_basis or not isinstance(row.get("points"), list)):
            continue
        values = {}
        for point in row["points"]:
            if not isinstance(point, list) or len(point) != 2:
                continue
            day, value = point
            day = canonical_day(day)
            if day and day <= through.isoformat() and type(value) in (int, float):
                try:
                    add_point(values, day, number(value))
                except ValueError:
                    values = {}
                    break
        result[key] = values
    return result


def read_sqlite(path: Path, cutoff: datetime, first: date, *, legacy_first: date | None = None) -> dict[str, Points]:
    if not path.is_file():
        return {}
    observed = {}
    minimum, maximum = first.isoformat(), cutoff.astimezone(SEOUL).date().isoformat()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "index_eod" in tables:
            for series, day, close, receipt in db.execute(
                """SELECT index_code, session_date, close, ingested_at FROM index_eod
                   WHERE index_code IN ('KOSPI','KOSDAQ') AND session_date BETWEEN ? AND ?
                   ORDER BY session_date, ingested_at""", ((legacy_first or first).isoformat(), maximum)
            ):
                if canonical_day(day) and known_at(receipt, cutoff) and (value := number(close)) and value > 0:
                    observed.setdefault(series.lower(), {})[day] = value
        if "macro_series" in tables:
            mapping = {identity: key for key, identity in LOCAL_MARKET.items()}
            mapping.update({
                (f"{prefix}_{years}Y", source): f"{country}_{years}y"
                for country, prefix in RATE_PREFIXES.items()
                for years in TENORS for source in RATE_SOURCES[country]
            })
            for series, day, raw, provider, receipt in db.execute(
                """SELECT series_code, observation_date, value, source, ingested_at FROM macro_series
                   WHERE observation_date BETWEEN ? AND ?
                   ORDER BY observation_date, revision_no, ingested_at""", (minimum, maximum)
            ):
                key = mapping.get((series, provider))
                if key and canonical_day(day) and known_at(receipt, cutoff) and (value := number(raw)) is not None:
                    if key in ("btc", "eth") and day >= datetime.fromisoformat(receipt).astimezone(UTC).date().isoformat():
                        continue
                    observed.setdefault(key, {})[day] = value
        if "session_input_observations" in tables:
            for series, day, raw, provider, receipt in db.execute(
                """SELECT series, observation_date, value, source, received_at FROM session_input_observations
                   WHERE market='GLOBAL' AND unit='percent' AND is_final=1 AND observation_date BETWEEN ? AND ?
                   ORDER BY observation_date, received_at""", (minimum, maximum)
            ):
                country, _, tenor = series.lower().partition("_yield_")
                if (provider in RATE_SOURCES.get(country, ()) and f"{country}_{tenor}" in YIELD_KEYS[country]
                        and canonical_day(day) and known_at(receipt, cutoff) and (value := number(raw)) is not None):
                    observed.setdefault(f"{country}_{tenor}", {})[day] = value
        if "market_chart_observation" in tables:
            for key, day, raw, receipt in db.execute(
                """SELECT series, observation_date, value, received_at FROM market_chart_observation
                   WHERE observation_date BETWEEN ? AND ?
                   ORDER BY observation_date, received_at""", (minimum, maximum)
            ):
                if key in SPECS and canonical_day(day) and known_at(receipt, cutoff) and (value := number(raw)) is not None:
                    observed.setdefault(key, {})[day] = value
    return observed


def persist(path: Path, series: dict, receipt: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(CACHE_TABLE)
        for key, row in series.items():
            points = row["points"]
            if not points:
                continue
            previous = {}
            for day, value, source, url in db.execute(
                """SELECT observation_date, value, source, source_url FROM market_chart_observation
                   WHERE series=? AND observation_date BETWEEN ? AND ?
                   ORDER BY observation_date, received_at""", (key, points[0][0], points[-1][0])
            ):
                previous[day] = (value, source, url)
            db.executemany("INSERT OR IGNORE INTO market_chart_observation VALUES (?, ?, ?, ?, ?, ?)", (
                (key, day, value, row["source"], row["source_url"], receipt.isoformat())
                for day, value in points
                if previous.get(day) != (value, row["source"], row["source_url"])
            ))


def daily_refresh_start(points: Points, first: date, *, every_day: bool = False) -> date:
    """Refresh the whole window when the saved observations have a substantial hole."""
    days = sorted(date.fromisoformat(day) for day in points if day >= first.isoformat())
    max_gap = 3 if every_day else 14
    min_per_ten_days = 9 if every_day else 6
    if not days or (days[0] - first).days > max_gap:
        return first
    if (len(days) * 10 < ((days[-1] - days[0]).days + 1) * min_per_ten_days
            or any((end - start).days > max_gap for start, end in pairwise(days))):
        return first
    return max(first, days[-1] - timedelta(days=14))


def missing_treasury_years(first: date, through: date, cached: dict[str, Points]) -> list[int]:
    missing = []
    for year in range(first.year, through.year):
        start = max(first, date(year, 1, 1))
        end = date(year, 12, 31)
        for key in YIELD_KEYS["us"]:
            annual = {day: value for day, value in cached[key].items() if start.isoformat() <= day <= end.isoformat()}
            if not annual or max(annual) < (end - timedelta(days=7)).isoformat() or daily_refresh_start(annual, start) == start:
                missing.append(year)
                break
    return [*missing, through.year]


def source_results(
    cutoff: datetime, first: date, cached: dict[str, Points], fetch: Fetch, public_dir: Path,
) -> tuple[dict[str, Points], dict[str, ReleaseDates], dict[str, str]]:
    kst, et = cutoff.astimezone(SEOUL), cutoff.astimezone(NY)
    kr_through = kst.date() - timedelta(days=kst.time() < time(16, 30))
    kr_yields_through = kst.date() - timedelta(days=1)
    jp_through = last_jgb_day(cutoff)
    us_through = et.date() - timedelta(days=et.time() < time(17))
    month = kst.date().replace(day=1)
    first_month = years_ago(first.replace(day=1), 1)
    api_key = os.environ.get("BOK_ECOS_API_KEY", "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9]+", api_key):
        api_key = "sample"
    jobs = {"macro": partial(macro_history, cutoff, first, cached, fetch)}
    for key in YAHOO:
        refresh_from = daily_refresh_start(cached[key], first, every_day=key in ("btc", "eth")) if key in WIDE_YAHOO else first
        jobs[key] = partial(fetch_yahoo, key, cutoff, fetch, refresh_from=refresh_from if refresh_from > first else None)
    jobs.update({
        key: partial(fetch_naver, symbol, kr_through, fetch)
        for key, symbol in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ"), ("spot", "KPI200"), ("future", "FUT"))
    })
    jobs.update({
        "vix": partial(fetch_vix, us_through, fetch),
        "kr_policy_rate": partial(fetch_bok_rate, kst.date(), fetch),
        "us_policy_rate_upper": partial(fetch_fed_rate, "DFEDTARU", first - timedelta(days=1), us_through, fetch),
        "us_policy_rate_lower": partial(fetch_fed_rate, "DFEDTARL", first - timedelta(days=1), us_through, fetch),
    })
    jobs.update({
        f"treasury_{year}": partial(fetch_us_yields, year, us_through, fetch)
        for year in missing_treasury_years(first, us_through, cached)
    })
    if any(daily_refresh_start(cached[key], first) <= jp_through - timedelta(days=45) for key in YIELD_KEYS["jp"]):
        jobs["jgb_historical"] = lambda: japan_points(fetch(JGB_HISTORICAL), first, jp_through)
    jobs["jgb_current"] = lambda: japan_points(fetch(JGB_CURRENT), first, jp_through)
    for code, years in KR_TENORS.items():
        key = f"kr_{years}y"
        yield_first = daily_refresh_start(cached[key], first)
        jobs[key] = partial(fetch_ecos, "817Y002", code, None, "연%", "D",
                            yield_first, kr_yields_through, api_key, fetch)
    jobs.update({
        key: partial(fetch_ecos, table, item, second, unit, "M",
                     first_month, month, api_key, fetch)
        for key, (table, item, second, unit) in KR_ECON.items()
    })
    jobs.update({
        key: partial(fetch_bls, series, first_month, month, fetch)
        for key, series in US_ECON.items()
    })

    def bls_archive(kind: str) -> ReleaseDates:
        raw = fetch(f"https://www.bls.gov/bls/news-release/{kind}.htm")
        return bls_release_dates(kind, raw, et.date())

    jobs.update({f"bls_{kind}": partial(bls_archive, kind) for kind in ("cpi", "empsit")})
    jobs.update({
        f"{kind}_{page}": partial(fetch_mods_releases, kind, page, kst.date(), fetch)
        for kind in MODS_ARCHIVES for page in (1, 2, 3, 4, 5)
    })
    jobs["credit"] = partial(credit_history, first, kst.date(), cached, fetch, public_dir=public_dir, cutoff=cutoff)

    fresh = {}
    releases = {}
    verified = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {key: pool.submit(job) for key, job in jobs.items()}
        for key, future in futures.items():
            try:
                points = future.result()
            except NETWORK_ERRORS:
                continue
            archive = key.rsplit("_", 1)[0] if key.startswith("mods_") else key
            if archive in ECON_ARCHIVES:
                for series in ECON_ARCHIVES[archive]:
                    releases.setdefault(series, {}).update(points)
                # BLS published October 2025 establishment data in the November report.
                # https://www.bls.gov/bls/2025-lapse-revised-release-dates.htm
                november = points.get("2025-11-01")
                if archive == "bls_empsit" and november and november[0] == "2025-12-16":
                    for series in ("us_employment", "us_employment_change"):
                        releases[series]["2025-10-01"] = november
            elif key.startswith(("treasury_", "jgb_")) or key == "credit":
                for series, values in points.items():
                    fresh.setdefault(series, {}).update(values)
            elif key == "macro":
                values, dates, verification = points
                fresh.update(values)
                releases.update(dates)
                verified.update(verification)
            else:
                fresh[key] = points

    fresh["kospi200_basis"] = basis_points(fresh.pop("spot", {}), fresh.pop("future", {}))
    fresh["us_policy_rate"] = {
        day: round((upper + fresh["us_policy_rate_lower"][day]) / 2, 6)
        for day, upper in fresh.get("us_policy_rate_upper", {}).items()
        if day in fresh.get("us_policy_rate_lower", {}) and upper >= fresh["us_policy_rate_lower"][day]
    }
    for key in ("kr_inflation", "us_inflation", "kr_employment"):
        fresh[key] = year_over_year(fresh.get(key, {}), as_percent=key.endswith("_inflation"))
    fresh["us_employment_change"] = month_over_month(fresh.get("us_employment", {}))
    if fresh.get("kr_policy_rate"):
        verified["kr_policy_rate"] = kst.date().isoformat()
    for key in ("us_policy_rate", "us_policy_rate_upper", "us_policy_rate_lower"):
        if fresh.get(key):
            verified[key] = max(fresh[key])
    return fresh, releases, verified


def _storable(points: Points, start: str, *, keep_older: bool) -> list[list[str | float]]:
    earlier = [day for day in points if day < start] if keep_older else []
    boundary = max(earlier, default=start)
    return [[day, value] for day, value in sorted(points.items()) if day >= boundary]


def retained_releases(row: dict, days: Points) -> ReleaseDates:
    dates = row.get("release_dates", {})
    urls = row.get("release_urls", {})
    if not isinstance(dates, dict) or not isinstance(urls, dict):
        return {}
    return {day: (dates[day], urls[day]) for day in days if day in dates and day in urls}


def dated_releases(details: ReleaseDates, points: list, through: date, *, effective: bool) -> ReleaseDates:
    valid = {}
    for day, _ in points:
        if day not in details:
            continue
        release, url = details[day]
        if (canonical_day(release) and release <= through.isoformat() and isinstance(url, str)
                and url.startswith("https://") and (release <= day if effective else day < release)):
            valid[day] = release, url
    return valid


def export_history(
    sqlite: Path, output: Path, *, seed: Path | None = None, public_dir: Path | None = None,
    as_of: datetime | None = None, fetch: Fetch = download, read_only: bool = True,
) -> dict:
    cutoff = as_of or datetime.now(UTC)
    if cutoff.utcoffset() is None:
        raise ValueError("A timezone-aware cutoff is required")
    public_dir = public_dir or output.parent
    if (sqlite.resolve().is_relative_to(public_dir.resolve())
            or sqlite.resolve().is_relative_to(output.resolve().parent)):
        raise ValueError("The private database must be outside the public directory")
    through = cutoff.astimezone(SEOUL).date()
    first = years_ago(through, 3)
    legacy_first = through - timedelta(days=DAYS)
    warmup = legacy_first - timedelta(days=45)
    seed = output if output.is_file() else (seed or output)
    saved = retained(seed, through)
    local = read_sqlite(sqlite, cutoff, first - timedelta(days=45), legacy_first=warmup)
    cached = {key: {**local.get(key, {}), **saved.get(key, {})} for key in SPECS}
    fresh, archived_releases, verified = source_results(cutoff, first, cached, fetch, public_dir)
    combined = {key: {**cached[key], **fresh.get(key, {})} for key in SPECS}
    # A missing release month cannot serve as the previous month for the difference.
    combined["us_employment_change"] = {
        **combined["us_employment_change"], **month_over_month(combined["us_employment"]), **fresh["us_employment_change"],
    }
    # Do not mix old ETF prices with newer prices that have a different split basis.
    for key in ("ewy", "koru"):
        values = fresh.get(key, {})
        if len(values) >= 220 and max(values) >= max(saved.get(key, {}), default=""):
            combined[key] = values
        else:
            combined[key] = saved.get(key) or local.get(key) or values
    _, session_index = exchange_schedule("XKRX", warmup, through)
    sessions = [day.date().isoformat() for day in session_index]
    for key in ("kospi", "kosdaq"):
        returns, volatility = domestic_derived(combined[key], sessions)
        combined[f"{key}_return"].update(returns)
        combined[f"{key}_volatility"].update(volatility)

    releases = release_metadata(public_dir / "economic_history.json", combined, cutoff)
    seed_series = read_json(seed).get("series", {})
    seed_series = seed_series if isinstance(seed_series, dict) else {}
    series = {}
    for key, spec in SPECS.items():
        monthly = spec.date_basis == "reference_month"
        effective = spec.date_basis == "effective_date"
        wide = monthly or effective or key in WIDE_KEYS
        start = (first.replace(day=1) if monthly else first if wide else legacy_first).isoformat()
        if not fresh.get(key) and saved.get(key):
            start = min(start, min(saved[key]))
        points = _storable(combined[key], start, keep_older=effective)
        row = {
            **spec.metadata(), "points": points, "history_years": 3 if wide else 1,
            "coverage": {"rows": len(points), "first": points[0][0] if points else None,
                         "last": points[-1][0] if points else None},
        }
        if monthly or effective:
            old = seed_series.get(key, {})
            details = retained_releases(old, saved.get(key, {})) if isinstance(old, dict) else {}
            if key == "us_employment_change":
                level = seed_series.get("us_employment", {})
                if isinstance(level, dict):
                    details.update(retained_releases(level, saved.get("us_employment", {})))
            details.update(archived_releases.get(key, {}))
            details.update(releases.get(key, {}))
            details = dated_releases(details, points, through, effective=effective)
            if details:
                row["release_dates"] = {day: item[0] for day, item in details.items()}
                row["release_urls"] = {day: item[1] for day, item in details.items()}
            if effective and points:
                last = points[-1][0]
                prior = old.get("verified_through") if isinstance(old, dict) else None
                current = verified.get(key) if max(fresh.get(key, {}), default="") >= last else None
                candidates = [day for day in (prior, current)
                              if canonical_day(day) and last <= day <= through.isoformat()]
                if candidates:
                    row["verified_through"] = max(candidates)
        series[key] = row
    result = {"schema_version": SCHEMA, "as_of_date": through.isoformat(), "series": series}
    if not read_only:
        persist(sqlite, series, datetime.now(UTC))
    if read_json(output) != result:
        atomic_json(output, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/data" / OUTPUT)
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--public-dir", type=Path)
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()
    result = export_history(args.sqlite, args.output, seed=args.seed, public_dir=args.public_dir,
                            read_only=args.read_only)
    print(json.dumps({key: row["coverage"] for key, row in result["series"].items()}))


if __name__ == "__main__":
    main()
