from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any

import pandas as pd

CREDIT_BALANCE_COLUMNS = [
    "date",
    "market",
    "credit_loan_balance_krw",
    "credit_loan_delta_1d_krw",
    "credit_loan_delta_5d_krw",
    "credit_loan_delta_20d_krw",
    "credit_to_market_cap",
    "source",
    "collected_at",
]
VALID_MARKETS = {"KOSPI", "KOSDAQ", "ALL"}


class MissingCreditBalanceConfig(RuntimeError):
    pass


def fetch_market_credit_balance(
    target_date: str,
    config: dict[str, Any] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> pd.DataFrame:
    cfg = (config or {}).get("market_credit_balance", {})
    environment = env if env is not None else os.environ
    service_key = str(cfg.get("service_key") or environment.get("DATA_GO_KR_SERVICE_KEY") or "").strip()
    endpoint = str(
        cfg.get("endpoint")
        or environment.get("KOFIA_CREDIT_BALANCE_ENDPOINT")
        or environment.get("DATA_GO_KR_KOFIA_CREDIT_ENDPOINT")
        or ""
    ).strip()
    if not service_key:
        raise MissingCreditBalanceConfig("DATA_GO_KR_SERVICE_KEY is not configured")
    if not endpoint:
        raise MissingCreditBalanceConfig("KOFIA/data.go.kr credit balance endpoint is not configured")

    payload = _fetch_payload(endpoint, service_key=service_key, target_date=target_date, config=cfg)
    return normalize_market_credit_balance(payload, source=str(cfg.get("source") or "data_go_kr_kofia"))


def normalize_market_credit_balance(payload: Any, *, source: str = "data_go_kr_kofia") -> pd.DataFrame:
    records = _extract_records(payload)
    if not records:
        return _empty_credit_balance()
    frame = pd.DataFrame(records)
    output = pd.DataFrame(
        {
            "date": _first_available(frame, ["date", "basDt", "trdDd", "기준일", "일자"]),
            "market": _first_available(frame, ["market", "mrktCtg", "시장", "시장구분"]),
            "credit_loan_balance_krw": _first_available(
                frame,
                [
                    "credit_loan_balance_krw",
                    "crdLoanBal",
                    "loanBalance",
                    "융자잔고",
                    "신용융자잔고",
                    "신용거래융자",
                ],
            ),
            "credit_loan_delta_1d_krw": _first_available(
                frame,
                ["credit_loan_delta_1d_krw", "crdLoanBalDelta1d", "전일대비", "증감"],
            ),
            "credit_loan_delta_5d_krw": _first_available(
                frame,
                ["credit_loan_delta_5d_krw", "crdLoanBalDelta5d"],
            ),
            "credit_loan_delta_20d_krw": _first_available(
                frame,
                ["credit_loan_delta_20d_krw", "crdLoanBalDelta20d"],
            ),
            "credit_to_market_cap": _first_available(
                frame,
                ["credit_to_market_cap", "creditToMarketCap", "시총대비"],
            ),
        }
    )
    output["date"] = pd.to_datetime(output["date"], errors="coerce").dt.date
    output["market"] = output["market"].map(_normalize_market)
    for column in [
        "credit_loan_balance_krw",
        "credit_loan_delta_1d_krw",
        "credit_loan_delta_5d_krw",
        "credit_loan_delta_20d_krw",
        "credit_to_market_cap",
    ]:
        output[column] = output[column].map(_coerce_number)
    output = output.dropna(subset=["date", "market"])
    output = output[output["market"].isin(VALID_MARKETS)].copy()
    if output.empty:
        return _empty_credit_balance()
    output = output.sort_values(["market", "date"])
    for window, column in [
        (1, "credit_loan_delta_1d_krw"),
        (5, "credit_loan_delta_5d_krw"),
        (20, "credit_loan_delta_20d_krw"),
    ]:
        missing = output[column].isna()
        computed = output.groupby("market")["credit_loan_balance_krw"].diff(window)
        output.loc[missing, column] = computed[missing]
    output["source"] = source
    output["collected_at"] = datetime.now(UTC).replace(tzinfo=None)
    return output[CREDIT_BALANCE_COLUMNS].drop_duplicates(["date", "market"], keep="last")


def _fetch_payload(
    endpoint: str,
    *,
    service_key: str,
    target_date: str,
    config: dict[str, Any],
) -> Any:
    params = dict(config.get("params") or {})
    params.setdefault("serviceKey", service_key)
    params.setdefault("resultType", "json")
    params.setdefault("basDt", str(target_date).replace("-", ""))
    url = endpoint
    delimiter = "&" if "?" in url else "?"
    url = f"{url}{delimiter}{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=float(config.get("timeout_seconds", 20))) as response:
        raw = response.read()
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, pd.DataFrame):
        return payload.to_dict(orient="records")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        if text.startswith("<"):
            return _extract_xml_records(text)
        try:
            return _extract_records(json.loads(text))
        except json.JSONDecodeError:
            return []
    if isinstance(payload, dict):
        for key in ("items", "item", "data", "records", "result"):
            value = payload.get(key)
            records = _extract_records(value)
            if records:
                return records
        for key in ("response", "body"):
            value = payload.get(key)
            records = _extract_records(value)
            if records:
                return records
        return [payload] if payload else []
    return []


def _extract_xml_records(text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    records = []
    for item in root.findall(".//item"):
        records.append({child.tag: child.text for child in item})
    return records


def _first_available(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    for column in columns:
        if column in frame.columns:
            return frame[column]
    return pd.Series(pd.NA, index=frame.index)


def _normalize_market(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"KOSPI", "KS", "STK", "유가증권", "거래소"}:
        return "KOSPI"
    if text in {"KOSDAQ", "KQ", "코스닥"}:
        return "KOSDAQ"
    if text in {"ALL", "TOTAL", "전체", "합계"}:
        return "ALL"
    return text if text in VALID_MARKETS else None


def _coerce_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _empty_credit_balance() -> pd.DataFrame:
    return pd.DataFrame(columns=CREDIT_BALANCE_COLUMNS)
