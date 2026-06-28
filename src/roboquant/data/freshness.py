from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from roboquant.db import table_exists

KST = ZoneInfo("Asia/Seoul")
DEFAULT_PRICE_TABLE = "prices_daily"
DEFAULT_PRICE_COLUMN = "date"
DEFAULT_UNIVERSE_RULE = "prediction_top_market_cap"


@dataclass(frozen=True)
class FreshnessReport:
    status: str
    expected_latest_date: date
    latest_date: date | None
    age_days: int | None
    stale: bool
    reason: str | None
    messages: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "expected_latest_date": self.expected_latest_date.isoformat(),
            "latest_date": None if self.latest_date is None else self.latest_date.isoformat(),
            "age_days": self.age_days,
            "stale": self.stale,
            "reason": self.reason,
            "messages": list(self.messages),
        }


def local_today(now: datetime | None = None) -> date:
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    return current.astimezone(KST).date()


def expected_latest_trading_day(
    today: date | str | None = None,
    *,
    holidays: set[date] | None = None,
    now: datetime | None = None,
    daily_bar_ready_time: time = time(18, 0),
) -> date:
    if today is None:
        current = now or datetime.now(KST)
        if current.tzinfo is None:
            current = current.replace(tzinfo=KST)
        current = current.astimezone(KST)
        day = current.date()
        if current.time() < daily_bar_ready_time:
            day -= timedelta(days=1)
    else:
        day = _to_date(today) or local_today(now)
    holiday_set = holidays or set()
    while day.weekday() >= 5 or day in holiday_set:
        day -= timedelta(days=1)
    return day


def table_max_date(conn, table: str, column: str) -> date | None:
    if not table_exists(conn, table):
        return None
    value = conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
    return _to_date(value)


def price_freshness_report(
    conn,
    *,
    expected_date: date | str | None = None,
    max_stale_days: int = 0,
    table: str = DEFAULT_PRICE_TABLE,
    column: str = DEFAULT_PRICE_COLUMN,
) -> FreshnessReport:
    expected = expected_latest_trading_day(expected_date)
    latest = table_max_date(conn, table, column)
    if latest is None:
        return FreshnessReport(
            status="not_collected",
            expected_latest_date=expected,
            latest_date=None,
            age_days=None,
            stale=True,
            reason=f"{table}.{column} 데이터가 없습니다.",
            messages=[f"최신 가격 데이터가 없습니다. 기대 최신 거래일: {expected.isoformat()}"],
        )

    age_days = max(0, (expected - latest).days)
    stale = latest < expected - timedelta(days=max(0, int(max_stale_days)))
    if stale:
        reason = (
            f"가격 최신일 {latest.isoformat()}이 기대 최신 거래일 "
            f"{expected.isoformat()}보다 {age_days}일 늦습니다."
        )
        return FreshnessReport(
            status="partial_ready",
            expected_latest_date=expected,
            latest_date=latest,
            age_days=age_days,
            stale=True,
            reason=reason,
            messages=[f"최신 학습 미완료: {reason}"],
        )

    return FreshnessReport(
        status="ready",
        expected_latest_date=expected,
        latest_date=latest,
        age_days=age_days,
        stale=False,
        reason=None,
        messages=[f"가격 최신성 확인: {latest.isoformat()}"],
    )


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return pd.to_datetime(value).date()
