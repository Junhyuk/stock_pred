import json
import math
from pathlib import Path

import pytest

from scripts.export_market_indicator_catalog import build_catalog, validate_output


def write(base, name, obj):
    (base / name).write_text(json.dumps(obj), encoding="utf-8")


def test_catalog_preserves_sources_and_avoids_false_history(tmp_path):
    write(
        tmp_path,
        "market-chart-history.json",
        {
            "series": {
                "kospi": {
                    "label": "KOSPI",
                    "unit": "index_points",
                    "source": "Naver",
                    "date_basis": "session_date",
                    "points": [["2026-01-02", 100], ["2026-01-05", 110], ["2026-01-06", None]],
                },
                "ewy": {
                    "label": "iShares ETF",
                    "unit": "USD",
                    "source": "Yahoo",
                    "date_basis": "session_date",
                    "points": [["2026-01-05", 10]],
                },
                "nvda": {
                    "label": "NVIDIA 주식",
                    "unit": "USD",
                    "source": "Yahoo",
                    "date_basis": "session_date",
                    "points": [["2026-01-05", 10]],
                },
                "kospi_return": {
                    "label": "KOSPI 수익률",
                    "unit": "percent",
                    "source": "Naver",
                    "date_basis": "session_date",
                    "points": [["2026-01-05", 10]],
                },
                "kr_inflation": {
                    "label": "물가",
                    "unit": "percent",
                    "source": "official",
                    "date_basis": "reference_month",
                    "note": "latest revised series",
                    "release_dates": {"2026-01-01": "2026-02-01"},
                    "points": [["2026-01-01", 2.3]],
                },
            }
        },
    )
    write(
        tmp_path,
        "kospi_investor_flow_chart.json",
        {
            "source": "Naver",
            "unit": "억원",
            "markets": {
                "KOSPI": {
                    "sessions": [
                        {
                            "date": "2026-01-05",
                            "foreign_eok": 3,
                            "institution_eok": -2,
                            "personal_eok": None,
                        }
                    ]
                }
            },
        },
    )
    write(
        tmp_path,
        "cfd_frees_is_history.json",
        {
            "source": "FreeSIS",
            "unit": "KRW",
            "series": {
                "with_margin": [{"date": "2026-01-05", "total_krw": 123}],
                "without_margin": [{"date": "2026-01-05", "total_krw": 99}],
            },
        },
    )
    write(
        tmp_path,
        "cfd_status.json",
        {"frees_is_series_status": {"with_margin": "stale", "without_margin": "ready"}},
    )
    write(
        tmp_path,
        "unofficial_numerics.json",
        {
            "received_at": "2026-01-06T00:00:00+00:00",
            "series": [
                {
                    "id": "btc",
                    "label": "BTC",
                    "date": "2026-01-05",
                    "value": 500,
                    "unit": "USD",
                    "source": "Yahoo",
                },
                {
                    "id": "kospi",
                    "label": "KOSPI 지수",
                    "date": "2026-01-05",
                    "value": 999,
                    "unit": "index_points",
                    "source": "Yahoo",
                },
            ],
        },
    )
    write(
        tmp_path,
        "market-chart-model-inputs.json",
        {
            "models": [
                {
                    "market": "KOSPI",
                    "key": "C",
                    "issue_stage": "premarket",
                    "rows": [
                        {
                            "key": "close",
                            "label": "close",
                            "chart_series_key": "kospi",
                            "chart_transform": "level",
                            "date": "2026-01-05",
                            "value": 110,
                            "unit": "index_points",
                            "availability": "observed_in_forecast",
                        },
                        {
                            "key": "ret",
                            "label": "return",
                            "chart_series_key": "kospi",
                            "chart_transform": "return1",
                            "date": "2026-01-05",
                            "value": 0.1,
                            "unit": "ratio",
                            "availability": "observed_in_forecast",
                        },
                        {
                            "key": "bad",
                            "chart_series_key": "kospi",
                            "chart_transform": "level",
                            "date": "2026-01-05",
                            "value": 500,
                            "availability": "observed_in_forecast",
                        },
                        {
                            "key": "missing",
                            "chart_series_key": "kospi",
                            "chart_transform": "level",
                            "date": "2026-01-05",
                            "value": 110,
                            "availability": "reconstructed",
                        },
                    ],
                }
            ]
        },
    )
    payload = build_catalog(tmp_path, now="2026-01-06T01:00:00+00:00")
    series = {s["id"]: s for s in payload["series"]}
    assert "ewy" not in series and "nvda" not in series
    assert series["kospi"]["points"] == [["2026-01-02", 100], ["2026-01-05", 110]]
    assert series["kr_inflation"]["release_dates"]["2026-01-01"] == "2026-02-01"
    assert series["flow_kospi_foreign"]["points"] == [["2026-01-05", 3]]
    assert series["cfd_with_margin_total"]["status"] == "stale"
    assert series["cfd_without_margin_total"]["points"] == [["2026-01-05", 99]]
    assert series["snapshot_btc"]["points"] == [["2026-01-05", 500]]
    assert "관측" in series["snapshot_btc"]["label"]
    assert series["snapshot_kospi"]["points"] == [["2026-01-05", 999]]
    assert series["kospi"]["missing_dates"] == ["2026-01-06"]
    assert [(x["input_key"], x["series_id"]) for x in payload["model_links"]] == [
        ("close", "kospi"),
        ("ret", "kospi_return"),
    ]


