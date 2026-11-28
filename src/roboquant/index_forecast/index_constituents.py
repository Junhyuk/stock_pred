"""Publish index memberships from Korea Investment & Securities security masters.

The archive timestamp is the master's snapshot date, not a KRX session date.
The upstream files are current snapshots and cannot answer historical queries.
Field widths and index flags follow the KIS-published parsers:
https://github.com/koreainvestment/open-trading-api/tree/main/stocks_info
KOSPI200 membership is encoded as a nonzero sector code, KOSDAQ150 as Y.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from roboquant.index_forecast.automation_state import atomic_json

KST = ZoneInfo("Asia/Seoul")
INDEX_CONFIG = {
    "KOSPI200": ("kospi_code.mst", 227, 18, b"123456789AB", 180, 230),
    "KOSDAQ150": ("kosdaq_code.mst", 221, 35, b"Y", 130, 180),
}
BASE_URL = "https://new.real.download.dws.co.kr/common/master/"


def valid_market(index: str, market: object, *, today: str | None = None) -> bool:
    try:
        if not isinstance(market, dict) or set(market) != {"as_of", "count", "constituents"}:
            return False
        day = date.fromisoformat(market["as_of"])
        if day.isoformat() != market["as_of"] or (today and day > date.fromisoformat(today)):
            return False
        rows = market["constituents"]
        low, high = INDEX_CONFIG[index][-2:]
        if not isinstance(rows, list) or type(market["count"]) is not int or market["count"] != len(rows) or not low <= len(rows) <= high:
            return False
        codes = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"code", "name"}:
                return False
            code, name = row["code"], row["name"]
            if not isinstance(code, str) or not re.fullmatch(r"[0-9A-Z]{6}", code) or code in codes:
                return False
            if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
                return False
            codes.add(code)
        return True
    except (KeyError, TypeError, ValueError):
        return False


def valid_payload(payload: object, *, today: str | None = None) -> bool:
    return (isinstance(payload, dict) and set(payload) == {"schema_version", "indices"}
            and type(payload["schema_version"]) is int and payload["schema_version"] == 1
            and isinstance(payload["indices"], dict)
            and set(payload["indices"]) == set(INDEX_CONFIG)
            and all(valid_market(k, v, today=today) for k, v in payload["indices"].items()))


def parse_kis_zip(index: str, data: bytes, today: str) -> dict:
    filename, suffix_size, flag_offset, accepted, _, _ = INDEX_CONFIG[index]
    if len(data) > 5_000_000:
        raise ValueError("oversized archive")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) != 1 or members[0].filename.lower() != filename or members[0].file_size > 10_000_000:
            raise ValueError("unexpected master archive")
        info = members[0]
        snapshot = date(*info.date_time[:3])
        today_date = date.fromisoformat(today)
        if snapshot > today_date or snapshot < today_date - timedelta(days=14):
            raise ValueError("future or stale master snapshot")
        data = archive.read(info)
    rows = []
    lines = data.splitlines()
    if len(lines) < 500:
        raise ValueError("incomplete security master")
    for line in lines:
        if len(line) <= 21 + suffix_size:
            raise ValueError("malformed security master row")
        suffix = line[-suffix_size:]
        flag = suffix[flag_offset:flag_offset + 1]
        allowed_flags = b"0123456789AB" if index == "KOSPI200" else b"YN"
        if len(flag) != 1 or flag[0] not in allowed_flags:
            raise ValueError("unrecognized membership flag")
        if flag[0] in accepted:
            code = line[:9].decode("ascii").strip()
            name = line[21:-suffix_size].decode("cp949").strip()
            rows.append({"code": code, "name": name})
    rows.sort(key=lambda item: item["code"])
    result = {"as_of": snapshot.isoformat(), "count": len(rows), "constituents": rows}
    if not valid_market(index, result, today=today):
        raise ValueError("invalid constituent snapshot")
    return result


def fetch_kis(index: str, today: str) -> dict:
    filename = INDEX_CONFIG[index][0]
    with urlopen(BASE_URL + filename + ".zip", timeout=20) as response:
        data = response.read(5_000_001)
    return parse_kis_zip(index, data, today)


def collect(path: Path, today: str, fetch=fetch_kis) -> dict:
    path = Path(path)
    previous = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if valid_payload(payload, today=today):
                previous = payload["indices"].copy()
            else:
                raise ValueError("invalid existing snapshot")
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("unreadable existing snapshot") from exc
    result = previous.copy()
    for index in INDEX_CONFIG:
        try:
            candidate = fetch(index, today)
            if not valid_market(index, candidate, today=today):
                raise ValueError("invalid market snapshot")
            old = previous.get(index)
            if old and (candidate["as_of"] < old["as_of"] or candidate["count"] < old["count"] * 0.9):
                raise ValueError("stale or incomplete market snapshot")
            result[index] = candidate
        except Exception as exc:
            print(f"{index}: collection failed ({type(exc).__name__}); retaining previous snapshot")
    payload = {"schema_version": 1, "indices": result}
    if valid_payload(payload, today=today) and payload != {"schema_version": 1, "indices": previous}:
        atomic_json(path, payload, indent=2)
    return payload
