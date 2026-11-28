"""Exact benchmark closes, private receipt/version ledger, and separate usage scopes.

KRX OpenAPI terms §§6.2/10.3/11.2/12.3 permit noncommercial research and
attributed derived screens, not redistribution of source records. FSC's KOGL4
feed is usable for private noncommercial research; public derivatives are not
assumed permitted. Neither source grants a public raw historical chart here.
Historical downloads are reconstruction, never evidence of past receipt.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .indexes import get_official_index
from .market_calendar import exchange_schedule

BENCHMARK_INDEXES = ("KOSPI200", "KOSDAQ150")
KRX_TERMS_URL = "https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO002.jsp"
FSC_INFO_URL = "https://www.data.go.kr/data/15094807/openapi.do"
FSC_ENDPOINT = "https://apis.data.go.kr/1160100/GetMarketIndexInfoService_V2/getStockMarketIndex_V2"
KRX_ENDPOINTS = {
    "KOSPI200": "https://data-dbg.krx.co.kr/svc/apis/idx/kospi_dd_trd",
    "KOSDAQ150": "https://data-dbg.krx.co.kr/svc/apis/idx/kosdaq_dd_trd",
}
REASON_TEXT = {
    "credential_missing": "무료 공식 API 인증키가 설정되지 않았습니다.",
    "authentication_failed": "API 인증키 또는 해당 지수 서비스 승인을 확인해야 합니다.",
    "rate_limited": "API 호출 한도에 도달했습니다.",
    "network_error": "공식 API 연결에 실패했습니다.",
    "invalid_response": "공식 응답의 형식 또는 지수 원본 검증에 실패했습니다.",
    "no_exact_index_rows": "요청 기간의 정확한 대상 지수 원본이 없습니다.",
    "request_budget_exhausted": "이번 수집 예산을 소진했습니다. 다음 실행에서 이어집니다.",
    "no_history": "학습할 수 있는 대상 지수 이력이 없습니다.",
    "derived_permission_missing": "학습 이력의 가공 결과 공개 권한이 확인되지 않았습니다.",
    "raw_chart_permission_missing": "원시 시계열의 공개 차트 이용 권한이 없습니다.",
    "no_pre_cutoff_receipt": "예측 마감 시각 이전에 수신한 원본이 없습니다.",
    "stale_anchor": "수신된 대상 지수 종가가 요구 거래일보다 오래되었습니다.",
    "expected_session_required": "최신성 판단에 필요한 거래일이 지정되지 않았습니다.",
}


@dataclass(frozen=True)
class SourceRights:
    allow_train: bool
    allow_derived: bool
    allow_chart: bool
    evidence_url: str
    attribution: str
    scope: str = "noncommercial"

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (self.allow_train, self.allow_derived, self.allow_chart)):
            raise ValueError("rights flags must be explicit booleans")
        if not self.evidence_url.strip() or not self.attribution.strip():
            raise ValueError("rights evidence and attribution are required")


DEFAULT_RIGHTS = {
    "krx_openapi": SourceRights(True, True, False, KRX_TERMS_URL, "한국거래소 통계정보"),
    "fsc_openapi": SourceRights(True, False, False, FSC_INFO_URL, "금융위원회 · 한국거래소"),
}


@dataclass
class CollectionResult:
    index_code: str
    source: str
    status: str = "unavailable"
    rows_received: int = 0
    rows_inserted: int = 0
    requests: int = 0
    reason_codes: list[str] = field(default_factory=list)
    earliest_session: str | None = None
    latest_session: str | None = None
    received_at: str | None = None
    remaining_sessions: int = 0
    absent_sessions_checked: int = 0

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["reasons"] = [REASON_TEXT.get(code, code) for code in self.reason_codes]
        return result


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class SourceError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_observation (
 id INTEGER PRIMARY KEY, index_code TEXT NOT NULL, official_code TEXT NOT NULL,
 session_date TEXT NOT NULL, source TEXT NOT NULL, source_url TEXT NOT NULL,
 open REAL, high REAL, low REAL, close REAL NOT NULL CHECK(close > 0),
 volume REAL, trading_value REAL, received_at TEXT NOT NULL, available_at TEXT NOT NULL,
 published_at TEXT, publication_rule TEXT NOT NULL, pit_class TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, allow_train INTEGER NOT NULL,
 allow_derived INTEGER NOT NULL, allow_chart INTEGER NOT NULL,
 rights_json TEXT NOT NULL, UNIQUE(index_code,source,session_date,received_at,payload_sha256)
);
CREATE INDEX IF NOT EXISTS benchmark_receipt_idx
 ON benchmark_observation(index_code,session_date,received_at,id);
CREATE TABLE IF NOT EXISTS benchmark_collection (
 id INTEGER PRIMARY KEY, index_code TEXT NOT NULL, source TEXT NOT NULL,
 finished_at TEXT NOT NULL, result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS benchmark_absent_session (
 id INTEGER PRIMARY KEY, index_code TEXT NOT NULL, source TEXT NOT NULL,
 session_date TEXT NOT NULL, checked_at TEXT NOT NULL,
 response_rows INTEGER NOT NULL CHECK(response_rows > 0),
 response_sha256 TEXT NOT NULL,
 UNIQUE(index_code,source,session_date,checked_at,response_sha256)
);
CREATE INDEX IF NOT EXISTS benchmark_absent_session_idx
 ON benchmark_absent_session(index_code,source,session_date);
"""


