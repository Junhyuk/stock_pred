#!/usr/bin/env python3
"""Collect public US Treasury daily constant-maturity yields, not FRED data.

The resulting snapshot is not point-in-time data: published_at is deliberately
blank because the source does not give historical publication timestamps.
"""
import argparse
import csv
import io
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.request import Request, urlopen

ARCHIVE = 'https://home.treasury.gov/system/files/276/yield-curve-rates-1990-2024.csv'
XML = ('https://home.treasury.gov/resource-center/data-chart-center/interest-rates/'
       'pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value={year}')
COLUMNS = ('market', 'series', 'observation_date', 'value', 'unit', 'source',
           'received_at', 'published_at', 'is_final', 'availability_policy')
START = date(2019, 1, 2)
ECOS_ITEMS = {
    '010190000': (1, '국고채(1년)'),
    '010200000': (3, '국고채(3년)'),
    '010210000': (10, '국고채(10년)'),
}


def ecos_key():
    """Read credentials from the environment or this workspace's .env."""
    for name in ('ECOS_API_KEY', 'BOK_API_KEY'):
        if os.environ.get(name, '').strip():
            return os.environ[name].strip()
    env_path = Path(__file__).resolve().parents[1] / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8-sig').splitlines():
            match = re.match(r'^\s*(?:export\s+)?(ECOS_API_KEY|BOK_API_KEY)\s*=\s*(.*?)\s*$', line)
            if match:
                value = match.group(2).strip().strip('"\'')
                if value and not value.startswith('#'):
                    return value
    return ''


def parse_ecos_page(content, code, received, start=START, end=None):
    end = end or datetime.now(UTC).date()
    try:
        body = json.loads(content)
        data = body['StatisticSearch']
        total = int(data['list_total_count'])
        raw_rows = data['row']
        if total <= 0 or not isinstance(raw_rows, list) or not raw_rows:
            raise ValueError
        maturity, name = ECOS_ITEMS[code]
        rows = []
        for record in raw_rows:
            if (record['STAT_CODE'] != '817Y002' or record['ITEM_CODE1'] != code
                    or record['ITEM_NAME1'] != name or record['UNIT_NAME'] != '연%'):
                raise ValueError
            day = datetime.strptime(record['TIME'], '%Y%m%d').date()
            value = record['DATA_VALUE']
            if not math.isfinite(float(value)) or not start <= day <= end:
                raise ValueError
            rows.append({'market': 'GLOBAL', 'series': f'KR_YIELD_{maturity}Y',
                         'observation_date': day.isoformat(), 'value': value,
                         'unit': 'percent', 'source': 'bok_ecos_817Y002',
                         'received_at': received, 'published_at': '', 'is_final': 'true',
                         'availability_policy': 'historical_dated_yield'})
        return rows, total
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise ValueError('ECOS returned an error or invalid yield data') from None


def collect_ecos(key, fetcher=None, start=START, end=None):
    end = end or datetime.now(UTC).date()
    if not key or key.lower() == 'sample' or not re.fullmatch(r'[A-Za-z0-9]+', key):
        raise ValueError('Provide an authorized ECOS_API_KEY or BOK_API_KEY; sample keys are not supported')
    if fetcher is None:
        fetcher = fetch
    all_rows = []
    date_sets = []
    for code in ECOS_ITEMS:
        offset = 1
        expected_total = None
        series_rows = []
        while expected_total is None or len(series_rows) < expected_total:
            page_end = offset + 999
            url = (f'https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/'
                   f'{offset}/{page_end}/817Y002/D/{start:%Y%m%d}/{end:%Y%m%d}/{code}')
            try:
                content, received = fetcher(url)
            except Exception:
                raise ValueError('ECOS request failed; verify network access and credential') from None
            page, total = parse_ecos_page(content, code, received, start, end)
            if expected_total is not None and total != expected_total:
                raise ValueError('ECOS total changed during pagination; retry collection')
            expected_total = total
            if len(page) != min(1000, total - len(series_rows)):
                raise ValueError('ECOS returned an incomplete page')
            series_rows.extend(page)
            offset += len(page)
        dates = {row['observation_date'] for row in series_rows}
        if len(dates) != len(series_rows):
            raise ValueError('ECOS returned duplicate observation dates')
        date_sets.append(dates)
        all_rows.extend(series_rows)
    if any(dates != date_sets[0] for dates in date_sets[1:]):
        raise ValueError('ECOS tenor dates do not match; refusing incomplete mix')
    return all_rows