def test_release_versions_and_events_keep_temporal_semantics(tmp_path):
    release = {
        "version_id": "v1",
        "event_id": "e",
        "country": "US",
        "measure": "cpi",
        "reference_period": "2026-01",
        "value": 3.2,
        "unit": "percent",
        "vintage": "revision",
        "revision_of": "v0",
        "available_at": "2026-03-01T00:00:00+00:00",
        "actual_release_date": None,
        "source_id": "bls",
    }
    event = {
        "event_id": "e",
        "title": "CPI",
        "country": "US",
        "event_type": "inflation",
        "scheduled_date": "2026-03-01",
        "time_precision": "date",
        "status": "tentative",
        "available_at": "2026-02-01T00:00:00+00:00",
    }
    write(
        tmp_path,
        "economic_history.json",
        {"release_versions": [release], "event_versions": [event]},
    )
    payload = build_catalog(tmp_path, now="2026-03-02T00:00:00+00:00")
    assert payload["releases"][0]["revision_of"] == "v0"
    release_series = [s for s in payload["series"] if s["id"].startswith("release_")]
    assert len(release_series) == 1
    assert release_series[0]["points"] == [["2026-01-01", 3.2]]
    assert "최신" in release_series[0]["note"]
    assert payload["releases"][0]["actual_release_date"] is None
    assert payload["events"][0]["scheduled_at"] is None
    assert payload["events"][0]["available_at"] == event["available_at"]


def test_real_public_catalog_is_finite_unique_and_has_market_coverage():
    data = Path(__file__).resolve().parents[1] / "docs/data"
    payload = build_catalog(data, now="2026-11-28T00:00:00+00:00")
    series = {s["id"]: s for s in payload["series"]}
    assert len(series) == len(payload["series"])
    assert len({(s["category"], s["label"]) for s in payload["series"]}) == len(payload["series"])
    assert {
        "kospi",
        "kr_10y",
        "us_inflation",
        "flow_kospi_foreign",
        "margin_kospi",
        "cfd_with_margin_total",
        "cfd_without_margin_total",
    } <= series.keys()
    assert payload["events"] and payload["releases"]
    for market in ("kospi", "kosdaq"):
        disclosed = series[f"cfd_disclosed_{market}"]
        assert disclosed["source"] == "금융투자협회 종목별 CFD 잔고동향"
        assert disclosed["source_url"] == "https://www.kofia.or.kr/brd/m_198/list.do"
    validate_output(payload)
    for item in series.values():
        assert item["points"] == sorted(item["points"])
        assert len({p[0] for p in item["points"]}) == len(item["points"])
        assert all(math.isfinite(p[1]) for p in item["points"])


def test_snapshot_backfill_keeps_only_same_source_and_unit(tmp_path):
    write(
        tmp_path,
        "market-indicator-catalog.json",
        {
            "series": [
                {
                    "id": "snapshot_btc",
                    "source": "old",
                    "unit": "USD",
                    "points": [["2026-01-01", 10]],
                }
            ]
        },
    )
    write(
        tmp_path,
        "unofficial_numerics.json",
        {
            "series": [
                {
                    "id": "btc",
                    "label": "BTC",
                    "source": "new",
                    "unit": "USD",
                    "date": "2026-01-02",
                    "value": 12,
                }
            ]
        },
    )
    series = {
        s["id"]: s for s in build_catalog(tmp_path, now="2026-01-03T00:00:00+00:00")["series"]
    }
    assert series["snapshot_btc"]["points"] == [["2026-01-02", 12]]