def _target(index_code: str) -> str:
    index_code = str(index_code).upper()
    if index_code not in BENCHMARK_INDEXES:
        raise ValueError(f"unsupported exact benchmark: {index_code}")
    return index_code


def _utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("an explicit timezone is required")
    return stamp.tz_convert("UTC")


def _stamp(value: Any) -> str:
    return _utc(value).isoformat()


def _today_clock() -> datetime:
    return datetime.now(UTC)


@contextmanager
def _connect(path: str | Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    path = Path(path).expanduser().resolve()
    # Keep restricted raw data out of the static publication tree.
    if any(part in {"docs", "_site", "public"} for part in path.parts):
        raise ValueError("benchmark database must be outside the public site tree")
    if readonly:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=20)
        os.chmod(path, 0o600)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _number(value: Any, *, required: bool = False, positive: bool = False) -> float | None:
    if value in (None, "", "-", "N/A"):
        if required:
            raise SourceError("invalid_response")
        return None
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise SourceError("invalid_response") from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise SourceError("invalid_response")
    return result


def _day(value: Any) -> str:
    raw = str(value)
    try:
        parsed = datetime.strptime(raw, "%Y%m%d").date() if len(raw) == 8 else date.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise SourceError("invalid_response") from exc
    return parsed.isoformat()


def _normal_name(value: Any) -> str:
    return "".join(str(value).upper().split())


def _normalize(row: Mapping[str, Any], index_code: str, source: str) -> dict[str, Any] | None:
    index = get_official_index(index_code)
    if source == "krx_openapi":
        name, day = row.get("IDX_NM"), row.get("BAS_DD")
        columns = {"close": "CLSPRC_IDX", "open": "OPNPRC_IDX", "high": "HGPRC_IDX",
                   "low": "LWPRC_IDX", "volume": "ACC_TRDVOL", "trading_value": "ACC_TRDVAL"}
    elif source == "fsc_openapi":
        name, day = row.get("idxNm"), row.get("basDt")
        columns = {"close": "clpr", "open": "mkp", "high": "hipr", "low": "lopr",
                   "volume": "trqu", "trading_value": "trPrc"}
    else:
        if row.get("index_code") != index_code or str(row.get("official_code")) != index.pykrx_code:
            raise SourceError("invalid_response")
        name, day = index.korean_name, row.get("session_date", row.get("date"))
        columns = {key: key for key in ("close", "open", "high", "low", "volume", "trading_value")}
    if _normal_name(name) not in {_normal_name(index.korean_name), index_code}:
        return None
    normalized = {"session_date": _day(day)}
    for dest, original in columns.items():
        normalized[dest] = _number(row.get(original), required=dest == "close", positive=dest in {"open", "high", "low", "close"})
    low, high, close, opening = (normalized[key] for key in ("low", "high", "close", "open"))
    if low is not None and high is not None:
        if not low <= close <= high or (opening is not None and not low <= opening <= high):
            raise SourceError("invalid_response")
    return normalized


def _sessions(start: str, end: str) -> list[str]:
    _, labels = exchange_schedule("XKRX", start, end)
    return labels.strftime("%Y-%m-%d").tolist()


