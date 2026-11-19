"""Read current KOFIA credit and reconcile optional official market details.

The FreeSIS homepage publishes a month/day only. This collector is therefore
for current-day collection, not a historical replay. A caller must compare the
actual receipt against its own forecast cutoff before using any observation.
"""

from __future__ import annotations

import json
import re
import warnings
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
FREESIS_URL = "https://freesis.kofia.or.kr/stat/main.do"
FSC_URL = "https://apis.data.go.kr/1160100/service/GetKofiaStatisticsInfoService/getGrantingOfCreditBalanceInfo"
TIMEOUT_SECONDS = 7
MAX_BYTES = 1_000_000


class CreditSourceError(RuntimeError):
    """The official snapshot is missing, ambiguous, stale or malformed."""


class CreditCollectionWarning(UserWarning):
    """A market split was not confirmed; the national observation is retained."""


def _now_utc() -> datetime:
    return datetime.now(UTC)


class _SummaryParser(HTMLParser):
    """Recognize the one labeled FreeSIS summary card, not other money fields."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.current = None
        self.capture = None
        self.text = []
        self.matches = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "dl":
            if self.depth == 0:
                self.current = {"labels": [], "fields": {}}
            self.depth += 1
        elif (
            self.depth
            and tag == "a"
            and re.search(r"clickJisuMenu\(\s*['\"]OS0026['\"]\s*\)", attrs.get("onclick", ""))
        ):
            self.capture, self.text = "label", []
        elif self.depth and tag == "span":
            names = set(attrs.get("class", "").split()) & {"dan", "date", "num1"}
            if len(names) == 1:
                self.capture, self.text = names.pop(), []

    def handle_data(self, data):
        if self.capture:
            self.text.append(data)

    def handle_endtag(self, tag):
        if (
            self.depth
            and self.capture
            and (
                (tag == "a" and self.capture == "label")
                or (tag == "span" and self.capture != "label")
            )
        ):
            value = "".join(self.text).strip()
            if self.capture == "label":
                self.current["labels"].append(value)
            else:
                self.current["fields"].setdefault(self.capture, []).append(value)
            self.capture, self.text = None, []
        if tag == "dl" and self.depth:
            self.depth -= 1
            if not self.depth and self.current["labels"]:
                self.matches.append(self.current)
                self.current = None


def _read(url: str) -> tuple[bytes, datetime]:
    request = Request(
        url,
        headers={
            "User-Agent": "roboquant-credit-research/1.0",
            "Accept": "text/html, application/json",
        },
    )
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        content = response.read(MAX_BYTES + 1)
    received = _now_utc()
    if len(content) > MAX_BYTES:
        raise CreditSourceError("Official response exceeds size limit")
    return content, received


def _summary(content: bytes, *, as_of: datetime, received: datetime) -> dict:
    try:
        parser = _SummaryParser()
        parser.feed(content.decode("utf-8", errors="strict"))
        if len(parser.matches) != 1:
            raise ValueError
        card = parser.matches[0]
        fields = card["fields"]
        if card["labels"] != ["신용융자"] or set(fields) != {"dan", "date", "num1"}:
            raise ValueError
        if any(len(fields[key]) != 1 for key in fields) or fields["dan"][0] != "백만원":
            raise ValueError
        amount, date_text = fields["num1"][0], fields["date"][0]
        if not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)", amount):
            raise ValueError
        if not re.fullmatch(r"\d{2}/\d{2}", date_text):
            raise ValueError
        local_now = received.astimezone(KST).date()
        local_asof = as_of.astimezone(KST).date()
        if local_now != local_asof:
            raise ValueError
        month, day = map(int, date_text.split("/"))
        observation = date(local_asof.year, month, day)
        if observation > local_asof:
            observation = date(local_asof.year - 1, month, day)
        if local_asof - observation > timedelta(days=10):
            raise ValueError
        value = int(amount.replace(",", "")) * 1_000_000
        if value <= 0:
            raise ValueError
    except (ValueError, UnicodeError, KeyError, TypeError, OverflowError) as exc:
        raise CreditSourceError(
            "FreeSIS summary lacks a unique, current, valid KRW-million credit observation"
        ) from exc
    return _row("GLOBAL", observation, value, "kofia_freesis_margin_credit", received)


def _row(market: str, observation: date, value: int, source: str, received: datetime) -> dict:
    return {
        "market": market,
        "series": "MARGIN_CREDIT_BALANCE",
        "observation_date": observation.isoformat(),
        "value": value,
        "unit": "KRW",
        "source": source,
        "received_at": received.astimezone(UTC).isoformat(),
        "published_at": None,
        "is_final": None,
        "availability_policy": "observed",
    }


def _nonnegative_integer(value) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number != number.to_integral_value():
            raise ValueError
        return int(number)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError from exc


def _split(content: bytes, global_row: dict, received: datetime) -> list[dict]:
    body = json.loads(content.decode("utf-8", errors="strict"))["response"]
    if body["header"]["resultCode"] not in ("00", "0000"):
        raise ValueError("FSC response did not indicate success")
    body = body["body"]
    if _nonnegative_integer(body["totalCount"]) != 1:
        raise ValueError("FSC result is not unique")
    items = body["items"]["item"]
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("FSC result is not unique")
    item = items[0]
    day = datetime.strptime(str(item["basDt"]), "%Y%m%d").date()
    if day.isoformat() != global_row["observation_date"]:
        raise ValueError("FSC result date differs from FreeSIS")
    total = _nonnegative_integer(item["crdTrFingWhl"])
    kospi = _nonnegative_integer(item["crdTrFingScrs"])
    kosdaq = _nonnegative_integer(item["crdTrFingKosdaq"])
    if not total or abs(total - kospi - kosdaq) > 1:
        raise ValueError("FSC market sum does not reconcile at source precision")
    factors = [
        factor
        for factor in (1, 1000, 1_000_000, 1_000_000_000)
        if total * factor == global_row["value"]
    ]
    if len(factors) != 1:
        raise ValueError("FSC amount unit does not reconcile with FreeSIS")
    factor = factors[0]
    return [
        _row("KOSPI", day, kospi * factor, "fsc_kofia_margin_credit", received),
        _row("KOSDAQ", day, kosdaq * factor, "fsc_kofia_margin_credit", received),
    ]


def collect_credit_balances(*, as_of: datetime, api_key: str | None = None) -> list[dict]:
    """Return a current observed total, plus independently reconciled official split.

    A supplied key enables the documented FSC endpoint; failure, unknown units,
    mismatched dates or totals preserve only GLOBAL and emit a sanitized warning.
    This function does not assert original publication time or finality.
    """
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of requires an explicit timezone")
    try:
        content, received = _read(FREESIS_URL)
        total = _summary(content, as_of=as_of, received=received)
    except CreditSourceError:
        raise
    except Exception as exc:
        raise CreditSourceError("FreeSIS summary could not be fetched") from exc
    rows = [total]
    if api_key:
        params = {
            "serviceKey": unquote(api_key.strip()),
            "resultType": "json",
            "basDt": total["observation_date"].replace("-", ""),
            "numOfRows": 2,
            "pageNo": 1,
        }
        try:
            raw, api_received = _read(FSC_URL + "?" + urlencode(params))
            rows.extend(_split(raw, total, api_received))
        except Exception:
            warnings.warn(
                "Official market split unavailable or did not reconcile; only GLOBAL is returned",
                CreditCollectionWarning,
                stacklevel=2,
            )
    return rows
