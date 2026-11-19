"""Official monthly first-release inputs for next-session forecast research."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pandas as pd

from roboquant.index_forecast.accuracy_research import _number, _timestamp


@dataclass(frozen=True)
class SeriesSpec:
    source_id: str
    hosts: tuple[str, ...]
    unit: str = "percent"
    adjustment: str = "SA"


BLS = ("bls.gov", "www.bls.gov")
FED = ("federalreserve.gov", "www.federalreserve.gov")
MODS = ("mods.go.kr", "www.mods.go.kr", "kostat.go.kr", "www.kostat.go.kr")
CENSUS = ("census.gov", "www.census.gov")
CUSTOMS = ("customs.go.kr", "www.customs.go.kr")
SPECS = {
    ("US", "cpi_headline_mom"): SeriesSpec("bls_cpi_first_release", BLS),
    ("US", "unemployment_rate"): SeriesSpec("bls_employment_first_release", BLS),
    ("US", "industrial_production_mom"): SeriesSpec("fed_g17_initial", FED),
    ("US", "exports_usd_million"): SeriesSpec("census_ft900_first_release", CENSUS, "usd_million"),
    ("US", "imports_usd_million"): SeriesSpec("census_ft900_first_release", CENSUS, "usd_million"),
    ("KR", "cpi_headline_yoy"): SeriesSpec("mods_cpi_first_release", MODS, adjustment="NSA"),
    ("KR", "unemployment_rate"): SeriesSpec("mods_employment_first_release", MODS),
    ("KR", "industrial_production_mom"): SeriesSpec("mods_industry_first_release", MODS),
    ("KR", "exports_usd_million"): SeriesSpec("korea_customs_monthly_first", CUSTOMS, "usd_million", "NSA"),
    ("KR", "imports_usd_million"): SeriesSpec("korea_customs_monthly_first", CUSTOMS, "usd_million", "NSA"),
}
TIMEZONES = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}
COLUMNS = {"country", "measure", "reference_period", "value", "unit", "seasonal_adjustment",
           "vintage", "source_id", "source_url", "release_date", "received_at"}


@dataclass(frozen=True)
class Release:
    period: pd.Period
    release_date: date
    publication: pd.Timestamp
    received: pd.Timestamp
    value: float
    source_url: str
    source_id: str
    unit: str
    release_clock: str

    def available_at(self, prospective: bool) -> pd.Timestamp:
        return max(self.publication, self.received) if prospective else self.publication


@dataclass(frozen=True)
class MacroReleaseInputs:
    rows: dict[str, dict[pd.Period, Release]]
    evidence: dict

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(sorted(self.rows))

    def features_at(self, family: str, cutoff: pd.Timestamp, *, prospective: bool) -> dict | None:
        eligible = {period: row for period, row in self.rows[family].items()
                    if row.available_at(prospective) <= cutoff}
        if not eligible:
            return None
        period = max(eligible)
        current = eligible[period]
        if cutoff > current.publication + timedelta(days=62):
            return None
        if cutoff.date() - period.start_time.date() > timedelta(days=100):
            return None
        # Compare trade values to the same months one year earlier.
        periods = (period, period - 1)
        if current.unit == "usd_million":
            periods += (period - 12, period - 13)
        if any(period not in eligible for period in periods):
            return None
        rows = [eligible[period] for period in periods]
        level, previous = (row.value for row in rows[:2])
        if len(rows) == 4:
            level = 100 * (level / rows[2].value - 1)
            previous = 100 * (previous / rows[3].value - 1)
        return {
            "features": {"release_level": level, "release_change_pp": level - previous},
            "reference_periods": sorted(str(row.period) for row in rows),
            "latest_reference_period": str(current.period), "latest_value": current.value,
            "unit": current.unit, "sources": sorted({row.source_id for row in rows}),
            "latest_source_url": current.source_url,
            "latest_source_release_date": current.release_date.isoformat(),
            "observation_dates": sorted({row.release_date.isoformat() for row in rows}),
            "available_at": max(row.available_at(prospective) for row in rows).isoformat(),
            "snapshot_received_at": max(row.received for row in rows).isoformat(),
            "availability_policy": "confirmed_receipt" if prospective else "official_first_release_archive_replay",
            "release_clocks": sorted({row.release_clock for row in rows}),
            "source_finality": "supplied_first_release_vintage",
        }


def _validated(row: dict, now: pd.Timestamp) -> tuple[str, Release]:
    if row["vintage"] != "first_release":
        raise ValueError("non_first_release")
    key = row["country"], row["measure"]
    spec = SPECS.get(key)
    url = urlsplit(str(row["source_url"]))
    if (spec is None or row["source_id"] != spec.source_id or url.scheme != "https" or url.username
            or url.hostname not in spec.hosts or row["unit"] != spec.unit
            or row["seasonal_adjustment"] != spec.adjustment):
        raise ValueError("unapproved_source_or_definition")
    value = _number(row["value"])
    if value is None or (spec.unit == "usd_million" and value <= 0):
        raise ValueError("invalid_value")
    received = _timestamp(row["received_at"])
    if received is None or received > now:
        raise ValueError("missing_or_future_receipt")
    try:
        period = pd.Period(row["reference_period"], freq="M")
        released = date.fromisoformat(row["release_date"])
        if str(period) != row["reference_period"] or released.isoformat() != row["release_date"]:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_reference_or_release_date") from exc
    zone = TIMEZONES[key[0]]
    if released < (period + 1).start_time.date() or received.tz_convert(zone).date() < released:
        raise ValueError("invalid_release_chronology")
    raw = row.get("actual_release_at")
    if pd.isna(raw) or raw == "":
        end = datetime.combine(released + timedelta(days=1), time.min, tzinfo=zone)
        publication = pd.Timestamp(end).tz_convert("UTC")
        release_clock = "next_source_day_start_assumption"
    else:
        publication = _timestamp(raw)
        if publication is None or publication > received or publication.tz_convert(zone).date() != released:
            raise ValueError("invalid_actual_release_clock")
        release_clock = "source_declared_timestamp"
    family = f"{key[0].lower()}_{key[1]}"
    return family, Release(period, released, publication, received, value, str(row["source_url"]),
                           spec.source_id, spec.unit, release_clock)


def prepare_macro_releases(releases: pd.DataFrame, *, as_of) -> MacroReleaseInputs:
    """Normalize authorized archives and quarantine conflicting first vintages."""
    now = _timestamp(as_of)
    if now is None:
        raise ValueError("Macro research cutoff requires an explicit timezone")
    missing = COLUMNS - set(releases.columns)
    if missing and not releases.empty:
        raise ValueError(f"Missing macro release fields: {', '.join(sorted(missing))}")
    groups = defaultdict(list)
    rejected = Counter()
    for raw in releases.to_dict("records"):
        try:
            family, release = _validated(raw, now)
        except ValueError as exc:
            rejected[str(exc)] += 1
        else:
            groups[family, release.period].append(release)
    rows = defaultdict(dict)
    for (family, period), versions in groups.items():
        if len({(item.value, item.release_date) for item in versions}) != 1:
            rejected["conflicting_period"] += 1
            continue
        rows[family][period] = min(versions, key=lambda item: (item.received, item.publication))
    evidence = {family: {
        "periods": len(values), "first_period": str(min(values)), "last_period": str(max(values)),
        "missing_periods": max(values).ordinal - min(values).ordinal + 1 - len(values),
        "release_clocks": dict(Counter(item.release_clock for item in values.values())),
    } for family, values in sorted(rows.items())}
    return MacroReleaseInputs(dict(rows), {"series": evidence, "rejections": dict(rejected),
                                           "input_validation": "curated_archive_metadata_and_cutoff_checks",
                                           "scope": "research_only"})