def test_revisions_are_not_relabelled_as_first_releases(tmp_path):
    common = {
        "event_id": "e",
        "country": "US",
        "measure": "pce",
        "reference_period": "2026-01",
        "unit": "percent",
        "source_id": "bea",
        "definition_version": "pce:v1",
        "seasonal_adjustment": "SA",
    }
    first = dict(
        common,
        version_id="v0",
        value=2.0,
        vintage="first_release",
        available_at="2026-02-01T00:00:00+00:00",
    )
    revised = dict(
        common,
        version_id="v1",
        value=2.2,
        vintage="revision",
        revision_of="v0",
        available_at="2026-03-01T00:00:00+00:00",
    )
    write(tmp_path, "economic_history.json", {"release_versions": [revised, first]})
    result = build_catalog(tmp_path, now="2026-03-02T00:00:00+00:00")
    assert {r["id"] for r in result["releases"]} == {"v0", "v1"}
    series = next(s for s in result["series"] if s["id"].startswith("release_"))
    assert series["points"] == [["2026-01-01", 2.2]]
    assert next(r for r in result["releases"] if r["id"] == "v1")["revision_of"] == "v0"


def test_workflow_and_public_allowlist_publish_only_reviewed_catalog():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/premarket-research-forecast.yml").read_text()
    allowlist = (root / "src/roboquant/index_forecast/automation_state.py").read_text()
    assert (
        workflow.index("python scripts/refresh_cfd_dashboard.py")
        < workflow.index("python scripts/export_market_indicator_catalog.py")
        < workflow.index("name: Commit reviewed public payloads")
    )
    assert "'market-indicator-catalog.json'" in allowlist
    assert "sqlite" not in (root / "docs/data/market-indicator-catalog.json").read_text().lower()


def test_multi_day_returns_require_verified_consecutive_sessions(tmp_path):
    from datetime import date, timedelta

    dates = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(23)]
    closes = [[day, 100 + i] for i, day in enumerate(dates)]
    verified = [[day, 1] for day in dates[1:] if day != dates[7]]
    write(
        tmp_path,
        "market-chart-history.json",
        {
            "series": {
                "kospi": {
                    "label": "KOSPI",
                    "unit": "index_points",
                    "source": "Naver",
                    "date_basis": "session_date",
                    "points": closes,
                },
                "kospi_return": {
                    "label": "daily",
                    "unit": "percent",
                    "source": "Naver",
                    "date_basis": "session_date",
                    "points": verified,
                },
            }
        },
    )
    write(
        tmp_path,
        "market-chart-model-inputs.json",
        {
            "models": [
                {
                    "market": "KOSPI",
                    "key": "B",
                    "run_id": "run1",
                    "publication_id": "pub1",
                    "target_date": "2026-02-02",
                    "asof_date": "2026-01-31",
                    "forecast_id": "fc1",
                    "prediction_cutoff": "2026-01-31T00:00:00+00:00",
                    "rows": [
                        {
                            "key": "return_5d",
                            "chart_series_key": "kospi",
                            "chart_transform": "return5",
                            "date": dates[5],
                            "value": 0.05,
                            "unit": "ratio",
                            "availability": "observed_in_forecast",
                        }
                    ],
                }
            ]
        },
    )
    payload = build_catalog(tmp_path, now="2026-02-01T00:00:00+00:00")
    series = {s["id"]: s for s in payload["series"]}
    assert payload["model_links"][0]["series_id"] == "kospi_return_5d"
    assert {
        k: payload["model_links"][0][k]
        for k in (
            "run_id",
            "publication_id",
            "target_date",
            "asof_date",
            "forecast_id",
            "prediction_cutoff",
        )
    } == {
        "run_id": "run1",
        "publication_id": "pub1",
        "target_date": "2026-02-02",
        "asof_date": "2026-01-31",
        "forecast_id": "fc1",
        "prediction_cutoff": "2026-01-31T00:00:00+00:00",
    }
    values = dict(series["kospi_return_5d"]["points"])
    assert abs(values[dates[5]] - 5) < 1e-9
    assert dates[7] not in values and dates[11] not in values
    assert dates[7] in series["kospi_return_5d"]["missing_dates"]
    assert dates[12] in values
    assert dates[20] not in dict(series["kospi_return_20d"]["points"])
    assert series["kospi_return_5d"]["unit"] == "percent"