def _availability(row: dict[str, Any], source: str, received: str) -> tuple[str, str, str]:
    day = row["session_date"]
    receipt = _utc(received)
    if day not in _sessions(day, day):
        raise SourceError("invalid_response")
    calendar, labels = exchange_schedule("XKRX", day, day)
    close_at = _utc(calendar.session_close(labels[0]))
    if receipt < close_at:
        raise SourceError("invalid_response")
    following = _sessions((date.fromisoformat(day) + timedelta(days=1)).isoformat(),
                          (date.fromisoformat(day) + timedelta(days=15)).isoformat())[0]
    rule = "actual_receipt_only; source publication timestamp unknown"
    floor = close_at
    if source == "fsc_openapi":
        floor = pd.Timestamp(f"{following} 13:00", tz="Asia/Seoul").tz_convert("UTC")
        rule = "T+1 XKRX business day after13:00 KST; actual_receipt is mandatory"
    pit = "historical_reconstruction" if receipt.tz_convert("Asia/Seoul").date().isoformat() > following else "observed_receipt"
    return max(receipt, floor).isoformat(), rule, pit


def _persist(conn: sqlite3.Connection, rows: list[dict[str, Any]], *, index_code: str,
             source: str, source_url: str, received_at: str, rights: SourceRights) -> int:
    if not rights.allow_train:
        raise ValueError("private training permission is required")
    inserted = 0
    rights_json = json.dumps(asdict(rights), ensure_ascii=False, sort_keys=True)
    for row in rows:
        available, rule, pit = _availability(row, source, received_at)
        digest = hashlib.sha256(json.dumps({**row, "rights": rights_json}, sort_keys=True).encode()).hexdigest()
        prior = conn.execute("SELECT payload_sha256 FROM benchmark_observation WHERE index_code=? AND source=? AND session_date=? ORDER BY received_at DESC,id DESC LIMIT 1",
                             (index_code, source, row["session_date"])).fetchone()
        if prior and prior[0] == digest:
            continue
        data = dict(index_code=index_code, official_code=get_official_index(index_code).pykrx_code,
                    source=source, source_url=source_url, **row, received_at=received_at,
                    available_at=available, published_at=None, publication_rule=rule, pit_class=pit,
                    payload_sha256=digest, allow_train=int(rights.allow_train),
                    allow_derived=int(rights.allow_derived), allow_chart=int(rights.allow_chart), rights_json=rights_json)
        cursor = conn.execute(f"INSERT OR IGNORE INTO benchmark_observation ({','.join(data)}) VALUES ({','.join('?' for _ in data)})", tuple(data.values()))
        inserted += cursor.rowcount
    return inserted


def _fetch(request: Request, timeout: float) -> HttpResponse:
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise SourceError("invalid_response")
            return HttpResponse(response.status, body)
    except HTTPError as exc:
        # Never expose response text or a URL containing the FSC service key.
        return HttpResponse(exc.code, b"")
    except (OSError, URLError) as exc:
        raise SourceError("network_error") from exc


def _json_response(response: HttpResponse, source: str) -> tuple[list[dict[str, Any]], int]:
    if response.status in (401, 403):
        raise SourceError("authentication_failed")
    if response.status == 429:
        raise SourceError("rate_limited")
    if response.status != 200:
        raise SourceError("network_error")
    try:
        body = json.loads(response.body)
        if source == "krx_openapi":
            if str(body.get("respCode", "")) in {"401", "403"}:
                raise SourceError("authentication_failed")
            rows = body["OutBlock_1"]
            total = len(rows)
        else:
            header = body["response"]["header"]
            code = str(header["resultCode"])
            if code not in {"00", "0"}:
                raise SourceError("authentication_failed" if code in {"20", "21", "22", "30", "31", "32"} else "invalid_response")
            payload = body["response"]["body"]
            items = payload.get("items") or {}
            rows = items.get("item", []) if isinstance(items, dict) else []
            if isinstance(rows, dict):
                rows = [rows]
            total = int(payload["totalCount"])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise SourceError("invalid_response")
        return rows, total
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError("invalid_response") from exc


