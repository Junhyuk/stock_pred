"""Shared exchange schedule with explicit, versioned public-holiday corrections."""
from __future__ import annotations

import warnings
from functools import lru_cache

import pandas as pd

CALENDAR_OVERRIDE_VERSION = 'krx_public_holidays_2026_v1'
# exchange_calendars 4.13.2 omits these national holidays. KRX equity policy:
# https://global.krx.co.kr/contents/GLB/06/0602/0602010201/GLB0602010201T1.jsp
# Election notice (KONEX-specific official notice; general holiday policy above):
# https://kind.krx.co.kr/external/2026/05/20/000110/20260520000197/32154.htm
EXCLUDED_SESSIONS = {'XKRX': frozenset({'2026-06-03', '2026-07-17'})}


@lru_cache(maxsize=32)
def _calendar(name: str, first_year: int, last_year: int):
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message="The 'generic' unit for NumPy timedelta is deprecated.*",
                                category=DeprecationWarning)
        import exchange_calendars as xcals
        return xcals.get_calendar(name, start=f'{first_year}-01-01', end=f'{last_year}-12-31')


def exchange_schedule(name: str, start, end):
    """Return one calendar and its corrected session labels for every consumer."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    calendar = _calendar(name, start.year - 1, end.year + 1)
    labels = calendar.sessions_in_range(start.date(), end.date())
    excluded = EXCLUDED_SESSIONS.get(name, frozenset())
    labels = labels[~labels.strftime('%Y-%m-%d').isin(excluded)]
    return calendar, labels
