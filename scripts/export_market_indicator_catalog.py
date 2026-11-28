#!/usr/bin/env python3
"""Consolidate reviewed public market artifacts; never read the private cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            name = stream.name
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


COUNTRIES = {"KR": "한국", "US": "미국", "JP": "일본", "EA": "유로 지역"}
MEASURES = {
    "policy_rate": "정책금리",
    "policy_rate_lower": "정책금리 목표 하한",
    "policy_rate_upper": "정책금리 목표 상한",
    "unemployment_rate": "실업률",
    "employment_yoy_change": "취업자 전년동월 증감",
    "total_nonfarm_employment": "비농업 고용자 수",
    "nfp_monthly_change": "비농업 고용 전월 증감",
    "cpi_core_yoy": "근원 소비자물가 전년동월비",
    "cpi_core_mom": "근원 소비자물가 전월비",
    "cpi_headline_yoy": "소비자물가 전년동월비",
    "cpi_headline_mom": "소비자물가 전월비",
    "core_pce_yoy": "근원 PCE 물가 전년동월비",
    "core_pce_mom": "근원 PCE 물가 전월비",
    "pce_headline_yoy": "PCE 물가 전년동월비",
    "pce_headline_mom": "PCE 물가 전월비",
}
ADJUSTMENT = {"SA": "계절조정", "NSA": "원계열", "not_applicable": "", None: ""}


def read(directory, name):
    path = directory / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def valid_date(value):
    try:
        return (
            isinstance(value, str)
            and len(value) == 10
            and date.fromisoformat(value).isoformat() == value
        )
    except ValueError:
        return False


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def points(rows):
    result = {}
    for day, value in rows:
        if valid_date(day) and number(value):
            result[day] = value
    return [[day, result[day]] for day in sorted(result)]


def category(key, item):
    if item.get("date_basis") in {"reference_month", "effective_date"} or "policy_rate" in key:
        return "경제지표"
    if key == "margin_credit_total":
        return "신용·CFD"
    if key.startswith(("kr_", "us_", "jp_")) and key.endswith(("1y", "3y", "5y", "10y")):
        return "금리"
    if key.startswith(("kospi", "kosdaq")):
        return "국내 지수"
    if key in {"sp500", "nasdaq", "sox", "es_futures", "nq_futures", "vix"}:
        return "해외 지수"
    if key in {"usdkrw", "dxy", "wti", "brent", "gold", "copper", "btc", "eth"}:
        return "환율·원자재"
    return "기타"


def approved_market(key, item):
    label = str(item.get("label") or "").upper()
    return (
        key not in {"ewy", "koru"}
        and "ETF" not in label
        and "주식" not in label
        and category(key, item) != "기타"
    )


def build_catalog(directory, now=None):
    directory = Path(directory)
    now = now or datetime.now(UTC).isoformat()
    stamp = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("generated_at must include an offset")
    result = {
        "schema_version": "market_indicator_catalog_v1",
        "generated_at": stamp.astimezone(UTC).isoformat(),
        "series": [],
        "events": [],
        "releases": [],
        "model_links": [],
    }
    by_id = {}

    def add(
        id,
        label,
        group,
        unit,
        source,
        source_url,
        date_basis,
        raw_points,
        *,
        note=None,
        status="unknown",
        updated_at=None,
        chart_kind="line",
        **extra,
    ):
        if id in by_id:
            raise ValueError(f"duplicate series id: {id}")
        raw_points = list(raw_points)
        present = {day for day, value in raw_points if valid_date(day) and number(value)}
        missing = sorted(
            {
                day
                for day, value in raw_points
                if valid_date(day) and value is None and day not in present
            }
        )
        s = {
            "id": id,
            "label": label,
            "category": group,
            "unit": unit,
            "source": source,
            "source_url": source_url,
            "date_basis": date_basis,
            "note": note,
            "status": status,
            "updated_at": updated_at,
            "chart_kind": chart_kind,
            "points": points(raw_points),
            "missing_dates": missing,
        }
        s.update(extra)
        by_id[id] = s
        result["series"].append(s)

    history = read(directory, "market-chart-history.json")
    for key, s in history.get("series", {}).items():
        if not approved_market(key, s):
            continue
        add(
            key,
            s.get("label", key),
            category(key, s),
            s.get("unit"),
            s.get("source"),
            s.get("source_url"),
            s.get("date_basis"),
            s.get("points", []),
            note=s.get("note"),
            chart_kind="points" if key == "margin_credit_total" else "line",
            release_dates=s.get("release_dates", {}),
            release_urls=s.get("release_urls", {}),
        )

    # Match the website's return5/return20 guard: each intervening daily
    # return must be present, which prevents bridging unverified sessions.
    for market in ("kospi", "kosdaq"):
        close = by_id.get(market)
        daily = by_id.get(market + "_return")
        if not close or not daily:
            continue
        observations = close["points"]
        verified = {day for day, _ in daily["points"]}
        for horizon in (5, 20):
            sid = f"{market}_return_{horizon}d"
            if sid in by_id:
                continue
            derived = []
            for index in range(horizon, len(observations)):
                window = observations[index - horizon : index + 1]
                if all(value > 0 for _, value in window) and all(
                    day in verified for day, _ in window[1:]
                ):
                    derived.append([window[-1][0], (window[-1][1] / window[0][1] - 1) * 100])
                else:
                    derived.append([window[-1][0], None])
            add(
                sid,
                f"{market.upper()} {horizon}거래일 수익률",
                "국내 지수",
                "percent",
                close["source"],
                close["source_url"],
                "session_date",
                derived,
                note=f"검증된 연속 {horizon}거래일 종가 수익률입니다. 확인되지 않은 거래일은 건너뜁니다.",
            )

    flow = read(directory, "kospi_investor_flow_chart.json")
    for market, data in flow.get("markets", {}).items():
        for name, field, label in [
            ("foreign", "foreign_eok", "외국인"),
            ("institution", "institution_eok", "기관"),
            ("personal", "personal_eok", "개인"),
        ]:
            add(
                f"flow_{market.lower()}_{name}",
                f"{market} {label} 순매수",
                "수급",
                flow.get("unit"),
                flow.get("source"),
                flow.get("source_url"),
                "session_date",
                [(p.get("date"), p.get(field)) for p in data.get("sessions", [])],
                note="비공식 수급 관측치. 누락 거래일은 보간하지 않습니다.",
                updated_at=flow.get("snapshot_received_at"),
                chart_kind="bar",
            )

    margin = read(directory, "margin_credit_chart.json")
    for market, rows in margin.get("markets", {}).items():
        add(
            f"margin_{market.lower()}",
            f"{market} 신용융자 잔고",
            "신용·CFD",
            margin.get("unit"),
            margin.get("source"),
            margin.get("source_url"),
            "observation_date",
            [(p.get("date"), p.get("balance_krw")) for p in rows],
            updated_at=margin.get("snapshot_received_at"),
        )

    cfd_status = read(directory, "cfd_status.json")
    disclosed = read(directory, "cfd_market_history.json")
    disclosed_source = disclosed.get("source")
    if isinstance(disclosed_source, dict):
        disclosed_url = disclosed_source.get("url")
        disclosed_source = disclosed_source.get("label")
    else:
        disclosed_url = "https://www.kofia.or.kr/brd/m_198/list.do"
    for market, rows in disclosed.get("markets", {}).items():
        add(
            f"cfd_disclosed_{market.lower()}",
            f"{market} CFD 공시 종목 잔고",
            "신용·CFD",
            disclosed.get("unit"),
            disclosed_source,
            disclosed_url,
            "observation_date",
            [(p.get("date"), p.get("value_krw")) for p in rows],
            note="공시된 국내 종목 합계이며 전체 CFD 잔고가 아닙니다.",
            status=cfd_status.get("data_status", "unknown"),
            release_dates={
                p["date"]: p["posted_date"] for p in rows if p.get("date") and p.get("posted_date")
            },
            release_urls={
                p["date"]: p["source_url"] for p in rows if p.get("date") and p.get("source_url")
            },
        )

    cfd = read(directory, "cfd_frees_is_history.json")
    fields = [
        ("total", "전체"),
        ("buy", "매수"),
        ("sell", "매도"),
        ("kospi", "KOSPI"),
        ("kospi_buy", "KOSPI 매수"),
        ("kospi_sell", "KOSPI 매도"),
        ("kosdaq", "KOSDAQ"),
        ("kosdaq_buy", "KOSDAQ 매수"),
        ("kosdaq_sell", "KOSDAQ 매도"),
    ]
    for kind, rows in cfd.get("series", {}).items():
        if kind not in ("with_margin", "without_margin"):
            continue
        for field, label in fields:
            add(
                f"cfd_{kind}_{field}",
                f"CFD {'증거금 포함' if kind == 'with_margin' else '증거금 제외'} {label}",
                "신용·CFD",
                cfd.get("unit"),
                cfd.get("source"),
                cfd_status.get("sources", {}).get("total_" + kind),
                "observation_date",
                [(p.get("date"), p.get(field + "_krw")) for p in rows],
                note="전체 보고 잔고로 해외분을 포함합니다.",
                status=cfd_status.get("frees_is_series_status", {}).get(kind, "unknown"),
                updated_at=cfd.get("series_received_at", {}).get(kind),
            )

    # Only preserve our own previously exported snapshots, and only when the
    # current source and unit still match. This avoids mixing definitions.
    prior = {
        s.get("id"): s
        for s in read(directory, "market-indicator-catalog.json").get("series", [])
        if s.get("id", "").startswith("snapshot_")
    }
    snapshot = read(directory, "unofficial_numerics.json")
    seen_snapshot_definitions = {}
    for s in snapshot.get("series", []):
        key = s.get("id")
        if (
            not isinstance(key, str)
            or not approved_market(key, s)
            or key.startswith(
                (
                    "kospi_foreign",
                    "kospi_institutional",
                    "kospi_personal",
                    "kosdaq_foreign",
                    "kosdaq_institutional",
                    "kosdaq_personal",
                )
            )
        ):
            continue
        sid = "snapshot_" + key
        seen_snapshot_definitions[sid] = (s.get("source"), s.get("unit"))
        if not valid_date(s.get("date")) or not number(s.get("value")):
            continue
        old = prior.get(sid, {})
        old_points = (
            old.get("points", [])
            if old.get("unit") == s.get("unit") and old.get("source") == s.get("source")
            else []
        )
        add(
            sid,
            f"{s.get('label', key)} · 수집 시점 관측",
            category(key, s),
            s.get("unit"),
            s.get("source"),
            s.get("source_url"),
            "observation_date",
            old_points + [[s.get("date"), s.get("value")]],
            note="관측된 스냅샷만 표시합니다. 과거 미관측일은 누락되어 있습니다.",
            updated_at=snapshot.get("received_at"),
            chart_kind="points",
        )

    for sid, old in prior.items():
        key = sid.removeprefix("snapshot_")
        definition = (old.get("source"), old.get("unit"))
        current = seen_snapshot_definitions.get(sid)
        if (
            sid in by_id
            or not approved_market(key, old)
            or not all(definition)
            or (current is not None and current != definition)
        ):
            continue
        add(
            sid,
            old.get("label", key),
            old.get("category", category(key, old)),
            old.get("unit"),
            old.get("source"),
            old.get("source_url"),
            "observation_date",
            old.get("points", []),
            note="이번 수집에서 관측되지 않아 이전 스냅샷을 보존합니다.",
            status="stale",
            updated_at=old.get("updated_at"),
            chart_kind="points",
        )

    official = read(directory, "market_indicators_status.json")
    approved_sources = {
        "kofia_freesis_margin_credit",
        "fsc_kofia_margin_credit",
        "bok_ecos_817Y002",
        "kofia_freesis_bond_yield",
        "us_treasury_daily_par_yield_curve",
    }
    prior_official = {
        s.get("id"): s
        for s in read(directory, "market-indicator-catalog.json").get("series", [])
        if s.get("id", "").startswith("official_")
    }
    seen_official_definitions = {}
    for row in official.get("observations", []):
        source = row.get("source")
        key = row.get("series", "")
        market = row.get("market", "")
        if source not in approved_sources or (
            key != "MARGIN_CREDIT_BALANCE" and not key.startswith(("KR_YIELD_", "US_YIELD_"))
        ):
            continue
        sid = f"official_{key.lower()}_{market.lower()}"
        seen_official_definitions[sid] = (source, row.get("unit"))
        if not valid_date(row.get("observation_date")) or not number(row.get("value")):
            continue
        old = next(
            (
                s
                for s in read(directory, "market-indicator-catalog.json").get("series", [])
                if s.get("id") == sid
                and s.get("source") == source
                and s.get("unit") == row.get("unit")
            ),
            {},
        )
        group = "credit" if key == "MARGIN_CREDIT_BALANCE" else key[:2].lower() + "_yields"
        state = official.get("sources", {}).get(group, {}).get("status", "unknown")
        label = (
            f"{market if market != 'GLOBAL' else '전체'} 신용융자 잔고"
            if group == "credit"
            else f"{COUNTRIES.get(key[:2], key[:2])} 국채 {key.rsplit('_', 1)[-1].lower().replace('y', '년')} 금리"
        )
        add(
            sid,
            label + " · 수집 시점 관측",
            "신용·CFD" if group == "credit" else "금리",
            row.get("unit"),
            source,
            None,
            "observation_date",
            old.get("points", []) + [[row.get("observation_date"), row.get("value")]],
            note="공식 출처의 보존된 관측치입니다. 출처를 확인할 수 없는 날짜는 표시하지 않습니다.",
            status=state,
            updated_at=row.get("received_at"),
            chart_kind="points",
        )

    for sid, old in prior_official.items():
        definition = (old.get("source"), old.get("unit"))
        current = seen_official_definitions.get(sid)
        if (
            sid in by_id
            or old.get("source") not in approved_sources
            or not all(definition)
            or not sid.startswith(
                ("official_margin_credit_balance_", "official_kr_yield_", "official_us_yield_")
            )
            or (current is not None and current != definition)
        ):
            continue
        add(
            sid,
            old.get("label", sid),
            old.get("category"),
            old.get("unit"),
            old.get("source"),
            old.get("source_url"),
            "observation_date",
            old.get("points", []),
            note="이번 수집에서 관측되지 않아 이전 공식 관측치를 보존합니다.",
            status="stale",
            updated_at=old.get("updated_at"),
            chart_kind="points",
        )

    economic = read(directory, "economic_history.json")
    calendar = read(directory, "economic_calendar.json")
    events = {}
    for event in economic.get("event_versions", []) + calendar.get("events", []):
        eid = event.get("event_id")
        if not eid:
            continue
        previous = events.get(eid)
        if previous is None or (event.get("available_at") or "") >= (
            previous.get("available_at") or ""
        ):
            events[eid] = event
    fields = (
        "title",
        "country",
        "event_type",
        "scheduled_date",
        "scheduled_at",
        "time_precision",
        "timezone",
        "status",
        "reference_period",
        "source_url",
        "available_at",
    )
    for eid, event in sorted(events.items()):
        result["events"].append(
            {"id": eid, **{k: event.get(k) for k in fields}, "source": event.get("source_id")}
        )
    seen = set()
    fields = (
        "country",
        "measure",
        "reference_period",
        "value",
        "unit",
        "vintage",
        "revision_of",
        "actual_release_date",
        "actual_release_at",
        "available_at",
        "source_url",
    )
    for row in economic.get("release_versions", []) + calendar.get("recent_releases", []):
        rid = row.get("version_id")
        if not rid or rid in seen or not number(row.get("value")):
            continue
        seen.add(rid)
        result["releases"].append(
            {"id": rid, **{k: row.get(k) for k in fields}, "source": row.get("source_id")}
        )

    # A series may contain revisions: choose the most recently received version
    # for each reference period, while retaining every version in releases.
    grouped = {}
    for row in economic.get("release_versions", []) + calendar.get("recent_releases", []):
        reference = row.get("reference_period") or ""
        day = reference + "-01" if len(reference) == 7 else reference
        if not valid_date(day) or not number(row.get("value")):
            continue
        identity = (
            row.get("country"),
            row.get("source_id"),
            row.get("measure"),
            row.get("definition_version"),
            row.get("seasonal_adjustment"),
            row.get("unit"),
        )
        if not all(identity[i] for i in (0, 1, 2, 5)):
            continue
        rows = grouped.setdefault(identity, {})
        old = rows.get(day)
        if old is None or (row.get("available_at") or "", row.get("version_id") or "") > (
            old.get("available_at") or "",
            old.get("version_id") or "",
        ):
            rows[day] = row
    for identity, rows in sorted(grouped.items(), key=lambda x: str(x[0])):
        country, source, measure, definition, adjustment, unit = identity
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:16]
        latest = max(rows.values(), key=lambda r: r.get("available_at") or "")
        suffix = ADJUSTMENT.get(adjustment, adjustment or "")
        label = (
            f"{COUNTRIES.get(country, country)} {MEASURES.get(measure, measure.replace('_', ' '))}"
            + (f" ({suffix})" if suffix else "")
        )
        add(
            "release_" + digest,
            label,
            "경제지표",
            unit,
            source,
            latest.get("source_url"),
            "reference_month"
            if all(len(r.get("reference_period") or "") == 7 for r in rows.values())
            else "observation_date",
            [(day, r["value"]) for day, r in rows.items()],
            note="기준기간별 최신 수집 관측치입니다. 최초 발표값과 수정값은 구분된 발표 기록을 확인하세요.",
            updated_at=latest.get("available_at"),
            release_dates={
                day: r["actual_release_date"]
                for day, r in rows.items()
                if r.get("actual_release_date")
            },
            release_urls={day: r["source_url"] for day, r in rows.items() if r.get("source_url")},
        )

    for model in read(directory, "market-chart-model-inputs.json").get("models", []):
        for row in model.get("rows", []):
            if row.get("availability") != "observed_in_forecast" or not number(row.get("value")):
                continue
            key, transform = row.get("chart_series_key"), row.get("chart_transform")
            suffixes = {"return1": "_return", "return5": "_return_5d", "return20": "_return_20d"}
            sid = (
                key
                if transform == "level"
                else key + suffixes[transform]
                if transform in suffixes and isinstance(key, str)
                else None
            )
            series = by_id.get(sid)
            if not series:
                continue
            graph_value = dict(series["points"]).get(row.get("date"))
            value = (
                row["value"] * 100
                if transform in suffixes and row.get("unit") == "ratio"
                else row["value"]
            )
            if not number(graph_value) or not math.isclose(
                value, graph_value, rel_tol=1e-7, abs_tol=1e-5
            ):
                continue
            result["model_links"].append(
                {
                    **{
                        field: model.get(field)
                        for field in (
                            "run_id",
                            "publication_id",
                            "target_date",
                            "asof_date",
                            "forecast_id",
                            "prediction_cutoff",
                        )
                    },
                    "market": model.get("market"),
                    "model": model.get("key"),
                    "issue_stage": model.get("issue_stage"),
                    "input_key": row.get("key"),
                    "label": row.get("label"),
                    "series_id": sid,
                    "transform": transform,
                    "date": row.get("date"),
                    "value": row["value"],
                    "unit": row.get("unit"),
                }
            )
    return result


REQUIRED = {
    "market-chart-history.json": ("market_chart_history_v1", {"series": dict}),
    "market-chart-model-inputs.json": ("market_chart_model_inputs_v1", {"models": list}),
    "kospi_investor_flow_chart.json": ("kospi_investor_flow_chart_v1", {"markets": dict}),
    "margin_credit_chart.json": ("margin_credit_chart_v1", {"markets": dict}),
    "cfd_market_history.json": ("cfd_market_history_v1", {"markets": dict}),
    "cfd_frees_is_history.json": ("cfd_frees_is_history_v1", {"series": dict}),
    "cfd_status.json": ("cfd_status_v1", {"frees_is_series_status": dict}),
    "unofficial_numerics.json": ("unofficial_numerics_v1", {"series": list}),
    "market_indicators_status.json": ("market_indicators_status_v1", {"observations": list}),
    "economic_history.json": (
        "economic_history_v1",
        {"release_versions": list, "event_versions": list},
    ),
    "economic_calendar.json": ("economic_calendar_v1", {"events": list, "recent_releases": list}),
    "market-indicator-catalog.json": ("market_indicator_catalog_v1", {"series": list}),
}


def validate_inputs(directory):
    """Fail before atomic replacement if a required public artifact is incomplete."""
    inputs = {}
    for name, (version, fields) in REQUIRED.items():
        path = directory / name
        if not path.is_file():
            raise ValueError(f"missing required public artifact: {name}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != version:
            raise ValueError(f"invalid schema: {name}")
        if any(not isinstance(payload.get(field), kind) for field, kind in fields.items()):
            raise ValueError(f"invalid content: {name}")
        inputs[name] = payload
    if not {"kospi", "kosdaq"} <= inputs["market-chart-history.json"]["series"].keys():
        raise ValueError("missing domestic chart series")
    for name in (
        "kospi_investor_flow_chart.json",
        "margin_credit_chart.json",
        "cfd_market_history.json",
    ):
        if not {"KOSPI", "KOSDAQ"} <= inputs[name]["markets"].keys():
            raise ValueError(f"missing markets: {name}")
    if (
        not {"with_margin", "without_margin"}
        <= inputs["cfd_frees_is_history.json"]["series"].keys()
    ):
        raise ValueError("missing CFD definitions")
    if (
        not inputs["unofficial_numerics.json"]["series"]
        or not inputs["market-indicator-catalog.json"]["series"]
    ):
        raise ValueError("missing snapshot observations or prior catalog")


def validate_output(payload):
    series = payload["series"]
    required_strings = (
        "id",
        "label",
        "category",
        "unit",
        "source",
        "date_basis",
        "status",
        "chart_kind",
    )
    nullable_strings = ("source_url", "note", "updated_at")
    for item in series:
        if not isinstance(item, dict):
            raise ValueError("invalid series entry")
        for field in required_strings:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"invalid series {field}")
        for field in nullable_strings:
            if item.get(field) is not None and not isinstance(item[field], str):
                raise ValueError(f"invalid series {field}")
        if not isinstance(item.get("points"), list) or not isinstance(
            item.get("missing_dates"), list
        ):
            raise ValueError("invalid series observations")
        for point in item["points"]:
            if (
                not isinstance(point, list)
                or len(point) != 2
                or not valid_date(point[0])
                or not number(point[1])
            ):
                raise ValueError("invalid series point")
        if not all(valid_date(day) for day in item["missing_dates"]):
            raise ValueError("invalid missing dates")
        for field in ("release_dates", "release_urls"):
            if field in item and (
                not isinstance(item[field], dict)
                or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in item[field].items()
                )
            ):
                raise ValueError(f"invalid series {field}")
    if len({item["id"] for item in series}) != len(series):
        raise ValueError("duplicate series IDs")
    required = {"국내 지수", "해외 지수", "환율·원자재", "금리", "경제지표", "수급", "신용·CFD"}
    if not required <= {item["category"] for item in series}:
        raise ValueError("incomplete market categories")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "docs/data")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    output = args.output or args.data_dir / "market-indicator-catalog.json"
    validate_inputs(args.data_dir)
    payload = build_catalog(args.data_dir)
    validate_output(payload)
    atomic_json(output, payload)
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