def fetch(url):
    with urlopen(Request(url, headers={'User-Agent': 'stock-pred-research/1.0'}), timeout=60) as response:
        content = response.read()
    return content, datetime.now(UTC).isoformat()


def treasury_urls(start, end):
    urls = [ARCHIVE] if start.year <= 2024 else []
    urls.extend(XML.format(year=year) for year in range(max(start.year, 2025), end.year + 1))
    return urls


def generate(output, start=START, end=None, include_korea=False):
    end = end or datetime.now(UTC).date()
    output = Path(output)
    if start > end or start < date(1990, 1, 1):
        raise ValueError('Start must be between 1990-01-01 and end')
    key = ecos_key() if include_korea else ''
    if include_korea and (not key or key.lower() == 'sample'):
        raise ValueError('Provide an authorized ECOS_API_KEY or BOK_API_KEY in the environment or workspace .env')
    urls = treasury_urls(start, end)
    with ThreadPoolExecutor(max_workers=3) as pool:
        files = list(pool.map(fetch, urls))
    rows = []
    seen = set()

    def add(day, maturity, raw, retrieved):
        if not start <= day <= end or raw in ('', None, 'N/A'):
            return
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(f'nonfinite value for {day} {maturity}')
        series = f'US_YIELD_{maturity}Y'
        key = day, series
        if key in seen:
            raise ValueError(f'duplicate {key}')
        seen.add(key)
        rows.append({'market': 'GLOBAL', 'series': series,
                     'observation_date': day.isoformat(), 'value': raw,
                     'unit': 'percent', 'source': 'us_treasury_daily_par_yield_curve',
                     'received_at': retrieved, 'published_at': '',
                     'is_final': 'true', 'availability_policy': 'historical_dated_yield'})

    first_xml = 0
    if urls[0] == ARCHIVE:
        archive, received = files[0]
        reader = csv.DictReader(io.StringIO(archive.decode('utf-8-sig')))
        for record in reader:
            day = datetime.strptime(record['Date'], '%m/%d/%y').date()
            for maturity in (1, 3, 10):
                add(day, maturity, record[f'{maturity} Yr'], received)
        first_xml = 1
    namespace = {'a': 'http://www.w3.org/2005/Atom',
                 'm': 'http://schemas.microsoft.com/ado/2007/08/dataservices/metadata',
                 'd': 'http://schemas.microsoft.com/ado/2007/08/dataservices'}
    for content, received in files[first_xml:]:
        root = ET.fromstring(content)
        entries = root.findall('a:entry', namespace)
        if not entries:
            raise ValueError('Treasury XML feed returned no records for a requested year')
        for entry in entries:
            p = entry.find('.//m:properties', namespace)
            if p is None:
                raise ValueError('Treasury entry is missing properties')
            day = date.fromisoformat(p.findtext('d:NEW_DATE', namespaces=namespace)[:10])
            for maturity in (1, 3, 10):
                add(day, maturity, p.findtext(f'd:BC_{maturity}YEAR', namespaces=namespace), received)
    if include_korea:
        rows.extend(collect_ecos(key, start=start, end=end))
    rows.sort(key=lambda row: (row['observation_date'], row['series']))
    if not rows:
        raise ValueError('No observations')
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + '.tmp')
    with temp.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(output)
    for series in sorted({row['series'] for row in rows}):
        selected = [row for row in rows if row['series'] == series]
        print(series, len(selected), selected[0]['observation_date'], selected[-1]['observation_date'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--include-korea', action='store_true', help='Fetch authenticated ECOS government bond yields')
    parser.add_argument('--output', type=Path, required=True, help='Write canonical CSV to this path')
    parser.add_argument('--start', type=date.fromisoformat, default=START, help='First observation date, YYYY-MM-DD (default 2019-01-02)')
    parser.add_argument('--end', type=date.fromisoformat, default=datetime.now(UTC).date(), help='Last observation date, YYYY-MM-DD (default UTC today)')
    args = parser.parse_args()
    try:
        generate(output=args.output, start=args.start, end=args.end, include_korea=args.include_korea)
    except Exception as exc:
        print(f'Collection failed: {exc}', file=sys.stderr)
        sys.exit(1)
