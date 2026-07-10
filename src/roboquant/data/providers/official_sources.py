from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from os import environ
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


class OfficialDataConfigurationError(RuntimeError):
    """Raised when an official provider is missing required config or credentials."""


@dataclass(frozen=True)
class OfficialRequestSettings:
    source: str
    endpoint: str
    params: Mapping[str, Any]
    headers: Mapping[str, str] | None = None


class OfficialJsonProvider:
    def __init__(
        self,
        *,
        opener: Callable[[Request], bytes | str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._opener = opener
        self._timeout = timeout

    def request_json(self, settings: OfficialRequestSettings) -> dict[str, Any]:
        endpoint = str(settings.endpoint or "").strip()
        if not endpoint:
            raise OfficialDataConfigurationError(f"{settings.source} endpoint is not configured.")
        query = urlencode({key: value for key, value in settings.params.items() if value is not None})
        url = f"{endpoint}?{query}" if query else endpoint
        request = Request(url, headers=dict(settings.headers or {}), method="GET")
        try:
            if self._opener is not None:
                payload = self._opener(request)
            else:
                with urlopen(request, timeout=self._timeout) as response:
                    payload = response.read()
        except HTTPError as exc:
            raise RuntimeError(f"{settings.source} HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"{settings.source} request failed: {exc.reason}") from exc
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return json.loads(payload.decode("utf-8"))


def collect_gdelt_news(
    *,
    config: Mapping[str, Any],
    query_date: date,
    opener: Callable[[Request], bytes | str] | None = None,
) -> pd.DataFrame:
    settings = config.get("gdelt", config)
    if not bool(settings.get("enabled", True)):
        return _empty_global_news_events()
    endpoint = str(settings.get("endpoint") or "https://api.gdeltproject.org/api/v2/doc/doc")
    max_records = max(1, min(250, int(settings.get("max_records", 75))))
    timespan = str(settings.get("timespan", "1d"))
    queries = [str(item).strip() for item in settings.get("queries", []) if str(item).strip()]
    if not queries:
        queries = ["KOSPI", "Korea semiconductor"]
    provider = OfficialJsonProvider(opener=opener)
    rows: list[dict[str, Any]] = []
    collected_at = _utcnow()
    for query in queries:
        payload = provider.request_json(
            OfficialRequestSettings(
                source="gdelt",
                endpoint=endpoint,
                params={
                    "query": query,
                    "mode": "ArtList",
                    "format": "json",
                    "maxrecords": max_records,
                    "timespan": timespan,
                    "sort": "HybridRel",
                },
            )
        )
        for item in payload.get("articles", []) or []:
            url = _clean(item.get("url"))
            title = _clean(item.get("title"))
            seendate = _parse_datetime(item.get("seendate"))
            raw_json = json.dumps(dict(item), ensure_ascii=False, allow_nan=False)
            event_id = _hash("gdelt", query, url, title, "" if seendate is None else seendate.isoformat())
            rows.append(
                {
                    "event_id": event_id,
                    "query": query,
                    "title": title,
                    "url": url,
                    "domain": _clean(item.get("domain")),
                    "source_country": _clean(item.get("sourcecountry")),
                    "language": _clean(item.get("language")),
                    "seendate": seendate,
                    "tone": _to_float(item.get("tone")),
                    "source": "gdelt_doc_api",
                    "raw_json": raw_json,
                    "collected_at": collected_at,
                }
            )
    return pd.DataFrame(rows, columns=_empty_global_news_events().columns).drop_duplicates(["event_id"])


def collect_bigkinds_issues(
    *,
    config: Mapping[str, Any],
    target_date: date,
    env: Mapping[str, str] | None = None,
    opener: Callable[[Request], bytes | str] | None = None,
) -> pd.DataFrame:
    settings = config.get("bigkinds", config)
    if not bool(settings.get("enabled", True)):
        return _empty_bigkinds_issues()
    endpoint = str(settings.get("endpoint") or "").strip()
    key_name = str(settings.get("service_key_env") or "DATA_GO_KR_SERVICE_KEY")
    key = str((environ if env is None else env).get(key_name, "")).strip()
    if not endpoint or not key:
        missing = "endpoint" if not endpoint else key_name
        raise OfficialDataConfigurationError(f"BIGKinds issue API requires {missing}.")
    provider = OfficialJsonProvider(opener=opener)
    payload = provider.request_json(
        OfficialRequestSettings(
            source="bigkinds_issue_top10",
            endpoint=endpoint,
            params={"serviceKey": key, "type": "json", "date": target_date.strftime("%Y%m%d")},
        )
    )
    items = _extract_items(payload)
    rows = []
    collected_at = _utcnow()
    for index, item in enumerate(items, start=1):
        rank = _to_int(_first(item, "순위", "rank", "RANK")) or index
        rows.append(
            {
                "issue_date": _parse_date(_first(item, "날짜", "date")) or target_date,
                "period": _clean(_first(item, "집계 시기", "period", "PERIOD")) or "unknown",
                "rank": rank,
                "article_count": _to_int(_first(item, "건수", "count", "article_count")),
                "title": _clean(_first(item, "제목", "title", "issue_title")),
                "source": "data_go_kr_bigkinds_issue",
                "raw_json": json.dumps(dict(item), ensure_ascii=False, allow_nan=False),
                "collected_at": collected_at,
            }
        )
    return pd.DataFrame(rows, columns=_empty_bigkinds_issues().columns)


def collect_dart_disclosures(
    *,
    config: Mapping[str, Any],
    symbols: Sequence[str],
    days: int,
    target_date: date,
    env: Mapping[str, str] | None = None,
    opener: Callable[[Request], bytes | str] | None = None,
) -> pd.DataFrame:
    settings = config.get("dart", config)
    if not bool(settings.get("enabled", True)):
        return _empty_dart_disclosures()
    endpoint = str(settings.get("endpoint") or "https://opendart.fss.or.kr/api/list.json")
    key_name = str(settings.get("api_key_env") or "DART_API_KEY")
    key = str((environ if env is None else env).get(key_name, "")).strip()
    if not key:
        raise OfficialDataConfigurationError(f"OpenDART requires {key_name}.")
    start_date = target_date - timedelta(days=max(1, int(days)))
    page_count = max(1, min(100, int(settings.get("page_count", 100))))
    max_pages = max(1, int(settings.get("max_pages", 10)))
    provider = OfficialJsonProvider(opener=opener)
    items: list[Mapping[str, Any]] = []
    for page_no in range(1, max_pages + 1):
        payload = provider.request_json(
            OfficialRequestSettings(
                source="opendart",
                endpoint=endpoint,
                params={
                    "crtfc_key": key,
                    "bgn_de": start_date.strftime("%Y%m%d"),
                    "end_de": target_date.strftime("%Y%m%d"),
                    "page_no": page_no,
                    "page_count": page_count,
                },
            )
        )
        status = str(payload.get("status") or "").strip()
        if status and status != "000":
            if status == "013":
                break
            message = str(payload.get("message") or "unknown error")
            raise RuntimeError(f"opendart status {status}: {message}")
        page_items = list(payload.get("list", []) or [])
        items.extend(page_items)
        total_page = _to_int(payload.get("total_page")) or page_no
        if page_no >= total_page or not page_items:
            break
    symbol_set = {str(symbol).zfill(6) for symbol in symbols}
    rows = []
    collected_at = _utcnow()
    for item in items:
        symbol = str(item.get("stock_code") or "").zfill(6)
        if symbol_set and symbol not in symbol_set:
            continue
        receipt_no = _clean(item.get("rcept_no"))
        rows.append(
            {
                "disclosure_id": _hash("dart", receipt_no, symbol),
                "symbol": symbol or None,
                "corp_code": _clean(item.get("corp_code")),
                "corp_name": _clean(item.get("corp_name")),
                "report_name": _clean(item.get("report_nm")),
                "receipt_no": receipt_no,
                "receipt_date": _parse_date(item.get("rcept_dt")),
                "submitter": _clean(item.get("flr_nm")),
                "disclosure_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}" if receipt_no else None,
                "disclosure_type": _clean(item.get("corp_cls")),
                "source": "opendart",
                "raw_json": json.dumps(dict(item), ensure_ascii=False, allow_nan=False),
                "collected_at": collected_at,
            }
        )
    return pd.DataFrame(rows, columns=_empty_dart_disclosures().columns)


def collect_official_market_prices(
    *,
    config: Mapping[str, Any],
    target_date: date,
    source: str,
    env: Mapping[str, str] | None = None,
    opener: Callable[[Request], bytes | str] | None = None,
) -> pd.DataFrame:
    settings = config.get(source, config)
    if not bool(settings.get("enabled", True)):
        return _empty_official_market_prices()
    endpoint = str(settings.get("endpoint") or "").strip()
    key_name = str(settings.get("auth_key_env") or settings.get("service_key_env") or "DATA_GO_KR_SERVICE_KEY")
    key = str((environ if env is None else env).get(key_name, "")).strip()
    if not endpoint or not key:
        missing = "endpoint" if not endpoint else key_name
        raise OfficialDataConfigurationError(f"{source} price API requires {missing}.")
    provider = OfficialJsonProvider(opener=opener)
    headers = {"AUTH_KEY": key} if source == "krx" else None
    params = {"serviceKey": key, "basDt": target_date.strftime("%Y%m%d"), "resultType": "json"} if source != "krx" else {"basDd": target_date.strftime("%Y%m%d")}
    payload = provider.request_json(OfficialRequestSettings(source=source, endpoint=endpoint, params=params, headers=headers))
    rows = []
    collected_at = _utcnow()
    for item in _extract_items(payload):
        symbol = str(_first(item, "srtnCd", "isuCd", "ISU_CD", "symbol", "종목코드") or "").zfill(6)
        if not symbol:
            continue
        rows.append(
            {
                "date": _parse_date(_first(item, "basDt", "trdDd", "date", "일자")) or target_date,
                "symbol": symbol,
                "market": _clean(_first(item, "mrktCtg", "mktNm", "market", "시장구분")),
                "open": _to_float(_first(item, "mkp", "open", "시가")),
                "high": _to_float(_first(item, "hipr", "high", "고가")),
                "low": _to_float(_first(item, "lopr", "low", "저가")),
                "close": _to_float(_first(item, "clpr", "close", "종가")),
                "volume": _to_float(_first(item, "trqu", "accTrdvol", "volume", "거래량")),
                "trading_value": _to_float(_first(item, "trPrc", "accTrdval", "trading_value", "거래대금")),
                "change_rate": _to_float(_first(item, "fltRt", "change_rate", "등락률")),
                "source": source,
                "raw_json": json.dumps(dict(item), ensure_ascii=False, allow_nan=False),
                "collected_at": collected_at,
            }
        )
    return pd.DataFrame(rows, columns=_empty_official_market_prices().columns)


def _extract_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates: list[Any] = [payload]
    for key in ("response", "body", "items", "item", "data", "list", "output", "OutBlock_1"):
        next_candidates = []
        for candidate in candidates:
            if isinstance(candidate, Mapping) and key in candidate:
                next_candidates.append(candidate[key])
        if next_candidates:
            candidates = next_candidates
    output: list[Mapping[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, list):
            output.extend(item for item in candidate if isinstance(item, Mapping))
        elif isinstance(candidate, Mapping):
            output.append(candidate)
    return output


def _first(item: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _hash(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _to_int(value: object) -> int | None:
    numeric = _to_float(value)
    return None if numeric is None else int(numeric)


def _parse_date(value: object) -> date | None:
    text = _clean(value)
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _parse_datetime(value: object) -> datetime | None:
    text = _clean(value)
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce", utc=True)
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().astimezone(UTC).replace(tzinfo=None)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _empty_bigkinds_issues() -> pd.DataFrame:
    return pd.DataFrame(columns=["issue_date", "period", "rank", "article_count", "title", "source", "raw_json", "collected_at"])


def _empty_global_news_events() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "event_id",
            "query",
            "title",
            "url",
            "domain",
            "source_country",
            "language",
            "seendate",
            "tone",
            "source",
            "raw_json",
            "collected_at",
        ]
    )


def _empty_dart_disclosures() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "disclosure_id",
            "symbol",
            "corp_code",
            "corp_name",
            "report_name",
            "receipt_no",
            "receipt_date",
            "submitter",
            "disclosure_url",
            "disclosure_type",
            "source",
            "raw_json",
            "collected_at",
        ]
    )


def _empty_official_market_prices() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "date",
            "symbol",
            "market",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trading_value",
            "change_rate",
            "source",
            "raw_json",
            "collected_at",
        ]
    )