def test_cli_preserves_last_good_catalog_if_required_input_is_missing_or_wrong(tmp_path):
    import shutil
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    for name in (
        "market-chart-history.json",
        "market-chart-model-inputs.json",
        "kospi_investor_flow_chart.json",
        "margin_credit_chart.json",
        "cfd_market_history.json",
        "cfd_frees_is_history.json",
        "cfd_status.json",
        "unofficial_numerics.json",
        "market_indicators_status.json",
        "economic_history.json",
        "economic_calendar.json",
        "market-indicator-catalog.json",
    ):
        shutil.copy(root / "docs/data" / name, tmp_path / name)
    original = (tmp_path / "market-indicator-catalog.json").read_bytes()
    command = [
        sys.executable,
        str(root / "scripts/export_market_indicator_catalog.py"),
        "--data-dir",
        str(tmp_path),
    ]
    (tmp_path / "economic_history.json").unlink()
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert (tmp_path / "market-indicator-catalog.json").read_bytes() == original
    write(tmp_path, "economic_history.json", {"schema_version": "wrong"})
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert (tmp_path / "market-indicator-catalog.json").read_bytes() == original


def test_cli_custom_output_uses_public_seed_without_modifying_it(tmp_path):
    import shutil
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    for name in (
        "market-chart-history.json",
        "market-chart-model-inputs.json",
        "kospi_investor_flow_chart.json",
        "margin_credit_chart.json",
        "cfd_market_history.json",
        "cfd_frees_is_history.json",
        "cfd_status.json",
        "unofficial_numerics.json",
        "market_indicators_status.json",
        "economic_history.json",
        "economic_calendar.json",
        "market-indicator-catalog.json",
    ):
        shutil.copy(root / "docs/data" / name, tmp_path / name)
    old = (tmp_path / "market-indicator-catalog.json").read_bytes()
    output = tmp_path / "replay.json"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts/export_market_indicator_catalog.py"),
            "--data-dir",
            str(tmp_path),
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    assert (tmp_path / "market-indicator-catalog.json").read_bytes() == old
    replay = json.loads(output.read_text())
    assert len(next(s for s in replay["series"] if s["id"] == "snapshot_btc")["points"]) >= 2