def _krx_absence_confirmed(rows: list[dict[str, Any]], index_code: str, day: str) -> bool:
    """Checkpoint only an actual dated series response, never an empty outage.

    Called after exact-target parsing returned no quote. Full official-series
    responses must still identify the requested market and session and contain
    valid quote records; malformed/empty/error payloads remain retryable.
    """
    if not rows:
        return False
    market = get_official_index(index_code).flow_market
    for row in rows:
        if (_day(row.get("BAS_DD")) != day or row.get("IDX_CLSS") != market
                or not isinstance(row.get("IDX_NM"), str) or not row["IDX_NM"].strip()):
            return False
        _number(row.get("CLSPRC_IDX"), required=True, positive=True)
    return True


def _finish(conn: sqlite3.Connection, result: CollectionResult, clock: Callable[[], Any]) -> CollectionResult:
    if result.rows_received:
        result.status = "partial" if result.reason_codes else "collected"
    elif not result.reason_codes:
        result.status = "unchanged"
    conn.execute("INSERT INTO benchmark_collection(index_code,source,finished_at,result_json) VALUES (?,?,?,?)",
                 (result.index_code, result.source, _stamp(clock()), json.dumps(result.to_dict(), ensure_ascii=False)))
    conn.commit()
    return result


def collect_benchmark_history(
    db_path: str | Path, *, index_code: str, start_date: str, end_date: str,
    source: str = "auto", env: Mapping[str, str] | None = None,
    max_requests: int = 64, timeout_seconds: float = 12, budget_seconds: float = 120,
    refresh_sessions: int = 3, fetch: Callable[[Request, float], HttpResponse] | None = None,
    receipt_clock: Callable[[], Any] = _today_clock, sleep: Callable[[float], None] = time.sleep,
    rights: SourceRights | None = None,
) -> CollectionResult:
    """Collect missing history plus recent revisions, bounded and resumable.

    KRX: one call per session, latest missing history first. Confirmed historical
    absence is checkpointed privately per index; recent sessions are rechecked.
    FSC: up to1000 exact-name rows/page; use range pagination for initial seed.
    The request budget includes retries. Secrets never enter SQLite or results.
    ``receipt_clock`` is a test seam, never an as-of override in a CLI.
    """
    index_code = _target(index_code)
    start, end = _day(start_date), _day(end_date)
    if start > end or max_requests < 1 or timeout_seconds <= 0 or budget_seconds <= 0 or refresh_sessions < 0:
        raise ValueError("invalid collection range or budget")
    env = os.environ if env is None else env
    krx_key = env.get("KRX_OPENAPI_KEY") or env.get("KRX_AUTH_KEY") or ""
    fsc_key = env.get("DATA_GO_KR_SERVICE_KEY") or ""
    if source == "auto" and krx_key and fsc_key:
        started = time.monotonic()
        primary = collect_benchmark_history(
            db_path, index_code=index_code, start_date=start, end_date=end, source="krx_openapi",
            env=env, max_requests=max_requests, timeout_seconds=timeout_seconds,
            budget_seconds=budget_seconds, refresh_sessions=refresh_sessions,
            fetch=fetch, receipt_clock=receipt_clock, sleep=sleep, rights=rights)
        remaining = budget_seconds - (time.monotonic() - started)
        if (primary.rows_received or not primary.reason_codes or primary.requests >= max_requests
                or remaining <= 0):
            return primary
        # FSC's restrictive scope stays attached to every fallback observation.
        fallback = collect_benchmark_history(
            db_path, index_code=index_code, start_date=start, end_date=end, source="fsc_openapi",
            env=env, max_requests=max_requests-primary.requests, timeout_seconds=timeout_seconds,
            budget_seconds=remaining, refresh_sessions=refresh_sessions,
            fetch=fetch, receipt_clock=receipt_clock, sleep=sleep)
        fallback.requests += primary.requests
        return fallback
    if source == "auto":
        source = "krx_openapi" if krx_key or not fsc_key else "fsc_openapi"
    if source not in DEFAULT_RIGHTS:
        raise ValueError("source must be auto, krx_openapi, or fsc_openapi")
    result = CollectionResult(index_code, source)
    rights = rights or DEFAULT_RIGHTS[source]
    fetch = fetch or _fetch
    key = krx_key if source == "krx_openapi" else fsc_key
    with _connect(db_path) as conn:
        if not key.strip():
            result.reason_codes.append("credential_missing")
            return _finish(conn, result, receipt_clock)
        effective_start = max(start, "2020-01-01") if source == "fsc_openapi" else start
        sessions = _sessions(effective_start, end) if effective_start <= end else []
        existing = {row[0] for row in conn.execute("SELECT DISTINCT session_date FROM benchmark_observation WHERE index_code=? AND source=?", (index_code, source))}
        recent = sessions[-refresh_sessions:] if refresh_sessions else []
        missing = [day for day in sessions if day not in existing]
        if source == "krx_openapi":
            absent = {row[0] for row in conn.execute(
                "SELECT DISTINCT session_date FROM benchmark_absent_session WHERE index_code=? AND source=?",
                (index_code, source))}
            missing = [day for day in reversed(sessions) if day not in existing and day not in absent]
            refresh = [day for day in reversed(recent) if day in existing or day in absent]
            # Reserve at least one request for unseen history, even with a tiny
            # budget. Remaining refreshes stay queued rather than being lost.
            refresh_slots = max_requests - 1 if missing else len(refresh)
            pending = refresh[:refresh_slots] + missing + refresh[refresh_slots:]
        else:
            pending = list(dict.fromkeys(list(reversed(recent)) + missing))
        result.remaining_sessions = len(pending)
        if not pending:
            return _finish(conn, result, receipt_clock)
        endpoint = KRX_ENDPOINTS[index_code] if source == "krx_openapi" else FSC_ENDPOINT
        begin = min(pending)
        # When a bounded FSC run has fetched only the newest page, narrow the
        # next run to still-missing dates instead of restarting that same page.
        query_end = max(missing) if source == "fsc_openapi" and missing else max(pending)
        page = 1
        seen_pages: set[str] = set()
        deadline = time.monotonic() + budget_seconds
        while pending:
            if result.requests >= max_requests or time.monotonic() >= deadline:
                result.reason_codes.append("request_budget_exhausted")
                break
            if source == "krx_openapi":
                request = Request(f"{endpoint}?{urlencode({'basDd': pending[0].replace('-', '')})}",
                                  headers={"AUTH_KEY": key, "Accept": "application/json"})
            else:
                params = {"serviceKey": unquote(key), "resultType": "json", "pageNo": page,
                          "numOfRows": 1000, "beginBasDt": begin.replace("-", ""),
                          # The official description is exclusive at the upper bound.
                          "endBasDt": (date.fromisoformat(query_end) + timedelta(days=1)).strftime("%Y%m%d"),
                          "idxNm": get_official_index(index_code).korean_name}
                request = Request(f"{endpoint}?{urlencode(params)}", headers={"Accept": "application/json"})
            try:
                response = None
                for attempt in range(2):
                    if result.requests >= max_requests or time.monotonic() >= deadline:
                        raise SourceError("request_budget_exhausted")
                    result.requests += 1
                    try:
                        response = fetch(request, min(timeout_seconds, max(.01, deadline - time.monotonic())))
                    except (OSError, TimeoutError, SourceError) as exc:
                        if attempt or (isinstance(exc, SourceError) and exc.reason != "network_error"):
                            raise
                        sleep(min(.5, max(0, deadline - time.monotonic())))
                        continue
                    if response.status not in {500, 502, 503, 504} or attempt:
                        break
                    sleep(min(.5, max(0, deadline - time.monotonic())))
                raw, total = _json_response(response, source)
                received = _stamp(receipt_clock())
                normalized = []
                for row in raw:
                    value = _normalize(row, index_code, source)
                    if value is not None and source == "krx_openapi" and value["session_date"] != pending[0]:
                        raise SourceError("invalid_response")
                    if value is not None and start <= value["session_date"] <= end:
                        normalized.append(value)
                if len({row["session_date"] for row in normalized}) != len(normalized):
                    raise SourceError("invalid_response")
                if source == "fsc_openapi":
                    page_hash = hashlib.sha256(response.body).hexdigest()
                    if page_hash in seen_pages or (total > 0 and not raw):
                        raise SourceError("invalid_response")
                    seen_pages.add(page_hash)
                    try:
                        page_size = int(json.loads(response.body)["response"]["body"].get("numOfRows", 1000))
                        if page_size < 1:
                            raise ValueError("invalid page size")
                    except (TypeError, ValueError) as exc:
                        raise SourceError("invalid_response") from exc
                with conn:
                    inserted = _persist(conn, normalized, index_code=index_code, source=source,
                                        source_url=endpoint, received_at=received, rights=rights)
                    if source == "krx_openapi" and not normalized and _krx_absence_confirmed(raw, index_code, pending[0]):
                        conn.execute(
                            "INSERT OR IGNORE INTO benchmark_absent_session(index_code,source,session_date,checked_at,response_rows,response_sha256) VALUES (?,?,?,?,?,?)",
                            (index_code, source, pending[0], received, len(raw), hashlib.sha256(response.body).hexdigest()))
                        result.absent_sessions_checked += 1
                result.rows_received += len(normalized)
                result.rows_inserted += inserted
                result.received_at = received
                for row in normalized:
                    day = row["session_date"]
                    result.earliest_session = min(result.earliest_session or day, day)
                    result.latest_session = max(result.latest_session or day, day)
                if source == "krx_openapi":
                    pending.pop(0)
                else:
                    covered = {row["session_date"] for row in normalized}
                    pending = [day for day in pending if day not in covered]
                    if page * page_size >= total:
                        break
                    page += 1
                result.remaining_sessions = len(pending)
                if pending:
                    sleep(min(.1, max(0, deadline - time.monotonic())))
            except SourceError as exc:
                result.reason_codes.append(exc.reason)
                break
            except (OSError, TimeoutError):
                result.reason_codes.append("network_error")
                break
        if result.rows_received == 0 and not result.reason_codes:
            result.reason_codes.append("no_exact_index_rows")
        result.remaining_sessions = len(pending)
        return _finish(conn, result, receipt_clock)


