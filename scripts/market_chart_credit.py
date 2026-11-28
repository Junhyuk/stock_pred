"""Official settlement-date margin lending balances for the market charts."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlencode

from roboquant.index_forecast.official_credit import FSC_URL, _nonnegative_integer

SERIES = "margin_credit_total"
OFFICIAL_SOURCES = {"kofia_freesis_margin_credit", "fsc_kofia_margin_credit"}
CREDIT_SPECS = {
    SERIES: {
        "label": "전체 신용융자 잔고",
        "unit": "KRW",
        "source": "금융투자협회",
        "source_url": "https://www.data.go.kr/data/15094809/openapi.do",
        "date_basis": "settlement_date",
        "note": "신용거래융자 유가증권·코스닥 합계입니다. 결제일 기준이며 공표까지 시차가 있습니다.",
    },
}


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _seen_at(value: str | None, cutoff: datetime) -> bool:
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.utcoffset() is not None and stamp <= cutoff
    except (TypeError, ValueError):
        return False


def _extend(rows, first: date, through: date) -> dict[str, int]:
    points = {}
    for day, amount in rows:
        whole_won = type(amount) is int or (type(amount) is float and amount.is_integer())
        if first <= day <= through and whole_won and amount > 0:
            points[day.isoformat()] = int(amount)
    return points


def workbook_points(rows, first: date, through: date) -> dict[str, int]:
    """Read the '신용거래융자' columns from an official FreeSIS period export."""
    title, unit, categories, markets = (next(rows, ()) for _ in range(4))
    if (title[:1] != ("신용공여 잔고 추이",) or unit[:1] != ("[단위 :백만원]",)
            or categories[1:2] != ("신용거래융자",) or markets[1:4] != ("전체", "유가증권", "코스닥")):
        raise ValueError("금융투자협회 일별 신용거래융자 원본과 열/단위가 일치하지 않습니다.")
    values = {}
    for row in rows:
        if not any(value is not None for value in row):
            continue
        day = datetime.strptime(row[0], "%Y/%m/%d").date()
        total, kospi, kosdaq = (_nonnegative_integer(str(amount).replace(",", "")) for amount in row[1:4])
        if day in values or not total or abs(total - kospi - kosdaq) > 1:
            raise ValueError("중복 결제일 또는 시장 합계 불일치")
        values[day] = total * 1_000_000
    return _extend(values.items(), first, through)


def read_workbook(path: Path, first: date, through: date) -> dict[str, int]:
    from openpyxl import load_workbook

    with path.open("rb") as stream:
        workbook = load_workbook(stream, read_only=True, data_only=True)
        try:
            sheet = workbook["신용공여 잔고 추이"]
            # FreeSIS can advertise A1:A1 for the range of the entire table.
            sheet.reset_dimensions()
            return workbook_points(iter(sheet.values), first, through)
        finally:
            workbook.close()


def _status_points(public_dir: Path, first: date, through: date, cutoff: datetime) -> dict[str, int]:
    status = _read(public_dir / "market_indicators_status.json")
    if status.get("schema_version") != "market_indicators_status_v1":
        return {}
    return _extend(
        ((date.fromisoformat(row["observation_date"]), row["value"])
         for row in status.get("observations", [])
         if row.get("series") == "MARGIN_CREDIT_BALANCE" and row.get("market") == "GLOBAL"
         and row.get("unit") == "KRW" and row.get("source") in OFFICIAL_SOURCES
         and row.get("is_final") is not False and _seen_at(row.get("received_at"), cutoff)
         and (not row.get("published_at") or _seen_at(row["published_at"], cutoff))),
        first, through,
    )


def _catalog_points(public_dir: Path, first: date, through: date, cutoff: datetime) -> dict[str, int]:
    catalog = _read(public_dir / "market-indicator-catalog.json")
    if catalog.get("schema_version") != "market_indicator_catalog_v1":
        return {}
    for row in catalog.get("series", []):
        if (row.get("id") == "official_margin_credit_balance_global" and row.get("unit") == "KRW"
                and row.get("source") in OFFICIAL_SOURCES and _seen_at(row.get("updated_at"), cutoff)):
            return _extend(((date.fromisoformat(day), amount) for day, amount in row["points"]), first, through)
    return {}


def fsc_points(first: date, through: date, fetch, api_key: str, known: dict[str, int]) -> dict[str, int]:
    """Bound the official interval request and confirm its unit against same-day FreeSIS totals."""
    values = {}
    page_size = 500
    for page in (1, 2, 3):
        params = dict(
            serviceKey=unquote(api_key), resultType="json", beginBasDt=first.strftime("%Y%m%d"),
            endBasDt=(through + timedelta(days=1)).strftime("%Y%m%d"), numOfRows=page_size, pageNo=page,
        )
        response = json.loads(fetch(FSC_URL + "?" + urlencode(params)))["response"]
        if response["header"]["resultCode"] not in ("00", "0000"):
            raise ValueError("금융위원회 신용융자 API 응답 오류")
        body = response["body"]
        count = _nonnegative_integer(body["totalCount"])
        if count == 0 and page == 1:
            return {}
        entries = body["items"]["item"]
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list) or len(entries) != min(page_size, count - len(values)):
            raise ValueError("신용융자 기간 응답이 불완전합니다.")
        for row in entries:
            day = date.fromisoformat(str(row["basDt"]))
            total = _nonnegative_integer(row["crdTrFingWhl"])
            parts = _nonnegative_integer(row["crdTrFingScrs"]) + _nonnegative_integer(row["crdTrFingKosdaq"])
            if not first <= day <= through or not total or abs(total - parts) > 1 or day.isoformat() in values:
                raise ValueError("신용융자 API의 일자 또는 합계가 일치하지 않습니다.")
            values[day.isoformat()] = total
        if len(values) == count:
            break
    else:
        raise ValueError("신용융자 API 응답이 페이지 한도를 초과합니다.")
    factors = {
        scale for day in values.keys() & known.keys()
        for scale in (1, 1_000, 1_000_000, 1_000_000_000)
        if abs(values[day] * scale - known[day]) < 1_000_000
    }
    if len(factors) != 1:
        raise ValueError("동일 결제일 공식 잔고와 API 단위를 대조하지 못했습니다.")
    scale = factors.pop()
    return {day: value * scale for day, value in values.items()}


def credit_history(first: date, through: date, cached, fetch, *, public_dir: Path, cutoff: datetime) -> dict:
    points = _catalog_points(public_dir, first, through, cutoff)
    workbook = os.environ.get("KOFIA_CREDIT_XLSX")
    if workbook:
        points.update(read_workbook(Path(workbook), first, through))
    points.update(_status_points(public_dir, first, through, cutoff))
    api_key = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()
    known = {**cached.get(SERIES, {}), **points}
    if api_key and known:
        query_first = first
        if min(known) <= (first + timedelta(days=14)).isoformat() and len(known) > 650:
            query_first = max(first, date.fromisoformat(max(known)) - timedelta(days=14))
        try:
            points = {**fsc_points(query_first, through, fetch, api_key, known), **points}
        except (OSError, LookupError, ValueError, TypeError):
            # Preserve the official workbook/snapshot during API or unit failures.
            pass
    return {SERIES: points}
