"""Official KRX index tickers for the research track.

pykrx names (do not swap codes):
- 1001 코스피
- 1034 코스피 100  (1028 is 코스피 200, never use as KOSPI100)
- 2001 코스닥
- KOSDAQ 100 is not in the current pykrx ticker list (KOSDAQ 150 is 2203).
  Do not substitute 2203 or reconstruct from current constituents.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OfficialIndex:
    code: str
    display_name: str
    korean_name: str
    pykrx_code: str | None
    flow_market: str
    substitute_codes: tuple[str, ...] = ()


OFFICIAL_INDEXES: dict[str, OfficialIndex] = {
    "KOSPI": OfficialIndex("KOSPI", "KOSPI", "코스피", "1001", "KOSPI"),
    "KOSDAQ": OfficialIndex("KOSDAQ", "KOSDAQ", "코스닥", "2001", "KOSDAQ"),
    "KOSPI100": OfficialIndex(
        "KOSPI100",
        "KOSPI 100",
        "코스피 100",
        "1034",
        "KOSPI",
        substitute_codes=("1028",),
    ),
    "KOSDAQ100": OfficialIndex(
        "KOSDAQ100",
        "KOSDAQ 100",
        "코스닥 100",
        None,
        "KOSDAQ",
        substitute_codes=("2203", "2002"),
    ),
}

RESEARCH_INDEXES = ("KOSPI", "KOSDAQ", "KOSPI100", "KOSDAQ100")
FORECAST_HORIZONS = (1, 5)


def get_official_index(index_code: str) -> OfficialIndex:
    try:
        return OFFICIAL_INDEXES[str(index_code).upper()]
    except KeyError as exc:
        raise KeyError(f"unsupported index_code={index_code}") from exc


def flow_market_for(index_code: str) -> str:
    return get_official_index(index_code).flow_market


def horizon_label(horizon_days: int) -> str:
    return f"{int(horizon_days)}D"