def import_benchmark_csv(
    db_path: str | Path, csv_path: str | Path, *, index_code: str, source: str,
    source_url: str, rights: SourceRights, receipt_clock: Callable[[], Any] = _today_clock,
) -> CollectionResult:
    """Import a licensed exact-index CSV; supplied historical receipt dates are ignored.

    Required columns: index_code,official_code,session_date,close. Optional:
    open,high,low,volume,trading_value. ``source`` must identify the licensed
    dataset (not claim to be either built-in API); evidence must cover its use.
    """
    index_code = _target(index_code)
    if source in DEFAULT_RIGHTS or not source.strip() or not source_url.strip():
        raise ValueError("use a distinct licensed source name and its source URL")
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as stream:
        rows = [_normalize(row, index_code, "licensed_csv") for row in csv.DictReader(stream)]
    if not rows or len({row["session_date"] for row in rows}) != len(rows):
        raise SourceError("invalid_response")
    received = _stamp(receipt_clock())
    with _connect(db_path) as conn:
        inserted = _persist(conn, rows, index_code=index_code, source=source,
                            source_url=source_url, received_at=received, rights=rights)
        result = CollectionResult(index_code, source, rows_received=len(rows), rows_inserted=inserted,
                                  earliest_session=min(row["session_date"] for row in rows),
                                  latest_session=max(row["session_date"] for row in rows), received_at=received)
        return _finish(conn, result, receipt_clock)