def test_partial_snapshot_retains_stale_series_and_merges_on_recovery(tmp_path):
    snapshot = {
        "received_at": "2026-01-01T01:00:00+00:00",
        "series": [
            {
                "id": "btc",
                "label": "비트코인",
                "date": "2026-01-01",
                "value": 100,
                "unit": "USD",
                "source": "yahoo_chart_unofficial",
            },
            {
                "id": "eth",
                "label": "이더리움",
                "date": "2026-01-01",
                "value": 20,
                "unit": "USD",
                "source": "yahoo_chart_unofficial",
            },
        ],
    }
    official = {
        "sources": {"us_yields": {"status": "ready"}},
        "observations": [
            {
                "market": "GLOBAL",
                "series": "US_YIELD_1Y",
                "observation_date": "2026-01-01",
                "value": 4.1,
                "unit": "percent",
                "source": "us_treasury_daily_par_yield_curve",
                "received_at": "2026-01-01T02:00:00+00:00",
            },
            {
                "market": "GLOBAL",
                "series": "US_YIELD_3Y",
                "observation_date": "2026-01-01",
                "value": 4.2,
                "unit": "percent",
                "source": "us_treasury_daily_par_yield_curve",
            },
        ],
    }
    write(tmp_path, "unofficial_numerics.json", snapshot)
    write(tmp_path, "market_indicators_status.json", official)
    seed = build_catalog(tmp_path, now="2026-01-01T03:00:00+00:00")
    write(tmp_path, "market-indicator-catalog.json", seed)
    snapshot["series"] = snapshot["series"][1:]
    official["observations"] = official["observations"][1:]
    write(tmp_path, "unofficial_numerics.json", snapshot)
    write(tmp_path, "market_indicators_status.json", official)
    partial = build_catalog(tmp_path, now="2026-01-02T03:00:00+00:00")
    series = {s["id"]: s for s in partial["series"]}
    assert series["snapshot_btc"]["points"] == [["2026-01-01", 100]]
    assert series["snapshot_btc"]["status"] == "stale"
    assert series["snapshot_btc"]["updated_at"] == "2026-01-01T01:00:00+00:00"
    assert series["official_us_yield_1y_global"]["points"] == [["2026-01-01", 4.1]]
    assert series["official_us_yield_1y_global"]["status"] == "stale"
    write(tmp_path, "market-indicator-catalog.json", partial)
    snapshot["series"].append(
        {
            "id": "btc",
            "label": "비트코인",
            "date": "2026-01-03",
            "value": 105,
            "unit": "USD",
            "source": "yahoo_chart_unofficial",
        }
    )
    snapshot["received_at"] = "2026-01-03T01:00:00+00:00"
    official["observations"].append(
        {
            "market": "GLOBAL",
            "series": "US_YIELD_1Y",
            "observation_date": "2026-01-03",
            "value": 4.3,
            "unit": "percent",
            "source": "us_treasury_daily_par_yield_curve",
            "received_at": "2026-01-03T02:00:00+00:00",
        }
    )
    write(tmp_path, "unofficial_numerics.json", snapshot)
    write(tmp_path, "market_indicators_status.json", official)
    recovered = {
        s["id"]: s for s in build_catalog(tmp_path, now="2026-01-03T03:00:00+00:00")["series"]
    }
    assert recovered["snapshot_btc"]["points"] == [["2026-01-01", 100], ["2026-01-03", 105]]
    assert recovered["snapshot_btc"]["status"] != "stale"
    assert recovered["official_us_yield_1y_global"]["points"] == [
        ["2026-01-01", 4.1],
        ["2026-01-03", 4.3],
    ]
    assert recovered["official_us_yield_1y_global"]["status"] == "ready"


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", {"label": "invalid"}),
        ("source", None),
        ("source", ""),
        ("label", 42),
        ("id", None),
        ("category", []),
        ("unit", None),
        ("date_basis", 1),
        ("status", {}),
        ("chart_kind", []),
        ("source_url", {}),
        ("note", []),
        ("updated_at", 10),
        ("points", {}),
        ("missing_dates", {}),
    ],
)
def test_validation_rejects_invalid_series_metadata(field, value):
    data = Path(__file__).resolve().parents[1] / "docs/data"
    payload = build_catalog(data, now="2026-11-28T00:00:00+00:00")
    payload["series"][0][field] = value
    with pytest.raises(ValueError):
        validate_output(payload)


def test_disclosed_cfd_source_object_maps_label_and_url(tmp_path):
    write(
        tmp_path,
        "cfd_market_history.json",
        {
            "source": {"label": "공식 출처", "url": "https://example.org/cfd"},
            "unit": "KRW",
            "markets": {"KOSPI": [{"date": "2026-01-01", "value_krw": 100}]},
        },
    )
    payload = build_catalog(tmp_path, now="2026-01-02T00:00:00+00:00")
    cfd = next(item for item in payload["series"] if item["id"] == "cfd_disclosed_kospi")
    assert cfd["source"] == "공식 출처"
    assert cfd["source_url"] == "https://example.org/cfd"


def test_cli_preserves_catalog_when_source_metadata_is_not_a_string(tmp_path):
    import shutil
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    from scripts.export_market_indicator_catalog import REQUIRED

    for name in REQUIRED:
        shutil.copy(root / "docs/data" / name, tmp_path / name)
    original = (tmp_path / "market-indicator-catalog.json").read_bytes()
    path = tmp_path / "market-chart-history.json"
    history = json.loads(path.read_text())
    history["series"]["kospi"]["source"] = {"label": "bad"}
    path.write_text(json.dumps(history))
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/export_market_indicator_catalog.py"),
            "--data-dir",
            str(tmp_path),
        ],
        capture_output=True,
    )
    assert result.returncode != 0
    assert b"invalid series source" in result.stderr
    assert (tmp_path / "market-indicator-catalog.json").read_bytes() == original