def load_benchmark_history(
    db_path: str | Path, index_code: str, *, as_of: Any = None, strict_live: bool = False,
    public_raw_only: bool = False, derived_only: bool = False, first_observed: bool = False,
) -> pd.DataFrame:
    """Return exact closes with latest permissible versions and no filling.

    Apply receipt/availability cutoff whenever ``as_of`` is supplied. With no
    cutoff this is a historical reconstruction, not an honest historical replay.
    Permission is filtered BEFORE choosing sources, so a newer restricted FSC
    row cannot hide an older derived-eligible KRX observation.
    """
    index_code = _target(index_code)
    if strict_live and as_of is None:
        raise ValueError("strict_live requires a timezone-aware as_of")
    columns = ["close", "open", "high", "low", "volume", "trading_value", "source",
               "source_url", "received_at", "available_at", "pit_class", "allow_train",
               "allow_derived", "allow_chart", "rights_json", "official_code", "payload_sha256", "attribution"]
    empty = pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], name="date"))
    if not Path(db_path).exists():
        return empty
    where, args = ["index_code=?"], [index_code]
    if as_of is not None:
        where.extend(["received_at<=?", "available_at<=?"])
        args.extend([_stamp(as_of)] * 2)
    with _connect(db_path, readonly=True) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='benchmark_observation'").fetchone():
            return empty
        rows = conn.execute(f"SELECT * FROM benchmark_observation WHERE {' AND '.join(where)} ORDER BY session_date,received_at,id", args).fetchall()
    if not rows:
        return empty
    frame = pd.DataFrame([dict(row) for row in rows])
    # A revoked grant must not resurrect an older version of the same source.
    # Other sources may still have an independently valid grant.
    latest = frame.drop_duplicates(["session_date", "source"], keep="last")
    eligible = latest.loc[latest["allow_train"] == 1]
    if derived_only:
        eligible = eligible.loc[eligible["allow_derived"] == 1]
    if public_raw_only:
        eligible = eligible.loc[eligible["allow_chart"] == 1]
    permitted_pairs = set(zip(eligible["session_date"], eligible["source"], strict=True))
    frame = frame.loc[[pair in permitted_pairs for pair in zip(frame["session_date"], frame["source"], strict=True)]]
    frame = frame.loc[frame["allow_train"] == 1]
    if derived_only:
        frame = frame.loc[frame["allow_derived"] == 1]
    if public_raw_only:
        frame = frame.loc[frame["allow_chart"] == 1]
    if frame.empty:
        return empty
    frame = frame.drop_duplicates("session_date", keep="first" if first_observed else "last")
    frame["date"] = pd.to_datetime(frame["session_date"])
    frame["attribution"] = frame["rights_json"].map(lambda value: json.loads(value)["attribution"])
    return frame.set_index("date")[columns].sort_index()


def benchmark_source_status(
    db_path: str | Path, index_code: str, *, as_of: Any,
    expected_session: str | None = None, env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Safe public metadata only: never includes source prices or credentials."""
    index_code = _target(index_code)
    _utc(as_of)
    env = os.environ if env is None else env
    history = load_benchmark_history(db_path, index_code, as_of=as_of)
    derived = load_benchmark_history(db_path, index_code, as_of=as_of, derived_only=True)
    chart = load_benchmark_history(db_path, index_code, as_of=as_of, public_raw_only=True)
    reasons = []
    latest = history.index[-1].date().isoformat() if not history.empty else None
    live_latest = derived.index[-1].date().isoformat() if not derived.empty else None
    if history.empty:
        reasons.extend(["no_history", "no_pre_cutoff_receipt"])
    if derived.empty:
        reasons.append("derived_permission_missing")
    if chart.empty:
        reasons.append("raw_chart_permission_missing")
    if expected_session is None:
        reasons.append("expected_session_required")
    elif live_latest is not None and live_latest != _day(expected_session):
        reasons.append("stale_anchor")
    attempts = []
    if Path(db_path).exists():
        with _connect(db_path, readonly=True) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='benchmark_collection'").fetchone():
                attempts = [json.loads(row[0]) for row in conn.execute("SELECT result_json FROM benchmark_collection WHERE index_code=? AND finished_at<=? ORDER BY id DESC LIMIT 2", (index_code, _stamp(as_of)))]
    for attempt in attempts:
        reasons.extend(attempt.get("reason_codes", []))
    configured = bool(env.get("KRX_OPENAPI_KEY") or env.get("KRX_AUTH_KEY") or env.get("DATA_GO_KR_SERVICE_KEY"))
    if not configured and history.empty:
        reasons.append("credential_missing")
    return {"index_code": index_code, "official_code": get_official_index(index_code).pykrx_code,
            "rows": len(history), "derived_rows": len(derived), "public_chart_rows": len(chart),
            "earliest_session": history.index[0].date().isoformat() if not history.empty else None,
            "latest_session": latest, "latest_derived_session": live_latest,
            "latest_received_at": str(history.iloc[-1]["received_at"]) if not history.empty else None,
            "allow_train": not history.empty, "allow_derived": not derived.empty,
            "allow_chart": not chart.empty,
            "strict_live_eligible": bool(expected_session and live_latest == _day(expected_session)),
            "historical_reconstruction_rows": int((history["pit_class"] == "historical_reconstruction").sum()),
            "sources": sorted(history["source"].unique().tolist()), "configured": configured,
            "reason_codes": list(dict.fromkeys(reasons)), "collection_attempts": attempts,
            "source_options": [{"source": name, **asdict(rights)} for name, rights in DEFAULT_RIGHTS.items()]}
